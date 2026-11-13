"""Content-free, bounded shutdown evidence; never a settlement or replay journal."""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from openvegas.emotes.spool import _locked, atomic_write, state_names

from .credentials import IntegrationError, profile

MAX_RECEIPTS = 64
MAX_ATTEMPTS = 4096
MAX_RECEIPT_BYTES = 4 * 1024 * 1024


def save_unresolved_dispatches(state, host):
    """Persist original lease identities without changing any in-memory outcome.

    This synchronous snapshot runs after listener/callback shutdown on the ledger
    owner. No provider, grant token, body, URL or financial state is serialized.
    The IDs are local dispatch evidence, not proof of backend reconciliation.
    """
    try:
        status = state.status()
        if (os.name != 'posix' or host not in ('codex', 'claude')
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
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Symlinked receipt path')
        missing = []
        ancestor = path
        while not ancestor.exists():
            missing.append(ancestor)
            ancestor = ancestor.parent
        name = uuid4().hex + '.json'
        with _locked(path) as directory:
            names = state_names(directory, limit=MAX_RECEIPTS, include_lock=False)
            if len(names) >= MAX_RECEIPTS or name in names:
                raise ValueError('Shutdown evidence store is full or identity collides')
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
