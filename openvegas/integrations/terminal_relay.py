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
import tty
import time
from pathlib import Path

from .terminal_handoff import SOCKET_ENV, TOKEN_ENV
from .terminal_wallet import LOCAL_CONTROL_SHORTCUT, MAX_MESSAGE, WalletScreen, wallet_label

MAX_BACKLOG = 1024 * 1024


class Modes:
    """Track private VT modes without interpreting OSC/DCS image payloads."""
    def __init__(self):
        self.values = {7: True, 25: True}
        self.state, self.sequence = "text", bytearray()

    def feed(self, data):
        for byte in data:
            if self.state == "string":
                if byte == 7:
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
                        for mode in (7, 25, 1000, 1002, 1003, 1004, 1006, 2004, 2026))


def write_all(fd, data):
    while data:
        count = os.write(fd, data)
        data = data[count:]


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
        if game_fd is not None:
            os.close(game_fd)
        # Textual has left its alternate buffer. Restore host modes and flush only
        # output produced while hidden; never replay user keystrokes.
        write_all(1, b"\x1b[?1049l\x1b[0m" + modes.restore())
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
                wallet_connection.sendall(b'{"refresh":true}\n')
            except OSError:
                close_wallet()
        update_wallet()

    def cleanup():
        for pid in (game_pid, host_pid):
            if pid:
                try:
                    if os.waitpid(pid, os.WNOHANG)[0] == 0:
                        os.killpg(pid, signal.SIGTERM)
                except (OSError, ChildProcessError):
                    pass
        if game_fd is not None:
            write_all(1, b"\x1b[?1049l\x1b[?25h\x1b[0m")
        if wallet.active:
            write_all(1, wallet.deactivate())
        close_wallet()
        termios.tcsetattr(0, termios.TCSADRAIN, original)
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
            os.execvpe(shell[0], shell, shell_env)
        resize()
        tty.setraw(0)
        while True:
            if keys.pending and time.monotonic() - key_time > keys.wait_seconds:
                write_all(game_fd if game_fd is not None else host_fd, keys.flush())
            if wallet_resized:
                update_wallet()
            # Pets/progress can keep drawing while their host is hidden. Apply
            # PTY backpressure at the buffer limit, never kill a human game or
            # discard part of an escape/image sequence to make room.
            host_ready = game_fd is None or len(backlog) < MAX_BACKLOG
            readable, _, _ = select.select([0, listener, *([host_fd] if host_ready else []),
                                           *([game_fd] if game_fd is not None else []),
                                           *([wallet_connection] if wallet_connection is not None else [])], [], [], .1)
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
                        valid = (type(request) is dict and request.get("kind") in {"game", "status", "wallet"}
                                 and isinstance(request.get("token"), str) and hmac.compare_digest(request["token"], token))
                        if not valid:
                            candidate.close()
                            continue
                        if request['kind'] == 'wallet':
                            if wallet_connection is not None or request.get('billing') not in {'host', 'v'}:
                                candidate.close()
                                continue
                            wallet_connection = candidate
                            candidate.settimeout(None)
                            wallet.payload = {'state': 'loading', 'billing': request['billing']}
                            update_wallet()
                            candidate.sendall(json.dumps({'wallet': 1,
                                'controls_shortcut': LOCAL_CONTROL_SHORTCUT}).encode() + b'\n')
                            continue
                        if request['kind'] == 'status':
                            candidate.sendall(b'{"ready":true}\n')
                            candidate.close()
                            continue
                        if game_fd is not None or modes.alternate:
                            candidate.sendall(b'{"ready":false,"message":"Terminal is already in a full-screen view or game. Return to the normal coding prompt first; no wager placed."}\n')
                            candidate.close()
                            continue
                        candidate.sendall(b'{"ready":true}\n')
                        candidate.settimeout(None)
                        connection = candidate
                        # Stop host mouse/paste modes only on the owned outer terminal.
                        write_all(1, b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2026l")
                        if wallet.active:
                            write_all(1, b'\x1b[r')
                        game_pid, game_fd = pty.fork()
                        if game_pid == 0:
                            listener.close()
                            os.close(host_fd)
                            try:
                                from .terminal_handoff import exec_renderer
                                exec_renderer(connection)
                            except Exception as exc:
                                # Never hide startup failures behind a successful
                                # exit or print exception payloads/credentials.
                                os.write(2, ('\r\nOpenVegas renderer startup failed (' +
                                             type(exc).__name__ + ').\r\n').encode())
                                os._exit(1)
                        resize()
                    except (OSError, ValueError):
                        candidate.close()
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
                                if (type(payload) is not dict or payload.get('state') not in
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
                    data = os.read(0, 65536)
                    if not data:
                        return
                    data, menu = keys.feed(data, game_fd is None and not modes.alternate
                                           and wallet_connection is not None
                                           and wallet.payload.get('controls') is True)
                    key_time = time.monotonic()
                    if menu:
                        try:
                            wallet_connection.sendall(b'{"control":"menu"}\n')
                        except OSError:
                            close_wallet()
                    write_all(game_fd if game_fd is not None else host_fd, data)
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
                        os.waitpid(game_pid, 0)
                        restore_game()
            if game_pid is not None:
                exited, _ = os.waitpid(game_pid, os.WNOHANG)
                if exited:
                    # Drain final screen-restoration bytes before reporting done.
                    try:
                        while data := os.read(game_fd, 65536):
                            write_all(1, data)
                    except OSError:
                        pass
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
    run(shell or ["/bin/zsh", "-l"])


if __name__ == "__main__":
    main()
