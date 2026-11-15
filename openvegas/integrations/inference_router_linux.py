"""Linux loopback ownership proofs for the retained native Codex child only."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import socket
import stat
import sys
from time import monotonic

from .credentials import IntegrationError
from .native_owned_process import OwnedProcess

_PROC = Path('/proc')
_MAX_TABLE = 1024 * 1024
_MAX_FDS = 4096


def _read(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('Invalid process inventory file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(maximum + 1)
        if not data or len(data) > maximum or not data.endswith(b'\n'):
            raise OSError('Incomplete process inventory')
        return data
    finally:
        os.close(fd)


def _number(value, *, maximum=2**64 - 1):
    if not value.isdigit() or len(value) > 20 or int(value) > maximum:
        raise ValueError('Invalid process inventory number')
    return int(value)


def _identity(pid):
    if type(pid) is not int or not 1 < pid < 2**31:
        raise ValueError('Invalid native PID')
    directory = _PROC / str(pid)
    before = directory.lstat()
    if not stat.S_ISDIR(before.st_mode) or before.st_uid != os.geteuid():
        raise OSError('Native process owner unavailable')
    raw = _read(directory / 'stat', 4096)
    # Like codex_live_transport's Linux birth token, field 22 follows comm.
    # comm may itself contain spaces or parentheses; never retain its value.
    if not raw.startswith(f'{pid} ('.encode()) or b') ' not in raw:
        raise ValueError('Invalid native process stat')
    fields = raw.rsplit(b') ', 1)[1].split()
    if len(fields) < 20 or fields[0] not in (b'R', b'S', b'D', b'T', b't', b'I'):
        raise ValueError('Native process is not live')
    parent, group, birth = (_number(fields[index]) for index in (1, 2, 19))
    owners = [line.split()[1:] for line in _read(directory / 'status', 65536).splitlines()
              if line.startswith(b'Uid:')]
    if len(owners) != 1 or len(owners[0]) != 4:
        raise ValueError('Native process UID unavailable')
    if any(_number(value, maximum=2**32 - 1) != os.geteuid() for value in owners[0]) or not birth:
        raise ValueError('Native process identity differs')
    network = (directory / 'ns/net').stat()
    local_network = (_PROC / 'self/ns/net').stat()
    after = directory.lstat()
    if ((before.st_dev, before.st_ino, before.st_uid, before.st_mode)
            != (after.st_dev, after.st_ino, after.st_uid, after.st_mode)
            or (network.st_dev, network.st_ino) != (local_network.st_dev, local_network.st_ino)):
        raise OSError('Native process namespace changed')
    return pid, parent, group, before.st_uid, birth, network.st_dev, network.st_ino


def _connection_inode(client_port, server_port, uid):
    address = f'{int.from_bytes(socket.inet_aton("127.0.0.1"), sys.byteorder):08X}'
    local, remote = (f'{address}:{port:04X}'.encode() for port in (client_port, server_port))
    rows = _read(_PROC / 'self/net/tcp', _MAX_TABLE).splitlines()
    if rows[0].split()[:4] != [b'sl', b'local_address', b'rem_address', b'st']:
        raise ValueError('Unsupported TCP inventory')
    matches = []
    for row in rows[1:]:
        fields = row.split()
        if (len(fields) < 10 or not re.fullmatch(rb'[0-9]+:', fields[0])
                or any(not re.fullmatch(rb'[0-9A-F]{8}:[0-9A-F]{4}', item) for item in fields[1:3])
                or not re.fullmatch(rb'[0-9A-F]{2}', fields[3])):
            raise ValueError('Incomplete TCP inventory')
        owner, inode = _number(fields[7], maximum=2**32 - 1), _number(fields[9])
        if fields[1:3] == [local, remote]:
            if fields[3] != b'01' or owner != uid or not inode:
                raise ValueError('Native connection is not owned and established')
            matches.append(inode)
    if len(matches) != 1:
        raise ValueError('Native connection is absent or ambiguous')
    return matches[0]


def _socket_fd(pid, inode, deadline):
    directory = _PROC / str(pid) / 'fd'
    found = None
    with os.scandir(directory) as entries:
        for count, entry in enumerate(entries, 1):
            if count > _MAX_FDS or monotonic() >= deadline:
                raise OSError('Native descriptor inventory exceeded its bound')
            if not entry.name.isascii() or not entry.name.isdigit():
                raise ValueError('Invalid native descriptor')
            try:
                target = os.readlink(entry.path)
            except FileNotFoundError:
                continue  # Unrelated descriptors may close; the selected FD is rechecked.
            if target == f'socket:[{inode}]':
                found = entry.name
    if found is None:
        raise ValueError('Native connection is not held by the retained child')
    return found


def _same_socket(pid, descriptor, inode, uid):
    path = _PROC / str(pid) / 'fd' / descriptor
    if os.readlink(path) != f'socket:[{inode}]':
        return False
    info = path.stat()
    return stat.S_ISSOCK(info.st_mode) and info.st_ino == inode and info.st_uid == uid


class LinuxTCPPeerAuth:
    """No PID discovery, descendant trust, credential reads, or auth fallback."""

    def __init__(self):
        if sys.platform != 'linux':
            raise OSError('Linux native peer authentication is unavailable')
        self.process = self.identity = None
        self.bound = asyncio.Event()
        self.limit = asyncio.Semaphore(4)
        self._pending = set()

    def bind(self, process):
        # The owned waitid/WNOWAIT contract prevents PID reuse until retirement.
        if self.process is not None or not isinstance(process, OwnedProcess) or process.returncode is not None:
            raise IntegrationError('Local router native process binding refused.')
        identity = _identity(process.pid)
        if identity[1:4] != (os.getpid(), process.pid, os.geteuid()):
            raise IntegrationError('Local router requires its retained native child.')
        if process.returncode is not None or _identity(process.pid) != identity:
            raise IntegrationError('Local router native process identity changed.')
        self.process, self.identity = process, identity
        self.bound.set()

    def _same_process(self):
        return (self.process is not None and self.process.returncode is None
                and _identity(self.process.pid) == self.identity)

    def _proof(self, client_port, server_port):
        deadline = monotonic() + .8
        if not self._same_process():
            return False
        pid, _, _, uid, *_ = self.identity
        inode = _connection_inode(client_port, server_port, uid)
        descriptor = _socket_fd(pid, inode, deadline)
        return (_same_socket(pid, descriptor, inode, uid)
                and _connection_inode(client_port, server_port, uid) == inode
                and _same_socket(pid, descriptor, inode, uid)
                and self._same_process() and monotonic() < deadline)

    def _finished(self, task):
        self._pending.discard(task)
        self.limit.release()
        if not task.cancelled():
            task.exception()  # A timed-out caller must not leave an unobserved worker failure.

    async def __call__(self, scope):
        client, server = scope.get('client'), scope.get('server')
        if not all(isinstance(endpoint, (list, tuple)) and len(endpoint) == 2
                   and endpoint[0] == '127.0.0.1' and type(endpoint[1]) is int
                   and 1 <= endpoint[1] <= 65535 for endpoint in (client, server)):
            return False
        try:
            async with asyncio.timeout(1):
                await self.bound.wait()
                await self.limit.acquire()
                task = asyncio.create_task(asyncio.to_thread(self._proof, client[1], server[1]))
                self._pending.add(task)
                task.add_done_callback(self._finished)
                # Keep the worker's semaphore slot until it really finishes,
                # even when the requesting connection times out or disconnects.
                return await asyncio.shield(task)
        except (OSError, ValueError, TimeoutError, IntegrationError):
            return False
