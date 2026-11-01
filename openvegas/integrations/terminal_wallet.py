"""Read-only wallet monitor and one reserved row in the owned terminal relay.

This is not a Codex status-line plugin. The real Codex child receives one fewer
row; the relay alone renders the wallet, outside the child's viewport.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
import json
import os
import re
import stat
import threading
import math
from decimal import Decimal

from .broker import Broker
from .game_result import amount
from .credentials import IntegrationError
from .terminal_handoff import SOCKET_ENV, TOKEN_ENV, validate_socket

REFRESH_SECONDS = 5
REQUEST_SECONDS = 3
MAX_MESSAGE = 1024


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
        game_token = game_credentials(self.backend).load()
        if game_token:
            return game_token
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise IntegrationError('Unsafe wallet profile.')
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            return self.fallback.load()
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
        return self.fallback.load()


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
                except Exception as exc:
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
    suffix = ' / F8 OpenVegas' if payload.get('controls') is True else ''
    quota = payload.get('quota_remaining')
    low = (payload.get('billing') != 'v' and type(quota) in (int, float)
           and math.isfinite(quota) and 0 <= quota <= 10)
    if low:
        suffix = f' / Host {quota:g}% left / F8: switch to $V'
    if payload.get('local_error') is True:
        suffix = ' / local menu unavailable; F8 to retry'
    compact = f'{value} / host {quota:g}% / F8' if low else f'{value} / F8' if suffix else value
    for text in (f" OPENVEGAS / {value} / {mode}{suffix}", f" OV / {value}{suffix}", compact, f" OV / {value}", value):
        if len(text) < columns:
            return text
    return value[:max(0, columns - 1)]


async def monitor(env, billing, *, broker_factory=Broker, interval=REFRESH_SECONDS, ready=None,
                  credentials=None, account_factory=WalletCredentials, control=None, status=None):
    """Lease the strip to this native child, not to a long-lived shell or MCP.

    Closing the pipe removes the strip even after a crash. Never authorize,
    refresh a provider, open a browser, place a wager, or print server errors.
    """
    if not env.get(SOCKET_ENV) or not env.get(TOKEN_ENV):
        if ready:
            ready.set()
        return
    writer = broker = receiver = None
    credentials = credentials or CredentialReader()
    try:
        async with asyncio.timeout(REQUEST_SECONDS):
            validate_socket(env[SOCKET_ENV])
            reader, writer = await asyncio.open_unix_connection(env[SOCKET_ENV], limit=MAX_MESSAGE)
            writer.write(json.dumps({"kind": "wallet", "token": env[TOKEN_ENV],
                                     "billing": billing}).encode() + b"\n")
            await writer.drain()
            if json.loads(await reader.readline()).get("wallet") != 1:
                return  # Older relay: leave its screen and the host untouched.
        if ready:
            ready.set()
        broker = broker_factory()
        account_credentials = account_factory(broker)
        refresh, disconnected = asyncio.Event(), asyncio.Event()
        async def receive():
            try:
                while line := await reader.readline():
                    event = json.loads(line)
                    if isinstance(event, dict):
                        if control:
                            control(event)
                        if event.get('refresh') is True:
                            refresh.set()
            finally:
                disconnected.set()
                refresh.set()
        if control:
            # Input must not wait behind wallet network latency.
            receiver = asyncio.create_task(receive())
            writer.write(json.dumps({'state': 'loading', 'billing': billing,
                                     **(status() if status else {})}).encode() + b'\n')
            await writer.drain()
        while not disconnected.is_set():
            payload = {"state": "unavailable", "billing": billing}
            try:
                async with asyncio.timeout(REQUEST_SECONDS):
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
            except (TimeoutError, OSError, ValueError, TypeError, AttributeError):
                pass
            if status:
                payload.update(status())
            writer.write(json.dumps(payload).encode() + b"\n")
            await writer.drain()
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
        if receiver:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        if broker:
            await broker.close()


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
        return b"\x1b7\x1b[r" + f"\x1b[{self.rows};1H\x1b[2K".encode() + b"\x1b8"

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
        if final == b"s": self.saved = True
        if final == b"u": self.saved = False
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
