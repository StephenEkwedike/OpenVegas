"""Content-free, bounded shutdown evidence; never a settlement or replay journal."""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from openvegas.emotes._windows_state import FILE_CREATE, private_windows_directory
from openvegas.emotes.spool import _locked, atomic_write, state_names

from .credentials import IntegrationError, profile

MAX_RECEIPTS = 64
MAX_ATTEMPTS = 4096
MAX_RECEIPT_BYTES = 4 * 1024 * 1024


def _check_capacity(directory, name):
    names = state_names(directory, limit=MAX_RECEIPTS, include_lock=False)
    if len(names) >= MAX_RECEIPTS or name in names:
        raise ValueError('Shutdown evidence store is full or identity collides')


def _flush_windows(handle):
    import ctypes
    from ctypes import wintypes

    flush = ctypes.WinDLL('kernel32.dll', winmode=0x800, use_last_error=True).FlushFileBuffers
    flush.argtypes = [wintypes.HANDLE]
    flush.restype = wintypes.BOOL
    if not flush(handle):
        raise OSError('Cannot flush private shutdown evidence')


def _publish_new_windows(api, handle, name):
    import ctypes as c

    from openvegas.emotes import _windows_resources as resources
    from openvegas.emotes._windows_state import _Rename

    # Same native handle-relative rename as the private state store, but never
    # replace an entry, even if a collision occurs after the locked inventory.
    resources._component(name)
    encoded = name.encode('utf-16-le')
    buffer = c.create_string_buffer(c.sizeof(_Rename) + len(encoded))
    info = _Rename.from_buffer(buffer)
    info.Flags, info.RootDirectory, info.FileNameLength = 0, None, len(encoded)
    c.memmove(c.addressof(buffer) + _Rename.FileName.offset, encoded, len(encoded))
    io = resources._IOStatusBlock()
    if api._set_native(handle, c.byref(io), buffer, len(buffer), 10) != 0:
        raise OSError('Cannot publish new private shutdown evidence')


def _save_windows(path, name, data):
    with private_windows_directory(path) as directory, directory.locked():
        _check_capacity(directory, name)
        temp = '.' + uuid4().hex + '.tmp'
        api = directory.api
        with directory.opened(temp, disposition=FILE_CREATE, writable=True, delete=True) as handle:
            published = False
            try:
                api.write(handle, data)
                info = api.private_info(handle)
                if info.st_size != len(data):
                    raise OSError('Incomplete shutdown evidence')
                _flush_windows(handle)
                _publish_new_windows(api, handle, name)
                published = True
                _flush_windows(handle)
            finally:
                if not published:
                    api.delete(handle)
        # A flush/readback failure is not success. Never remove or overwrite a
        # published record, and never mark an ambiguous attempt settled.
        with directory.opened(name) as handle:
            saved = api.private_info(handle)
            if (saved.st_dev, saved.st_ino, saved.st_size) != (info.st_dev, info.st_ino, len(data)):
                raise OSError('Published shutdown evidence changed')
            offset = 0
            while offset < len(data):
                chunk = api.read(handle, min(65536, len(data) - offset))
                if not chunk or chunk != data[offset:offset + len(chunk)]:
                    raise OSError('Private shutdown evidence could not be verified')
                offset += len(chunk)
            if api.read(handle, 1) or api.private_info(handle) != saved:
                raise OSError('Published shutdown evidence changed during readback')
    return path / name


def save_unresolved_dispatches(state, host):
    """Persist original lease identities without changing any in-memory outcome.

    This synchronous snapshot runs after listener/callback shutdown on the ledger
    owner. No provider, grant token, body, URL or financial state is serialized.
    The IDs are local dispatch evidence, not proof of backend reconciliation.
    """
    try:
        status = state.status()
        if (os.name not in ('posix', 'nt') or host not in ('codex', 'claude')
                or status['boundary_scope'] != 'requests' or status['active_requests']
                or status['pending'] is not None):
            raise ValueError('Shutdown evidence preconditions not met')
        # RouterState intentionally exposes no mutable ledger API. Read only the
        # immutable leases and terminal outcome; never serialize _Operation.
        records = tuple({
            'mode': attempt.lease.mode,
            'generation': attempt.lease.generation,
            'operation_id': attempt.lease.operation_id,
            'attempt_id': attempt.lease.attempt_id,
            'outcome': 'ambiguous',
        } for attempt in state._attempts.values() if attempt.outcome == 'ambiguous')
        if not 0 < len(records) == status['ambiguous_outcomes'] <= MAX_ATTEMPTS:
            raise ValueError('Unresolved evidence is missing or exceeds capacity')
        data = json.dumps({'schema_version': 1, 'host': host,
                           'boundary_scope': 'requests', 'unresolved_dispatches': records},
                          ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')
        if len(data) > MAX_RECEIPT_BYTES:
            raise ValueError('Shutdown evidence exceeds capacity')
        path = profile() / 'integrations' / 'router-unresolved'
        name = uuid4().hex + '.json'
        if os.name == 'nt':
            return _save_windows(path, name, data)
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Symlinked receipt path')
        missing = []
        ancestor = path
        while not ancestor.exists():
            missing.append(ancestor)
            ancestor = ancestor.parent
        with _locked(path) as directory:
            _check_capacity(directory, name)
            atomic_write(directory, name, data)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(directory)
            # Persist new directory entries too, including a newly made profile.
            for created in missing:
                parent = os.open(created.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
        return Path(path / name)
    except (OSError, ValueError, TypeError):
        raise IntegrationError(
            'Router closed with unresolved work; durable shutdown receipt could not be saved. '
            'No reconciliation or automatic retry was performed.') from None
