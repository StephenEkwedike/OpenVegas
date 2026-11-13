"""Read-only wallet monitor and one reserved row in the owned terminal relay.

This is not a Codex status-line plugin. The real Codex child receives one fewer
row; the relay alone renders the wallet, outside the child's viewport.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import stat
import threading
from concurrent.futures import Future
from contextlib import suppress

from .broker import Broker
from .credentials import IntegrationError
from .game_result import amount
from .session_ownership import fence_fields
from .terminal_handoff import SOCKET_ENV, TOKEN_ENV, validate_socket

REFRESH_SECONDS = 5
REQUEST_SECONDS = 3
CLEANUP_SECONDS = .25
MAX_MESSAGE = 1024
LOCAL_CONTROL_SHORTCUT = 'ctrl-bracket-o'
LOCAL_CONTROL_LABEL = 'Ctrl+] then o'


def terminal_reset(*, clear=False, alternate=True):
    """Reset presentation modes, not scrollback, after the previous writer stops."""
    # CAN/ST also recover a renderer killed midway through a CSI/OSC/DCS packet.
    # Kitty's flags/stacks are per screen. Set current flags to zero rather than
    # blindly popping a caller's stack; also disable xterm modifyOtherKeys.
    # https://sw.kovidgoyal.net/kitty/keyboard-protocol/#progressive-enhancement
    # https://invisible-island.net/xterm/ctlseqs/ctlseqs.html (XTMODKEYS)
    keyboard = b'\x1b[=0u\x1b[>4;0m'
    data = b'\x18\x1b\\\x1b[?2026l' + keyboard
    if alternate:
        data += b'\x1b[?1049l\x1b[?1047l\x1b[?47l' + keyboard
    data += (b'\x1b[?6l\x1b[r\x1b[0m\x1b(B\x1b>\x1b[4l\x1b[?1l'
            b'\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1004l\x1b[?1005l'
            b'\x1b[?1006l\x1b[?1015l\x1b[?2004l\x1b[?7h\x1b[?25h\x1b[0 q')
    return data + (b'\x1b[2J\x1b[H' if clear else b'')


async def handoff_screen(env, *, output_fd=1):
    """Barrier before a blocking supervisor prompt, after child/task cleanup.

    The caller must stop/reap its native writers and cancel/await its wallet
    monitor first. A managed relay must acknowledge the reset; silently falling
    back would race queued native output and the relay's reserved wallet row.
    """
    if not env.get(SOCKET_ENV) or not env.get(TOKEN_ENV):
        if os.isatty(output_fd):
            data = terminal_reset(clear=True)
            while data:
                data = data[os.write(output_fd, data):]
        return
    writer = None
    try:
        async with asyncio.timeout(REQUEST_SECONDS):
            validate_socket(env[SOCKET_ENV])
            reader, writer = await asyncio.open_unix_connection(env[SOCKET_ENV], limit=MAX_MESSAGE)
            writer.write(json.dumps({'kind': 'handoff', 'token': env[TOKEN_ENV],
                                     **fence_fields(env)}).encode() + b'\n')
            await writer.drain()
            reply = json.loads(await reader.readline())
            if not isinstance(reply, dict) or reply.get('reset') is not True:
                raise ValueError('Handoff was not acknowledged')
    except (OSError, ValueError, TimeoutError, IntegrationError):
        raise IntegrationError('Terminal handoff unavailable. Restart the managed terminal; '
                               'no billing prompt or new host was started.') from None
    finally:
        if writer is not None:
            writer.close()
            with suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), CLEANUP_SECONDS)


class WalletCredentials:
    """Read the game's profile, never the supervisor's native-host HOME.

    The HUD must not enroll, refresh credentials or prompt for Keychain access.
    A browser game/login refresh is picked up on the next poll. A game login
    takes precedence over a CLI login; both precede a cosmetic-only account.
    """
    def __init__(self, broker):
        self.fallback = broker.credentials
        self.backend = broker.backend
        self.identity = (self.fallback.identity, self.backend, 'wallet-account')
        self.path = self.fallback.path.parent.parent / 'config.json'

    def load(self):
        from .game_browser import game_credentials
        game_token = game_credentials(self.backend).load_noninteractive()
        if game_token:
            return game_token
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise IntegrationError('Unsafe wallet profile.')
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            return self.fallback.load_noninteractive()
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024
                    or (os.name == 'posix' and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
                raise IntegrationError('Unsafe wallet profile.')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise IntegrationError('Unsafe wallet profile.')
            config = json.loads(raw)
        finally:
            os.close(fd)
        if not isinstance(config, dict):
            raise IntegrationError('Invalid wallet profile.')
        if str(config.get('backend_url', '')).rstrip('/') != self.backend.rstrip('/'):
            raise IntegrationError('Wallet profile belongs to another backend.')
        session = config.get('session') or {}
        if not isinstance(session, dict):
            raise IntegrationError('Invalid wallet session.')
        token = session.get('access_token')
        if token and isinstance(token, str):
            return token
        # Do not show a different integration account after a CLI logout/expiry.
        if 'session' in config:
            return None
        return self.fallback.load_noninteractive()


class CredentialReader:
    """At most one keychain read per supervisor, without blocking process exit."""
    def __init__(self):
        self.pending = None
        self.identity = None

    async def load(self, credentials):
        identity = getattr(credentials, 'identity', id(credentials))
        if self.pending is not None and self.identity != identity:
            if not self.pending.done():
                raise IntegrationError('Wallet credential read unavailable.')
            self.pending = None
        if self.pending is None:
            self.identity = identity
            future = self.pending = Future()
            def read():
                try:
                    future.set_result(credentials.load())
                except Exception as exc:  # noqa: BLE001 - Deliver adapter failures through the future.
                    future.set_exception(exc)
            # asyncio's default executor waits for keychain threads at shutdown.
            # A daemon is read-only, reused after timeout, and never joined.
            threading.Thread(target=read, name='ov-wallet-credentials', daemon=True).start()
        future = self.pending
        while not future.done():
            await asyncio.sleep(.025)
        try:
            return future.result()
        finally:
            self.pending = None


def wallet_value(value):
    # Strict text grammar prevents terminal-control injection and huge decimals.
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,15}(?:\.\d{1,6})?", value):
        raise ValueError("Invalid wallet balance")
    return amount(value)[3:]


def wallet_label(payload, columns):
    state = payload.get("state")
    if state == "ready":
        value = '$V ' + wallet_value(payload.get("balance"))
    else:
        value = {"loading": "wallet loading", "login": "sign in to OpenVegas",
                 "unavailable": "wallet unavailable"}.get(state, "wallet unavailable")
    mode = "$V billing" if payload.get("billing") == "v" else "host billing"
    switch = payload.get('switch_status')
    if switch in ('pending', 'timed_out', 'blocked', 'refreshing'):
        notice = {'pending': 'Switching... payer not confirmed',
                  'refreshing': 'Billing confirmed; updating display'}.get(
                      switch, 'Switch not completed; input held')
        for text in (f' OPENVEGAS / {value} / {notice}', notice):
            if len(text) < columns:
                return text
        return notice[:max(0, columns - 1)]
    target = payload.get('switch_on_exit')
    if target in ('v', 'host') and target != payload.get('billing') and payload.get('billing') in ('host', 'v'):
        current = '$V' if payload['billing'] == 'v' else 'host'
        destination = '$V' if target == 'v' else 'host'
        notice = f'/exit to switch to {destination}'
        for text in (f' OPENVEGAS / {value} / {mode} / {notice}',
                     f' {current} billing / {notice}', notice):
            if len(text) < columns:
                return text
        return notice[:max(0, columns - 1)]
    controls = (payload.get('controls') is True
                and payload.get('controls_shortcut') == LOCAL_CONTROL_SHORTCUT)
    suffix = f' / {LOCAL_CONTROL_LABEL}: OpenVegas' if controls else ''
    quota = payload.get('quota_remaining')
    low = (payload.get('billing') != 'v' and type(quota) in (int, float)
           and math.isfinite(quota) and 0 <= quota <= 10)
    if low:
        suffix = f' / Host {quota:g}% left'
        if controls:
            suffix += f' / {LOCAL_CONTROL_LABEL}: play or switch to $V'
    if payload.get('local_error') is True and controls:
        suffix = f' / local menu unavailable; {LOCAL_CONTROL_LABEL} to retry'
    if payload.get('relay_restart_required') is True:
        suffix += ' / restart terminal for local menu'
    compact = value + (f' / host {quota:g}%' if low else '')
    if controls:
        compact += f' / {LOCAL_CONTROL_LABEL}'
    recovery = (f'Host 0% / {LOCAL_CONTROL_LABEL}: menu', f'{LOCAL_CONTROL_LABEL}: menu',
                LOCAL_CONTROL_LABEL) if low and quota == 0 and controls else ()
    for text in (f" OPENVEGAS / {value} / {mode}{suffix}", f" OV / {value}{suffix}", compact,
                 *recovery, f" OV / {value}", value):
        if len(text) < columns:
            return text
    return value[:max(0, columns - 1)]


async def monitor(env, billing, *, broker_factory=Broker, interval=REFRESH_SECONDS, ready=None,
                  credentials=None, account_factory=WalletCredentials, control=None, status=None, status_changed=None):
    """Lease the strip to this native child, not to a long-lived shell or MCP.

    Closing the pipe removes the strip even after a crash. Never authorize,
    refresh a provider, open a browser, place a wager, or print server errors.
    The ready event signals startup completion, not wallet login or balance.
    """
    if not env.get(SOCKET_ENV) or not env.get(TOKEN_ENV):
        if ready:
            ready.set()
        return
    writer = broker = receiver = notifier = account_credentials = None
    credentials = credentials or CredentialReader()
    fields = fence_fields(env)
    try:
        async with asyncio.timeout(REQUEST_SECONDS):
            validate_socket(env[SOCKET_ENV])
            reader, writer = await asyncio.open_unix_connection(env[SOCKET_ENV], limit=MAX_MESSAGE)
            writer.write(json.dumps({"kind": "wallet", "token": env[TOKEN_ENV],
                                     "billing": billing, **fields}).encode() + b"\n")
            await writer.drain()
            handshake = json.loads(await reader.readline())
            if not isinstance(handshake, dict) or handshake.get("wallet") != 1:
                return  # Older relay: leave its screen and the host untouched.
            if any(handshake.get(key) != value for key, value in fields.items()):
                return
            controls_supported = handshake.get('controls_shortcut') == LOCAL_CONTROL_SHORTCUT
        if ready:
            ready.set()
        def display_status():
            payload = dict(status() if status else {})
            payload['controls'] = (control is not None and controls_supported
                                   and payload.get('controls', True) is True)
            payload['controls_shortcut'] = LOCAL_CONTROL_SHORTCUT if payload['controls'] else None
            payload['relay_restart_required'] = control is not None and not controls_supported
            if not controls_supported:
                # Old relays label these fields with F8 even when controls=False.
                payload.pop('quota_remaining', None)
                payload.pop('local_error', None)
            return payload
        cached = {'state': 'loading', 'billing': billing}
        sending = asyncio.Lock()
        async def publish():
            async with sending, asyncio.timeout(REQUEST_SECONDS):
                writer.write(json.dumps({**cached, **display_status(), **fields}).encode() + b'\n')
                await writer.drain()
        refresh, disconnected = asyncio.Event(), asyncio.Event()
        async def push_status():
            try:
                while not disconnected.is_set():
                    await status_changed.wait()
                    status_changed.clear()
                    await publish()
            except (OSError, ValueError, TimeoutError):
                disconnected.set()
                refresh.set()
        async def receive():
            try:
                while line := await reader.readline():
                    event = json.loads(line)
                    if isinstance(event, dict) and all(event.get(key) == value for key, value in fields.items()):
                        if control:
                            control(event)
                        if event.get('refresh') is True:
                            refresh.set()
            finally:
                disconnected.set()
                refresh.set()
        if control:
            # The private relay lease, not wallet initialization or balance,
            # enables input. Keep one receiver even while account reads fail.
            receiver = asyncio.create_task(receive())
            await publish()
            if status_changed is not None and fields:
                notifier = asyncio.create_task(push_status())
        while not disconnected.is_set():
            payload = {"state": "unavailable", "billing": billing}
            try:
                async with asyncio.timeout(REQUEST_SECONDS):
                    if broker is None:
                        broker = broker_factory()
                    if account_credentials is None:
                        account_credentials = account_factory(broker)
                    # Keychain I/O must not block the native supervisor's loop.
                    token = await credentials.load(account_credentials)
                    if not token:
                        payload["state"] = "login"
                    else:
                        account = await broker.request("GET", "/account", token=token)
                        wallet_value(account.get("balance"))
                        payload.update(state="ready", balance=account["balance"])
            except IntegrationError as exc:
                payload["state"] = "login" if exc.status in (401, 403) else "unavailable"
            except Exception:  # noqa: BLE001,S110 - Never log private wallet adapter failures.
                # Optional wallet adapters may fail during construction too.
                # Never drop the control lease or expose account error text.
                pass
            if disconnected.is_set():
                break
            cached = payload
            await publish()
            # The relay sends a refresh after a game closes. Otherwise poll at
            # a bounded cadence, never in the host's model/tool execution loop.
            try:
                async with asyncio.timeout(interval):
                    if receiver:
                        await refresh.wait()
                        refresh.clear()
                        continue
                    line = await reader.readline()
                    if not line:
                        return
                    event = json.loads(line)
                    if control and isinstance(event, dict):
                        control(event)
            except TimeoutError:
                pass
    except (OSError, ValueError, TimeoutError, IntegrationError):
        pass  # Optional display failures must not interrupt native coding.
    finally:
        if ready:
            ready.set()
        # Release the display lease synchronously, before any cancellable wait.
        # A second cancellation must not leave the HUD alive over a consent prompt.
        if writer:
            with suppress(Exception):
                writer.close()
        if receiver:
            receiver.cancel()
        if notifier:
            notifier.cancel()
        async def cleanup():
            await asyncio.gather(*(task for task in (receiver, notifier) if task is not None), return_exceptions=True)
            if writer:
                with suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), CLEANUP_SECONDS)
            if broker:
                with suppress(Exception):
                    await asyncio.wait_for(broker.close(), CLEANUP_SECONDS)
        closing = asyncio.create_task(cleanup())
        cancelled = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                cancelled = True
        closing.result()
        if cancelled:
            raise asyncio.CancelledError


class WalletScreen:
    """Bound VT viewport controls; pass all text and graphics bytes unchanged.

    Never draw inside a partial UTF-8/control/OSC/DCS sequence, synchronized
    update, or an application's saved-cursor pair (including pet frames).
    """
    def __init__(self):
        self.active = False
        self.rows, self.columns = 24, 80
        self.payload = {"state": "loading", "billing": "host"}
        self.state, self.sequence = "text", bytearray()
        self.utf8 = 0
        self.saved = False
        self.sync = False
        self.wrap = True
        self.origin = False
        self.string_bel = False
        self.top, self.bottom = 1, None

    @property
    def child_rows(self):
        return max(1, self.rows - int(self.active))

    @property
    def safe(self):
        return self.state == "text" and not self.utf8 and not self.saved and not self.sync

    def margins(self):
        bottom = min(self.bottom or self.child_rows, self.child_rows)
        top = min(self.top, max(1, bottom - 1))
        return f"\x1b[{top};{bottom}r".encode() if self.active else b"\x1b[r"

    def activate(self):
        self.active = True
        # Start a newly launched child inside its own viewport, even when the
        # invoking shell's prompt was at the physical bottom of the terminal.
        return self.margins() + f"\x1b[{self.child_rows};1H".encode()

    def deactivate(self):
        self.active = False
        return b"\x1b7\x1b[?6l\x1b[r" + f"\x1b[{self.rows};1H\x1b[2K".encode() + b"\x1b8"

    def paint(self):
        if not self.active or not self.safe:
            return b""
        label = wallet_label(self.payload, self.columns)
        return (b"\x1b7\x1b[?6l\x1b[?7l" + f"\x1b[{self.rows};1H\x1b[0m\x1b[2K\x1b[37m".encode()
                + label.encode() + b"\x1b8" + (b"\x1b[?7h" if self.wrap else b"\x1b[?7l"))

    def _csi(self, raw):
        body, final = raw[2:-1], raw[-1:]
        if final in (b"h", b"l") and body.startswith(b"?"):
            for mode in body[1:].split(b";"):
                enabled = final == b"h"
                if mode == b"2026": self.sync = enabled
                if mode == b"7": self.wrap = enabled
                if mode == b"6": self.origin = enabled
        if final == b"s" and body in (b"", b"0"): self.saved = True
        if final == b"u" and body in (b"", b"0"): self.saved = False
        if self.active and re.fullmatch(rb"[0-9;]*", body):
            parts = body.split(b";")
            if final == b"r":
                top = max(1, min(int(parts[0] or 1), self.child_rows))
                bottom = min(int(parts[1] or self.child_rows) if len(parts) > 1 else self.child_rows, self.child_rows)
                self.top, self.bottom = top, bottom if bottom < self.child_rows else None
                return f"\x1b[{top};{max(top, bottom)}r".encode()
            if final in (b"H", b"f", b"d"):
                parts[0] = str(min(max(1, int(parts[0] or 1)), self.child_rows)).encode()
                return b"\x1b[" + b";".join(parts) + final
        return raw

    def feed(self, data):
        output = bytearray()
        for byte in data:
            if byte in (24, 26):
                self.state, self.utf8 = "text", 0
                self.sequence.clear()
                output.append(byte)
                continue
            if byte == 27 and self.state in ("escape", "csi", "csi_pass", "escape_intermediate"):
                self.sequence.clear()
                self.state = "escape"
                continue
            if self.state == "csi":
                self.sequence.append(byte)
                if 64 <= byte <= 126:
                    output += self._csi(bytes(self.sequence))
                    self.sequence.clear()
                    self.state = "text"
                elif len(self.sequence) > 256:
                    output += self.sequence
                    self.sequence.clear()
                    self.state = "csi_pass"
                continue
            if self.state == "escape":
                if byte == 91:
                    self.sequence = bytearray(b"\x1b[")
                    self.state = "csi"
                    continue
                output += bytes((27, byte))
                if byte in (93, 80, 95, 94, 88):
                    self.state = "string"
                    self.string_bel = byte == 93
                elif 32 <= byte <= 47:
                    self.state = "escape_intermediate"
                else:
                    self.state = "text"
                    if byte == 55: self.saved = True
                    if byte == 56: self.saved = False
                continue
            output.append(byte)
            if self.state == "string":
                if byte == 7 and self.string_bel: self.state = "text"
                elif byte == 27: self.state = "string_escape"
            elif self.state == "string_escape":
                self.state = "text" if byte == 92 else "string"
            elif self.state == "csi_pass":
                if 64 <= byte <= 126: self.state = "text"
            elif self.state == "escape_intermediate":
                if 48 <= byte <= 126: self.state = "text"
            elif byte == 27:
                output.pop()
                self.state = "escape"
            elif byte >= 192:
                self.utf8 = 1 if byte < 224 else 2 if byte < 240 else 3
            elif 128 <= byte < 192 and self.utf8:
                self.utf8 -= 1
            else:
                self.utf8 = 0
        return bytes(output)
