"""Read-only Darwin process proofs; never inspect arguments or credentials."""

from __future__ import annotations

import ctypes
import os
import sys


class _BSDInfo(ctypes.Structure):
    # Public ABI: sys/proc_info.h, proc_bsdinfo / PROC_PIDTBSDINFO.
    _fields_ = [(name, ctypes.c_uint32) for name in
                ('flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid',
                 'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in
                ('nfiles', 'pgid', 'jobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64),
                ('start_usec', ctypes.c_uint64)]


def _library():
    if (sys.platform != 'darwin' or ctypes.sizeof(_BSDInfo) != 136
            or _BSDInfo.start_sec.offset != 120):
        raise OSError('Unsupported process inventory ABI')
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    for name in ('proc_listpgrppids', 'proc_listchildpids'):
        call = getattr(lib, name)
        call.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        call.restype = ctypes.c_int
    return lib


def _members(call, pid):
    values = (ctypes.c_int * 1024)()
    ctypes.set_errno(0)
    count = call(pid, values, ctypes.sizeof(values))
    # The wrappers return PID counts, not byte lengths. A full buffer may
    # have truncated an inventory; neither it nor an error proves absence.
    if ctypes.get_errno() or not 0 <= count < len(values):
        raise OSError('Incomplete process inventory')
    result = tuple(values[:count])
    if any(value <= 0 for value in result) or len(set(result)) != count:
        raise OSError('Invalid process inventory')
    return set(result)


def _identity(lib, pid):
    value = _BSDInfo()
    ctypes.set_errno(0)
    # Nonzero arg includes retained zombies; arg=0 reports ESRCH for them.
    size = lib.proc_pidinfo(pid, 3, 1, ctypes.byref(value), ctypes.sizeof(value))
    if (size != ctypes.sizeof(value) or ctypes.get_errno() or value.pid != pid
            or not value.start_sec or value.start_usec >= 1_000_000):
        raise OSError('Process identity unavailable')
    return (value.pid, value.ppid, value.pgid, value.uid, value.status,
            value.start_sec, value.start_usec)


def _valid_group(pid):
    return type(pid) is int and 1 < pid < 2**31 and pid != os.getpgrp()


def darwin_server_alone(pid):
    """Snapshot of one owned server, not proof about previously detached work."""
    if not _valid_group(pid):
        return False
    try:
        lib = _library()
        before = _identity(lib, pid)
        expected = pid, os.getpid(), pid, os.geteuid()
        if before[:4] != expected or before[4] not in (1, 2, 3, 4):
            return False
        for _ in range(2):
            if (_members(lib.proc_listpgrppids, pid) != {pid}
                    or _members(lib.proc_listchildpids, pid)):
                return False
        after = _identity(lib, pid)
        # Numeric PID/parent/group/UID alone cannot distinguish a reused PID.
        # This binds the two observations, not the earlier launch or later kill.
        return (after[:4] == expected and after[4] in (1, 2, 3, 4)
                and after[5:] == before[5:])
    except (OSError, ValueError, AttributeError):
        return False


def darwin_group_retired(group):
    """Group-only snapshot; escaped/reparented descendants need separate proof."""
    if not _valid_group(group):
        return False
    try:
        lib = _library()
        for _ in range(2):
            for pid in _members(lib.proc_listpgrppids, group):
                _, _, pgid, uid, status, _, _ = _identity(lib, pid)
                if (pgid, uid, status) != (group, os.geteuid(), 5):  # SZOMB
                    return False
        return True
    except (OSError, ValueError, AttributeError):
        return False
