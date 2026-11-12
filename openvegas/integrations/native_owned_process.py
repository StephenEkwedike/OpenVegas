"""Pinned POSIX child lifetimes. No asyncio child watcher or implicit Popen reap.

Only this module may reap these children. The host must retain SIGCHLD's default
disposition and must not run wait(-1)/foreign reapers. An escaped session remains
outside the group contract. An uncertain lifetime is retained, never signalled.
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
import sys
import threading

from .credentials import IntegrationError

_ACTIVE = {}  # Strong references prevent Popen.__del__ from reaping a pinned PID.
_KEY = object()


class _DarwinSiginfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int) for name in ('signo', 'error', 'code', 'pid')]
    _fields_ += [('uid', ctypes.c_uint32), ('status', ctypes.c_int),
                ('addr', ctypes.c_void_p), ('value', ctypes.c_void_p),
                ('band', ctypes.c_long), ('pad', ctypes.c_ulong * 7)]


class _DarwinSigaction(ctypes.Structure):
    _fields_ = [('handler', ctypes.c_void_p), ('mask', ctypes.c_uint32),
                ('flags', ctypes.c_int)]


class _LinuxSigaction(ctypes.Structure):
    # glibc Linux LP64 (x86_64/aarch64); never assume a musl/32-bit layout.
    _fields_ = [('handler', ctypes.c_void_p), ('mask', ctypes.c_ulong * 16),
                ('flags', ctypes.c_int), ('restorer', ctypes.c_void_p)]


def _default_reaper(lib, layout, no_child_wait):
    lib.sigaction.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
    lib.sigaction.restype = ctypes.c_int
    action = layout()
    if (lib.sigaction(signal.SIGCHLD, None, ctypes.byref(action))
            or action.handler or action.flags & no_child_wait):
        raise IntegrationError('Native child lifetime requires retained child exit status.')


def _contract():
    if (os.name != 'posix' or sys.platform not in ('darwin', 'linux')
            or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL):
        raise IntegrationError('Native child lifetime requires an exclusive default reaper.')
    if sys.platform == 'darwin':
        if ctypes.sizeof(_DarwinSiginfo) != 104 or ctypes.sizeof(_DarwinSigaction) != 16:
            raise IntegrationError('Unsupported Darwin child lifetime ABI.')
        lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        _default_reaper(lib, _DarwinSigaction, 0x20)
        lib.waitid.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
        lib.waitid.restype = ctypes.c_int
        return lib
    if not all(hasattr(os, name) for name in ('waitid', 'WNOWAIT', 'WEXITED', 'P_PID')):
        raise IntegrationError('Native child lifetime observation is unavailable.')
    lib = ctypes.CDLL(None, use_errno=True)
    if (not hasattr(lib, 'gnu_get_libc_version')
            or os.uname().machine not in ('x86_64', 'aarch64')
            or ctypes.sizeof(_LinuxSigaction) != 152 or _LinuxSigaction.flags.offset != 136):
        raise IntegrationError('Unsupported Linux child lifetime ABI.')
    _default_reaper(lib, _LinuxSigaction, 2)
    return None


def _observe(pid):
    lib = _contract()
    try:
        if lib is None:
            info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if info is None:
                return None
            found, code, status = info.si_pid, info.si_code, info.si_status
        else:
            info = _DarwinSiginfo()
            if lib.waitid(1, pid, ctypes.byref(info), 4 | 1 | 0x20):
                raise OSError(ctypes.get_errno(), 'Child observation failed')
            found, code, status = info.pid, info.code, info.status
            if found == 0:
                return None
        if found != pid or code not in (1, 2, 3):
            raise OSError('Invalid child exit observation')
        return status if code == 1 else -status
    except OSError:
        raise IntegrationError('Owned child lifetime was lost; no group signal authorized.') from None


class _PinnedProcess:
    def __init__(self, child, key):
        if key is not _KEY:
            raise IntegrationError('Native process requires owned launch.')
        self._child = child
        self._pid = child.pid
        self._lock = threading.RLock()
        self.stop_lock = asyncio.Lock()
        self._retired = False
        self._status = None
        _ACTIVE[id(self)] = self

    @property
    def pid(self):
        return self._pid

    @property
    def retired(self):
        return self._retired

    @property
    def returncode(self):
        with self._lock:
            return self._status if self._retired else _observe(self.pid)

    async def wait(self):
        """Observe exit without releasing PID/PGID ownership."""
        while (status := self.returncode) is None:
            await asyncio.sleep(.01)
        return status

    async def communicate(self):
        # Never Popen.communicate/wait/poll: each can reap the leader too early.
        data = (await asyncio.to_thread(self._child.stdout.read, 8 * 1024 * 1024 + 1)
                if self._child.stdout is not None else None)
        await self.wait()
        return data, None

    def _signal(self, sig, *, group):
        with self._lock:
            if self._retired or _ACTIVE.get(id(self)) is not self:
                raise IntegrationError('Retired child cannot authorize signals.')
            status = _observe(self.pid)
            if not group and status is not None:
                return False
            # No owned reaper can run between this observation and the signal;
            # exited leaders remain kernel zombies, so their PID cannot be reused.
            try:
                if group:
                    os.killpg(self.pid, sig)
                else:
                    os.kill(self.pid, sig)
                return True
            except ProcessLookupError:
                return False

    def _reap(self):
        with self._lock:
            if self._retired:
                return
            status = _observe(self.pid)
            if status is None:
                raise IntegrationError('Owned leader has not exited; cannot release lifetime.')
            try:
                pid, raw = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                raise IntegrationError('Owned child reaper contract was violated.') from None
            if pid != self.pid or os.waitstatus_to_exitcode(raw) != status:
                raise IntegrationError('Owned child retirement was not verified.')
            self._child.returncode = self._status = status
            self._retired = True
            _ACTIVE.pop(id(self))
            if self._child.stdout is not None:
                self._child.stdout.close()


class OwnedProcess(_PinnedProcess):
    """Private new-session server whose entire process group is owned."""

    def signal_group(self, sig):
        return self._signal(sig, group=True)

    def reap_retired_group(self):
        """Called only after group retirement, under the caller's stop lock."""
        self._reap()


class OwnedLeader(_PinnedProcess):
    """Inherited interactive job control; never authorizes a process-group signal."""

    def signal_leader(self, sig):
        return self._signal(sig, group=False)

    def reap_exited_leader(self):
        """Release this child only, without claiming descendant retirement."""
        self._reap()


async def spawn(*args, **kwargs):
    """Used inside the route's cancellation-settled launch transaction."""
    _contract()
    if (kwargs.get('start_new_session') is not True
            or kwargs.get('stdin') != subprocess.DEVNULL
            or kwargs.get('stderr') != subprocess.DEVNULL
            or kwargs.get('stdout') not in (subprocess.PIPE, subprocess.DEVNULL)):
        raise IntegrationError('Unsupported owned native process launch.')

    def launch():
        _contract()
        return OwnedProcess(subprocess.Popen(args, **kwargs), _KEY)

    return await asyncio.to_thread(launch)


async def spawn_leader(*args, env):
    """Pinned TUI child with unchanged inherited stdio, cwd, session and pgrp."""
    _contract()

    def launch():
        _contract()
        return OwnedLeader(subprocess.Popen(args, env=env), _KEY)

    return await asyncio.to_thread(launch)
