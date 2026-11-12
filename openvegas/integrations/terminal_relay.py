"""Own one terminal, display either its ordinary shell or a human game screen.

Hosts run unmodified on a PTY. Only this relay reads/writes the outer terminal.
A game gets a separate PTY, never the host's tty; no input injection or SIGSTOP.
"""
from __future__ import annotations

import atexit
import fcntl
import hmac
import json
import os
import pty
import re
import secrets
import select
import signal
import socket
import struct
import sys
import tempfile
import termios
import time
import tty
from contextlib import suppress
from pathlib import Path

from .session_ownership import RelayFence
from .terminal_handoff import SOCKET_ENV, TOKEN_ENV
from .terminal_wallet import (
    LOCAL_CONTROL_SHORTCUT,
    MAX_MESSAGE,
    WalletScreen,
    terminal_reset,
    wallet_label,
)

MAX_BACKLOG = 1024 * 1024


class InputOwnerLost(RuntimeError):
    pass


class Modes:
    """Track private VT modes without interpreting OSC/DCS image payloads."""
    def __init__(self):
        self.values = {7: True, 25: True}
        self.state, self.sequence = "text", bytearray()
        self.string_bel = False

    def feed(self, data):
        for byte in data:
            if byte in (24, 26):
                self.state = "text"
                self.sequence.clear()
                continue
            if byte == 27 and self.state in ("escape", "csi"):
                self.state = "escape"
                self.sequence.clear()
                continue
            if self.state == "string":
                if byte == 7 and self.string_bel:
                    self.state = "text"
                elif byte == 27:
                    self.state = "string_escape"
            elif self.state == "string_escape":
                self.state = "text" if byte == 92 else "string"
            elif self.state == "escape":
                if byte == 91:
                    self.state, self.sequence = "csi", bytearray()
                elif byte in (93, 80, 95, 94, 88):
                    self.state = "string"
                    self.string_bel = byte == 93
                else:
                    self.state = "text"
            elif self.state == "csi":
                self.sequence.append(byte)
                if 64 <= byte <= 126:
                    match = re.fullmatch(rb"\?([0-9;]+)([hl])", self.sequence)
                    if match:
                        for value in match[1].split(b";"):
                            if value:
                                self.values[int(value)] = match[2] == b"h"
                    self.state = "text"
                elif len(self.sequence) > 128:
                    self.state = "text"
            elif byte == 27:
                self.state = "escape"

    @property
    def alternate(self):
        return any(self.values.get(mode) for mode in (47, 1047, 1049))

    def restore(self):
        return b"".join(f"\x1b[?{mode}{'h' if self.values.get(mode, False) else 'l'}".encode()
                        for mode in (1, 7, 25, 1000, 1002, 1003, 1004, 1005,
                                     1006, 1015, 2004, 2026))


def write_all(fd, data):
    while data:
        count = os.write(fd, data)
        data = data[count:]


def drain_ready(fd, consume, *, limit=MAX_BACKLOG):
    """Drain queued PTY bytes, never wait on a descendant holding the slave open."""
    remaining = limit
    while remaining > 0 and select.select([fd], [], [], 0)[0]:
        try:
            data = os.read(fd, min(65536, remaining))
        except OSError:
            return True
        if not data:
            return True
        consume(data)
        remaining -= len(data)
    return not select.select([fd], [], [], 0)[0]


def handoff_input_clear(keys, fd=0):
    """Reject, rather than discard/replay, an incomplete input transaction."""
    return not (keys.pending or keys.paste or select.select([fd], [], [], 0)[0])


def stop_child(pid, *, timeout=.25):
    """Terminate only a PTY child's owned group, including a surviving renderer."""
    if not pid or pid == os.getpgrp():
        return
    def reap():
        try:
            return bool(os.waitpid(pid, os.WNOHANG)[0])
        except ChildProcessError:
            return True

    reaped = reap()
    # Darwin may report EPERM when the group's only remaining member is a
    # zombie. Reap before signalling and tolerate that exit/signalling race.
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not reaped:
            reaped = reap()
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            break
        time.sleep(.01)
    else:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)
    if not reaped:
        deadline = time.monotonic() + timeout
        while not reap() and time.monotonic() < deadline:
            time.sleep(.01)


