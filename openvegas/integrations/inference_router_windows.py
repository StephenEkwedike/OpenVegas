"""Windows TCP peer authentication for one retained, locally launched child.

Trust the process handle from CreateProcess, never a PID supplied in isolation.
The IP Helper table identifies the context binder, not subsequent transfers of
an already authenticated socket. The trusted native child must not share it.
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import socket
import subprocess
import sys

from .credentials import IntegrationError

_DWORD = ctypes.c_uint32
_LOOPBACK = int.from_bytes(socket.inet_aton('127.0.0.1'), 'little')
_MAX_TABLE = 2 * 1024 * 1024
_TIMEOUT = 1.0
_ESTABLISHED = 5
_OWNER_MODULE_CONNECTIONS = 7
_INSUFFICIENT_BUFFER = 122


class _TCPRow(ctypes.Structure):
    # Windows SDK: MIB_TCPROW_OWNER_MODULE. Do not interpret module metadata.
    _fields_ = [(name, _DWORD) for name in (
        'state', 'local_addr', 'local_port', 'remote_addr', 'remote_port', 'pid'
    )] + [('created', ctypes.c_int64), ('module', ctypes.c_uint64 * 16)]


class _TCPTable(ctypes.Structure):
    _fields_ = [('count', _DWORD), ('rows', _TCPRow * 1)]


def _connection(buffer, size, ports, identity):
    offset, stride = _TCPTable.rows.offset, ctypes.sizeof(_TCPRow)
    if not offset <= size <= ctypes.sizeof(buffer) <= _MAX_TABLE:
        raise ValueError('Invalid TCP inventory size')
    count = _DWORD.from_buffer(buffer).value
    if count > (size - offset) // stride:
        raise ValueError('Incomplete TCP inventory')
    pid, birth = identity
    forward = tuple(map(socket.htons, ports))
    reverse = forward[::-1]
    stamps = {}
    for index in range(count):
        row = _TCPRow.from_buffer(buffer, offset + index * stride)
        if row.local_addr != _LOOPBACK or row.remote_addr != _LOOPBACK:
            continue
        pair = row.local_port & 0xffff, row.remote_port & 0xffff
        if pair != forward and pair != reverse:
            continue
        if (pair in stamps or row.state != _ESTABLISHED or row.created <= 0
                or row.pid != (pid if pair == forward else os.getpid())
                or (pair == forward and row.created <= birth)):
            raise ValueError('Ambiguous TCP ownership')
        stamps[pair] = row.created
    if len(stamps) != 2:
        raise ValueError('Missing TCP ownership')
    return stamps[forward], stamps[reverse]


class _Windows:
    def __init__(self):
        if (sys.platform != 'win32' or os.name != 'nt' or sys.byteorder != 'little'
                or ctypes.sizeof(_TCPRow) != 160 or _TCPTable.rows.offset != 8):
            raise OSError('Windows native peer authentication is unavailable')
        # Restrict DLL resolution to System32, not the native launch directory.
        system32 = 0x800
        kernel = ctypes.WinDLL('kernel32.dll', winmode=system32)
        ip = ctypes.WinDLL('iphlpapi.dll', winmode=system32)
        self.system_time = kernel.GetSystemTimePreciseAsFileTime
        self.system_time.argtypes = [ctypes.POINTER(ctypes.c_uint64)]
        self.system_time.restype = None
        self.process_id = kernel.GetProcessId
        self.process_id.argtypes = [ctypes.c_void_p]
        self.process_id.restype = _DWORD
        self.process_times = kernel.GetProcessTimes
        self.process_times.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
        self.process_times.restype = ctypes.c_int
        self.table = ip.GetExtendedTcpTable
        self.table.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DWORD), ctypes.c_int,
                               _DWORD, ctypes.c_int, _DWORD]
        self.table.restype = _DWORD

    def now(self):
        value = ctypes.c_uint64()
        self.system_time(ctypes.byref(value))
        return value.value

    @staticmethod
    def pin(child):
        import _winapi

        # Popen owns the original CreateProcess handle. Never reopen by PID.
        handle = child._handle
        if not isinstance(handle, subprocess.Handle) or handle.closed:
            raise ValueError('Missing launch handle')
        current = _winapi.GetCurrentProcess()
        rights = 0x00100000 | 0x1000  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        return subprocess.Handle(_winapi.DuplicateHandle(current, handle, current, rights, False, 0))

    def identity(self, handle):
        import _winapi

        pid = self.process_id(handle)
        times = [ctypes.c_uint64() for _ in range(4)]
        if (not pid or not self.process_times(handle, *(ctypes.byref(t) for t in times))
                or not times[0].value or _winapi.WaitForSingleObject(handle, 0) != _winapi.WAIT_TIMEOUT):
            raise ValueError('Native child identity is unavailable or exited')
        return pid, times[0].value

    def connection(self, ports, identity):
        size = _DWORD()
        buf = None
        # Allow one resize if unrelated connections appeared since the size probe.
        for _ in range(3):
            status = self.table(buf, ctypes.byref(size), False, socket.AF_INET, _OWNER_MODULE_CONNECTIONS, 0)
            if status == 0 and buf is not None:
                return _connection(buf, size.value, ports, identity)
            if (status != _INSUFFICIENT_BUFFER or not _TCPTable.rows.offset <= size.value <= _MAX_TABLE
                    or (buf is not None and size.value <= ctypes.sizeof(buf))):
                break
            buf = ctypes.create_string_buffer(size.value)
        raise OSError('Windows TCP ownership inventory is unavailable')


class WindowsTCPPeerAuth:
    """Bind once to Popen or a launch wrapper retaining that Popen as _child."""

    def __init__(self):
        self._api = _Windows()
        self._process = self._handle = self._identity = None
        self._bound = asyncio.Event()
        self._limit = asyncio.Semaphore(4)

    def bind(self, process):
        child = process if isinstance(process, subprocess.Popen) else getattr(process, '_child', None)
        if self._process is not None or not isinstance(child, subprocess.Popen):
            raise IntegrationError('Local router requires the retained Windows child.')
        handle = None
        try:
            pid = process.pid
            if (type(pid) is not int or pid <= 1 or pid == os.getpid() or child.pid != pid
                    or process.returncode is not None):
                raise ValueError('Invalid launch identity')
            handle = self._api.pin(child)
            identity = self._api.identity(handle)
            if identity[0] != pid:
                raise ValueError('Launch handle and PID differ')
        except (OSError, ValueError, AttributeError, TypeError, IntegrationError):
            if handle is not None:
                handle.Close()
            raise IntegrationError('Local router Windows child identity not confirmed.') from None
        self._process, self._handle, self._identity = process, handle, identity
        self._bound.set()

    def _live(self):
        return (self._process.returncode is None and self._process.pid == self._identity[0]
                and self._api.identity(self._handle) == self._identity)

    def _proof(self, ports, observed):
        if not self._live():
            return False
        before = self._api.connection(ports, self._identity)
        return (max(before) <= observed and before == self._api.connection(ports, self._identity)
                and self._live())

    def _finished(self, future):
        self._limit.release()
        if not future.cancelled():
            future.exception()

    async def __call__(self, scope):
        client, server = scope.get('client'), scope.get('server')
        if (not all(isinstance(ep, (list, tuple)) and len(ep) == 2 and ep[0] == '127.0.0.1'
                    and type(ep[1]) is int and 1 <= ep[1] <= 65535 for ep in (client, server))
                or client[1] == server[1]):
            return False
        try:
            # Never identify a queued request by a connection created during its wait.
            observed = self._api.now()
            async with asyncio.timeout(_TIMEOUT):
                await self._bound.wait()
                await self._limit.acquire()
                try:
                    loop = asyncio.get_running_loop()
                    future = loop.run_in_executor(None, self._proof, (client[1], server[1]), observed)
                except BaseException:
                    self._limit.release()
                    raise
                # A timeout or disconnect must not release a still-running inspector's slot.
                future.add_done_callback(self._finished)
                return await asyncio.shield(future)
        except (OSError, ValueError, AttributeError, TypeError, RuntimeError, IntegrationError):
            return False
