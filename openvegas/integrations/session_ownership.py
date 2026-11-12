"""Cross-process, current-user leases for native session identities.

Lock files are never unlinked on release: unlinking a locked inode would allow
two owners on different inodes. Kernel flock releases the lease after a crash.
Only a monotonic fencing generation is persisted, never auth or conversation.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path

from .credentials import IntegrationError


class SessionLease:
    def __init__(self, host, identifier, *, directory=None):
        from .billing_host import session_id
        if host not in {'codex', 'claude'} or not session_id(identifier):
            raise IntegrationError('Invalid native ownership identity.')
        self.fd = None
        self.generation = None
        root = Path(directory) if directory is not None else Path('/tmp') / f'ov-session-owner-{os.getuid()}'
        try:
            root.mkdir(mode=0o700, exist_ok=True)
            parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                info = os.fstat(parent)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise IntegrationError('Native ownership directory is not private.')
                name = hashlib.sha256((host + ':' + identifier).encode()).hexdigest() + '.lock'
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            finally:
                os.close(parent)
            self.fd = fd
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise IntegrationError('Native ownership file is not private.')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise IntegrationError('Another OpenVegas runtime owns this native session; no handoff started.') from None
            raw = os.read(fd, 129)
            value = json.loads(raw) if raw else {'generation': 0}
            generation = value.get('generation') if isinstance(value, dict) else None
            if len(raw) > 128 or type(generation) is not int or not 0 <= generation < 2**63 - 1:
                raise IntegrationError('Invalid native ownership generation; no handoff started.')
            self.generation = generation + 1
            os.lseek(fd, 0, os.SEEK_SET)
            data = json.dumps({'generation': self.generation}).encode()
            written = os.write(fd, data)
            if written != len(data):
                raise IntegrationError('Native ownership record could not be persisted.')
            os.ftruncate(fd, len(data))
            os.fsync(fd)
        except BaseException as exc:
            self.close()
            if isinstance(exc, (OSError, ValueError)):
                raise IntegrationError('Native session ownership unavailable; no handoff started.') from None
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()


OWNER_ENV = 'OPENVEGAS_TERMINAL_OWNER'
GENERATION_ENV = 'OPENVEGAS_TERMINAL_GENERATION'


def fence_fields(env):
    owner, generation = env.get(OWNER_ENV), env.get(GENERATION_ENV)
    if owner is None and generation is None:
        return {}
    if (not isinstance(owner, str) or len(owner) != 32
            or any(c not in '0123456789abcdef' for c in owner)
            or not isinstance(generation, str) or not generation.isascii() or not generation.isdecimal()
            or not 1 <= int(generation) < 2**63):
        raise IntegrationError('Invalid terminal ownership generation.')
    return {'owner': owner, 'generation': int(generation)}


class RelayFence:
    """Authenticated request ordering; not a substitute for OS session flock."""
    def __init__(self):
        self.owner = None
        self.generation = None
        self.seen = {}
        self.modern = False
        self.consumed = False

    def claim(self, request):
        owner, generation = request.get('owner'), request.get('generation')
        if (not isinstance(owner, str) or len(owner) != 32
                or any(c not in '0123456789abcdef' for c in owner)
                or type(generation) is not int or not 1 <= generation < 2**63
                or generation <= self.seen.get(owner, 0) or self.owner is not None
                or (owner not in self.seen and len(self.seen) >= 4096)):
            return False
        self.seen[owner] = generation
        self.owner, self.generation = owner, generation
        self.modern, self.consumed = True, False
        return True

    def matches(self, request):
        if not self.modern or (self.owner is None and not self.consumed):
            # The invoking shell has no child-generation environment. Preserve
            # its ordinary game/status flow after the owned child exits, while
            # rejecting any old generation-tagged child message.
            return 'owner' not in request and 'generation' not in request
        return (not self.consumed and self.owner is not None
                and request.get('owner') == self.owner
                and type(request.get('generation')) is int
                and request['generation'] == self.generation)

    def release(self, request):
        if not self.matches(request):
            return False
        self.owner = self.generation = None
        self.consumed = False
        return True

    def consume_handoff(self):
        if self.modern:
            self.consumed = True

    def fields(self):
        return {} if self.owner is None else {'owner': self.owner, 'generation': self.generation}

    def disconnect(self):
        self.owner = self.generation = None
        self.consumed = False


async def claim_relay(env, *, allow_legacy=False):
    """Negotiate without mutation; only an authenticated legacy status may fall back.

    Keep a modern ownership connection open for the child lifetime. No timeout,
    malformed reply, failed claim or unknown protocol may downgrade to legacy.
    """
    import asyncio

    from .terminal_handoff import SOCKET_ENV, TOKEN_ENV, close_writer, validate_socket
    if not env.get(SOCKET_ENV) or not env.get(TOKEN_ENV):
        return None
    writer = None
    try:
        async with asyncio.timeout(3):
            validate_socket(env[SOCKET_ENV])
            fields = fence_fields(env)
            reader, writer = await asyncio.open_unix_connection(env[SOCKET_ENV], limit=4096)
            writer.write(json.dumps({'kind': 'status', 'token': env[TOKEN_ENV]}).encode() + b'\n')
            await writer.drain()
            reply = json.loads(await reader.readline())
            if not isinstance(reply, dict) or reply.get('ready') is not True:
                raise ValueError
            await close_writer(writer)
            writer = None
            if 'ownership_protocol' not in reply:
                if not allow_legacy:
                    raise ValueError
                return None
            if type(reply['ownership_protocol']) is not int or reply['ownership_protocol'] != 1:
                raise ValueError
            reader, writer = await asyncio.open_unix_connection(env[SOCKET_ENV], limit=4096)
            writer.write(json.dumps({'kind': 'owner', 'token': env[TOKEN_ENV], **fields}).encode() + b'\n')
            await writer.drain()
            reply = json.loads(await reader.readline())
            if (not fields or not isinstance(reply, dict) or reply.get('ready') is not True
                    or type(reply.get('generation')) is not int
                    or any(reply.get(key) != value for key, value in fields.items())):
                raise ValueError
            return writer
    except (OSError, ValueError, TimeoutError, IntegrationError):
        if writer is not None:
            await close_writer(writer)
        raise IntegrationError('Managed terminal handshake unavailable or incompatible. Restart the managed '
                               'terminal; no native child or billing switch was started.') from None
    except BaseException:
        if writer is not None:
            await close_writer(writer)
        raise


async def supports_relay_input(env):
    """Read-only capability check before starting a retained native PTY."""
    import asyncio

    from .terminal_handoff import SOCKET_ENV, TOKEN_ENV, close_writer, validate_socket
    writer = None
    try:
        async with asyncio.timeout(3):
            validate_socket(env[SOCKET_ENV])
            fields = fence_fields(env)
            if not fields:
                return False
            reader, writer = await asyncio.open_unix_connection(env[SOCKET_ENV], limit=4096)
            writer.write(json.dumps({'kind': 'status', 'token': env[TOKEN_ENV], **fields}).encode() + b'\n')
            await writer.drain()
            reply = json.loads(await reader.readline())
            return (isinstance(reply, dict) and reply.get('ready') is True
                    and type(reply.get('input_protocol')) is int and reply['input_protocol'] == 1)
    except (OSError, ValueError, KeyError, TimeoutError, IntegrationError):
        return False
    finally:
        if writer is not None:
            await close_writer(writer)


async def set_relay_input(env, *, hold, nonce):
    """Hold future keystrokes in the terminal's input queue, never replay old input."""
    import asyncio
    import re

    from .terminal_handoff import SOCKET_ENV, TOKEN_ENV, close_writer, validate_socket
    fields = fence_fields(env)
    if (not fields or type(hold) is not bool or not isinstance(nonce, str)
            or re.fullmatch(r'[a-f0-9]{32}', nonce) is None):
        raise IntegrationError('Invalid native input fence.')
    writer = None
    try:
        async with asyncio.timeout(3):
            validate_socket(env[SOCKET_ENV])
            reader, writer = await asyncio.open_unix_connection(env[SOCKET_ENV], limit=4096)
            writer.write(json.dumps({'kind': 'input', 'token': env[TOKEN_ENV],
                **fields, 'hold': hold, 'nonce': nonce}).encode() + b'\n')
            await writer.drain()
            reply = json.loads(await reader.readline())
            if reply != {'ready': True, 'held': hold, 'nonce': nonce, **fields}:
                raise ValueError('Unacknowledged input fence')
    except (OSError, ValueError, KeyError, TimeoutError):
        raise IntegrationError('Terminal input fence was not acknowledged; no payer commit.') from None
    finally:
        if writer is not None:
            await close_writer(writer)