def run(shell):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        # Shell -c used by profile verification does not require a relay.
        if "-c" in shell:
            os.execvpe(shell[0], shell, os.environ)
        raise RuntimeError("OpenVegas Games requires an interactive terminal.")
    if os.tcgetpgrp(0) != os.getpgrp():
        raise RuntimeError("OpenVegas Games does not own the foreground terminal.")
    original = termios.tcgetattr(0)
    owner_pid = os.getpid()
    modes = Modes()
    backlog = bytearray()
    game_pid = game_fd = connection = None
    host_pid = host_fd = None
    wallet_connection = None
    owner_connection = None
    input_nonce = None
    fence = RelayFence()
    wallet_buffer = bytearray()
    wallet = WalletScreen()
    wallet_resized = False
    from .local_controls import LocalKeys
    keys = LocalKeys()
    key_time = 0
    size = fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8))
    token = secrets.token_hex(32)
    directory = tempfile.TemporaryDirectory(prefix="ov-terminal-", dir="/tmp")
    os.chmod(directory.name, 0o700)
    listener = socket.socket(socket.AF_UNIX)
    path = str(Path(directory.name) / "game.sock")
    listener.bind(path)
    os.chmod(path, 0o600)
    listener.listen(2)
    from .terminal_shell import prepare
    shell_env = prepare(shell, {**os.environ, SOCKET_ENV: path, TOKEN_ENV: token},
                        Path(directory.name) / "shell")

    def resize(*_):
        nonlocal size, wallet_resized
        # A forked renderer can receive SIGWINCH before exec, while pty.fork's
        # child-side descriptor is -1. Only the relay owns viewport resizing.
        if os.getpid() != owner_pid:
            return
        size = fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8))
        wallet.rows, wallet.columns, xpixels, ypixels = struct.unpack('HHHH', size)
        wallet_resized = True
        host_size = struct.pack('HHHH', wallet.child_rows, wallet.columns, xpixels,
                               ypixels * wallet.child_rows // max(1, wallet.rows))
        for fd, dimensions in ((host_fd, host_size), (game_fd, size)):
            if fd is not None:
                try:
                    fcntl.ioctl(fd, termios.TIOCSWINSZ, dimensions)
                except OSError:
                    pass

    def close_wallet():
        nonlocal wallet_connection
        if wallet_connection is not None:
            wallet_connection.close()
            wallet_connection = None
        wallet_buffer.clear()

    def update_wallet():
        nonlocal wallet_resized
        # Serialize all drawing with the host's byte stream, never inside its
        # escape/image payload. Games use the full screen and their own wallet.
        if game_fd is not None or not wallet.safe:
            return
        wanted = wallet_connection is not None and wallet.rows >= 5 and wallet.columns >= 12
        if wanted != wallet.active:
            write_all(1, wallet.activate() if wanted else wallet.deactivate())
            resize()
        if wallet.active:
            if wallet_resized:
                write_all(1, b'\x1b7' + wallet.margins() + b'\x1b8')
            write_all(1, wallet.paint())
        wallet_resized = False

    def restore_game():
        nonlocal game_pid, game_fd, connection, backlog
        stop_child(game_pid)
        if game_fd is not None:
            os.close(game_fd)
        # Textual has left its alternate buffer. Restore host modes and flush only
        # output produced while hidden; never replay user keystrokes.
        # Returning to a live host must preserve its saved primary-screen cursor;
        # unlike a supervisor handoff, do not reset margins/home the cursor here.
        write_all(1, b'\x18\x1b\\\x1b[?2026l\x1b[?1049l\x1b[?1047l\x1b[?47l\x1b[0m'
                  + modes.restore())
        write_all(1, wallet.feed(backlog))
        modes.feed(backlog)
        backlog.clear()
        if connection:
            try:
                connection.sendall(b'{"closed":true}\n')
            except OSError:
                pass
            connection.close()
        game_pid = game_fd = connection = None
        resize()
        if wallet_connection is not None:
            try:
                wallet_connection.sendall(json.dumps({'refresh': True, **fence.fields()}).encode() + b'\n')
            except OSError:
                close_wallet()
        update_wallet()

    cleaned = False
    def cleanup():
        nonlocal cleaned
        if os.getpid() != owner_pid or cleaned:
            return
        cleaned = True
        try:
            for pid in (game_pid, host_pid):
                with suppress(OSError, ChildProcessError):
                    stop_child(pid)
            with suppress(OSError):
                write_all(1, terminal_reset(clear=True,
                                           alternate=game_fd is not None or modes.alternate))
                if wallet.active:
                    write_all(1, wallet.deactivate())
        finally:
            # A disconnected display must not strand the user's tty in raw mode.
            with suppress(OSError, termios.error):
                termios.tcsetattr(0, termios.TCSANOW, original)
            close_wallet()
            if owner_connection is not None:
                owner_connection.close()
            if connection is not None:
                connection.close()
            for fd in (game_fd, host_fd):
                if fd is not None:
                    with suppress(OSError):
                        os.close(fd)
            listener.close()
            directory.cleanup()

    atexit.register(cleanup)
    previous_winch = signal.signal(signal.SIGWINCH, resize)
    def stop(*_):
        raise SystemExit(0)
    previous_signals = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        host_pid, host_fd = pty.fork()
        if host_pid == 0:
            listener.close()
            # exec keeps the user's shell HOME, cwd, executable and host billing.
            try:
                os.execvpe(shell[0], shell, shell_env)
            except OSError:
                os.write(2, b'OpenVegas shell startup failed.\r\n')
                os._exit(1)
        resize()
        # setraw defaults to TCSAFLUSH: a command already typed while the
        # managed shell was starting would disappear before our first read.
        tty.setraw(0, termios.TCSANOW)
        while True:
            if input_nonce is None and keys.pending and time.monotonic() - key_time > keys.wait_seconds:
                write_all(host_fd, keys.flush())
            if wallet_resized:
                update_wallet()
            # Pets/progress can keep drawing while their host is hidden. Apply
            # PTY backpressure at the buffer limit, never kill a human game or
            # discard part of an escape/image sequence to make room.
            host_ready = game_fd is None or len(backlog) < MAX_BACKLOG
            readable, _, _ = select.select([listener, *([0] if input_nonce is None else []), *([host_fd] if host_ready else []),
                                           *([game_fd] if game_fd is not None else []),
                                           *([wallet_connection] if wallet_connection is not None else []),
                                           *([owner_connection] if owner_connection is not None else [])], [], [], .1)
            for ready in readable:
                if ready == listener:
                    candidate, _ = listener.accept()
                    candidate.settimeout(1)
                    try:
                        data = bytearray()
                        while not data.endswith(b"\n") and len(data) < MAX_MESSAGE:
                            piece = candidate.recv(1)
                            if not piece:
                                break
                            data += piece
                        request = json.loads(data)
                        valid = (type(request) is dict and request.get("kind") in {"game", "status", "wallet", "handoff", "owner", "input"}
                                 and isinstance(request.get("token"), str) and hmac.compare_digest(request["token"], token))
                        if not valid:
                            candidate.close()
                            continue
                        if (owner_connection is not None
                                and select.select([owner_connection], [], [], 0)[0]
                                and not owner_connection.recv(1, socket.MSG_PEEK)):
                            owner_connection.close()
                            owner_connection = None
                            if input_nonce is not None:
                                raise InputOwnerLost('Billing owner disconnected while terminal input was held.')
                            fence.disconnect()
                            close_wallet()
                            update_wallet()
                        if request['kind'] == 'owner':
                            # Only one live controller. EOF releases the lease without
                            # consuming input or clearing terminal buffers.
                            if owner_connection is not None or wallet_connection is not None or game_fd is not None or not fence.claim(request):
                                candidate.close()
                                continue
                            owner_connection = candidate
                            candidate.settimeout(None)
                            candidate.sendall(json.dumps({'ready': True, **fence.fields()}).encode() + b'\n')
                            continue
                        if not fence.matches(request):
                            candidate.close()
                            continue
                        if request['kind'] == 'input':
                            nonce = request.get('nonce')
                            valid = (owner_connection is not None and game_fd is None
                                     and isinstance(nonce, str) and re.fullmatch(r'[a-f0-9]{32}', nonce)
                                     and type(request.get('hold')) is bool)
                            if (valid and request['hold'] and input_nonce is None
                                    and not keys.paste and not keys.pending):
                                input_nonce = nonce
                            elif valid and not request['hold'] and input_nonce == nonce:
                                input_nonce = None
                            else:
                                candidate.close()
                                continue
                            candidate.sendall(json.dumps({'ready': True, 'held': input_nonce is not None,
                                'nonce': nonce, **fence.fields()}).encode() + b'\n')
                            candidate.close()
                            continue
                        if request['kind'] == 'handoff':
                            def flush_host(data, *, modes=modes, wallet=wallet):
                                modes.feed(data)
                                write_all(1, wallet.feed(data))
                            if (input_nonce is not None or game_fd is not None or not handoff_input_clear(keys)
                                    or not drain_ready(host_fd, flush_host)
                                    or not handoff_input_clear(keys)):
                                candidate.sendall(b'{"reset":false}\n')
                            else:
                                close_wallet()
                                rows, columns = wallet.rows, wallet.columns
                                wallet = WalletScreen()
                                wallet.rows, wallet.columns = rows, columns
                                modes = Modes()
                                keys = LocalKeys()
                                write_all(1, terminal_reset(clear=True))
                                resize()
                                candidate.sendall(b'{"reset":true}\n')
                                fence.consume_handoff()
                            candidate.close()
                            # drain_ready consumed any host_fd readiness snapshot.
                            break
                        if request['kind'] == 'wallet':
                            if wallet_connection is not None or request.get('billing') not in {'host', 'v'}:
                                candidate.close()
                                continue
                            wallet_connection = candidate
                            candidate.settimeout(None)
                            wallet.payload = {'state': 'loading', 'billing': request['billing']}
                            update_wallet()
                            candidate.sendall(json.dumps({'wallet': 1,
                                'controls_shortcut': LOCAL_CONTROL_SHORTCUT, **fence.fields()}).encode() + b'\n')
                            continue
                        if request['kind'] == 'status':
                            candidate.sendall(b'{"ready":true,"ownership_protocol":1,"input_protocol":1}\n')
                            candidate.close()
                            continue
                        if input_nonce is not None:
                            candidate.sendall(b'{"ready":false,"message":"Billing handoff is holding terminal input."}\n')
                            candidate.close()
                            continue
                        if game_fd is not None or modes.alternate:
                            candidate.sendall(b'{"ready":false,"message":"Terminal is already in a full-screen view or game. Return to the normal coding prompt first; no wager placed."}\n')
                            candidate.close()
                            continue
                        if not handoff_input_clear(keys):
                            candidate.sendall(b'{"ready":false,"message":"Finish typing or pasting, then open Play again. No wager placed."}\n')
                            candidate.close()
                            continue
                        candidate.sendall(b'{"ready":true}\n')
                        candidate.settimeout(None)
                        connection = candidate
                        # Stop host mouse/paste modes only on the owned outer terminal.
                        write_all(1, b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2026l")
                        if wallet.active:
                            write_all(1, b'\x1b7\x1b[r\x1b8')
                        game_pid, game_fd = pty.fork()
                        if game_pid == 0:
                            listener.close()
                            os.close(host_fd)
                            if wallet_connection is not None:
                                wallet_connection.close()
                            try:
                                from .terminal_handoff import exec_renderer
                                exec_renderer(connection)
                            except Exception as exc:  # noqa: BLE001
                                # Never hide startup failures behind a successful
                                # exit or print exception payloads/credentials.
                                os.write(2, ('\r\nOpenVegas renderer startup failed (' +
                                             type(exc).__name__ + ').\r\n').encode())
                                os._exit(1)
                        resize()
                    except (OSError, ValueError):
                        candidate.close()
                elif ready == owner_connection:
                    # The controller never sends application data on its lease.
                    if input_nonce is not None:
                        raise InputOwnerLost('Billing owner disconnected while terminal input was held.')
                    owner_connection.close()
                    owner_connection = None
                    fence.disconnect()
                    close_wallet()
                    update_wallet()
                elif ready == wallet_connection:
                    try:
                        data = wallet_connection.recv(MAX_MESSAGE)
                        if not data:
                            close_wallet()
                        else:
                            wallet_buffer.extend(data)
                            while b'\n' in wallet_buffer:
                                line, _, remaining = wallet_buffer.partition(b'\n')
                                wallet_buffer[:] = remaining
                                if len(line) > MAX_MESSAGE:
                                    raise ValueError
                                payload = json.loads(line)
                                if (type(payload) is not dict or not fence.matches(payload) or payload.get('state') not in
                                        {'ready', 'loading', 'login', 'unavailable'}):
                                    raise ValueError
                                # Billing comes from the authenticated lease, not wallet JSON.
                                payload['billing'] = wallet.payload['billing']
                                payload['controls'] = (payload.get('controls') is True and
                                    payload.get('controls_shortcut') == LOCAL_CONTROL_SHORTCUT)
                                wallet_label(payload, wallet.columns)
                                wallet.payload = payload
                            if len(wallet_buffer) > MAX_MESSAGE:
                                raise ValueError
                    except (OSError, ValueError, TypeError):
                        close_wallet()
                    update_wallet()
                elif ready == 0:
                    if input_nonce is not None:
                        continue
                    data = os.read(0, 65536)
                    if not data:
                        return
                    if game_fd is not None:
                        # Escape, paste, and mouse packets belong entirely to the
                        # game; never carry a partial packet back to the host.
                        write_all(game_fd, data)
                        continue
                    data, menu = keys.feed(data, not modes.alternate
                                           and wallet_connection is not None
                                           and wallet.payload.get('controls') is True)
                    key_time = time.monotonic()
                    if menu:
                        try:
                            wallet_connection.sendall(json.dumps({'control': 'menu', **fence.fields()}).encode() + b'\n')
                        except OSError:
                            close_wallet()
                    write_all(host_fd, data)
                elif ready == host_fd:
                    try:
                        limit = min(65536, MAX_BACKLOG - len(backlog)) if game_fd is not None else 65536
                        data = os.read(host_fd, limit)
                    except OSError:
                        data = b""
                    if not data:
                        return
                    if game_fd is None:
                        modes.feed(data)
                        write_all(1, wallet.feed(data))
                        update_wallet()
                    else:
                        backlog += data
                elif ready == game_fd:
                    try:
                        data = os.read(game_fd, 65536)
                    except OSError:
                        data = b""
                    if data:
                        write_all(1, data)
                    else:
                        restore_game()
            if game_pid is not None:
                exited, _ = os.waitpid(game_pid, os.WNOHANG)
                if exited:
                    # Drain final screen-restoration bytes before reporting done.
                    drain_ready(game_fd, lambda data: write_all(1, data))
                    restore_game()
    finally:
        signal.signal(signal.SIGWINCH, previous_winch)
        for sig, handler in previous_signals.items():
            signal.signal(sig, handler)
        cleanup()
        atexit.unregister(cleanup)


def main(argv=None):
    shell = list(sys.argv[1:] if argv is None else argv)
    if shell and shell[0] == "--":
        shell = shell[1:]
    try:
        run(shell or ["/bin/zsh", "-l"])
    except KeyboardInterrupt:
        # An interrupt sent to the relay process, not a key on the native PTY.
        raise SystemExit(130) from None
    except InputOwnerLost:
        print('OpenVegas stopped the managed terminal because its billing owner disconnected. '
              'Held input was not sent to a payer or shell.', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
