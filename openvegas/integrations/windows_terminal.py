"""Same-console Windows transport. Never read, buffer or replay native UI input."""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import hmac
import os
import re
import secrets
import signal
import socket
import tempfile
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path

from .credentials import IntegrationError

ACTIONS = ('status', 'host', 'v')
RESULTS = ('host', 'v', 'refused', 'held')
BAD = 'Native terminal control not confirmed. Check status; no automatic retry.'


def _require(ok):
    if not ok:
        raise IntegrationError(BAD)


@contextmanager
def console_modes():
    """Require actual console handles, including a ConPTY-backed Windows Terminal."""
    import msvcrt
    from ctypes import wintypes as W

    api = ctypes.WinDLL('kernel32.dll', use_last_error=True, winmode=0x800)
    api.GetConsoleMode.argtypes = (W.HANDLE, ctypes.POINTER(W.DWORD))
    api.SetConsoleMode.argtypes = (W.HANDLE, W.DWORD)
    api.GetConsoleCP.restype = api.GetConsoleOutputCP.restype = W.UINT
    api.SetConsoleCP.argtypes = api.SetConsoleOutputCP.argtypes = (W.UINT,)
    saved = {}
    for fd in (0, 1, 2):
        handle = msvcrt.get_osfhandle(fd)
        mode = W.DWORD()
        if not api.GetConsoleMode(handle, ctypes.byref(mode)):
            raise IntegrationError('Native Windows host needs an attached console on all three streams.')
        saved[handle] = mode.value
    cp, out_cp = api.GetConsoleCP(), api.GetConsoleOutputCP()
    if not cp or not out_cp:
        raise IntegrationError('Windows console code pages could not be captured.')
    # A cooked console broadcasts Ctrl+C. A Python handler is not inherited by
    # the child, unlike SetConsoleCtrlHandler(NULL, TRUE). The TUI handles it.
    original = signal.signal(signal.SIGINT, lambda *_: None)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, original)
        failed = [h for h, m in saved.items() if not api.SetConsoleMode(h, m)]
        input_ok, output_ok = api.SetConsoleCP(cp), api.SetConsoleOutputCP(out_cp)
        if failed or not input_ok or not output_ok:
            raise IntegrationError('Windows console mode restoration failed.')


def _directory():
    from openvegas.emotes._windows_state import private_windows_directory
    _require(os.name == 'nt')
    return private_windows_directory(Path(tempfile.gettempdir()) / 'OpenVegas-Terminal-v1')


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(rf'[0-9a-f]{{{length}}}', value) is not None


def _mac(key, *parts):
    return hmac.new(key, ' '.join(('ov-control-v1', *parts)).encode('ascii'), hashlib.sha256).hexdigest()


async def _line(reader):
    raw = await reader.readline()
    _require(0 < len(raw) <= 256 and raw.endswith(b'\n'))
    return raw[:-1].decode('ascii')


async def _send(writer, line):
    writer.write(line.encode('ascii') + b'\n')
    await writer.drain()


@asynccontextmanager
async def controls(command, *, directory):
    """Discovery capability lives only in a protected DACL file, never argv/env.

    Both ends authenticate fresh nonces, the action and the result. A stale
    locator, reflected proof or port rebind cannot authorize a command.
    """
    sid, key = secrets.token_hex(16), secrets.token_bytes(32)
    active = set()
    closing = False

    async def handle(reader, writer):
        task = asyncio.current_task()
        if closing or len(active) >= 8:
            writer.close()
            return
        active.add(task)
        try:
            async with asyncio.timeout(6):
                client = await _line(reader)
                _require(_hex(client, 32))
                nonce = secrets.token_hex(16)
                await _send(writer, nonce + ' ' + _mac(key, 'hello', sid, client, nonce))
                action, proof = (await _line(reader)).split(' ')
                _require(action in ACTIONS and hmac.compare_digest(
                    proof, _mac(key, 'cmd', sid, client, nonce, action)))
                result = await command(action)
                _require(result in RESULTS)
                await _send(writer, result + ' ' + _mac(key, 'reply', sid, client, nonce, action, result))
        except (IntegrationError, OSError, ValueError, TimeoutError):
            pass
        finally:
            writer.close()
            active.discard(task)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == 'nt':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = await asyncio.start_server(handle, sock=sock, limit=256, backlog=8)
        async with server:
            directory.atomic_write(sid, f'{port} {key.hex()}'.encode('ascii'))
            try:
                yield sid
            finally:
                closing = True
                server.close()
                try:
                    directory.unlink(sid)
                finally:
                    for task in list(active):
                        task.cancel()
                    await asyncio.gather(*list(active), return_exceptions=True)
    finally:
        sock.close()


async def request(sid, action, *, directory=None):
    _require(_hex(sid, 32) and action in ACTIONS)
    try:
        if directory is None:
            with _directory() as private:
                return await request(sid, action, directory=private)
        raw = directory.read(sid, 80).decode('ascii')
        port, secret = raw.split(' ')
        _require(_hex(secret, 64) and re.fullmatch('[1-9][0-9]{0,4}', port) and int(port) <= 65535)
        key, client = bytes.fromhex(secret), secrets.token_hex(16)
        async with asyncio.timeout(7):
            reader, writer = await asyncio.open_connection('127.0.0.1', int(port), limit=256)
            try:
                await _send(writer, client)
                nonce, proof = (await _line(reader)).split(' ')
                _require(_hex(nonce, 32) and hmac.compare_digest(proof, _mac(key, 'hello', sid, client, nonce)))
                await _send(writer, action + ' ' + _mac(key, 'cmd', sid, client, nonce, action))
                result, proof = (await _line(reader)).split(' ')
                _require(result in RESULTS and hmac.compare_digest(
                    proof, _mac(key, 'reply', sid, client, nonce, action, result)))
                return result
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
    except (OSError, ValueError, TimeoutError):
        raise IntegrationError(BAD) from None
