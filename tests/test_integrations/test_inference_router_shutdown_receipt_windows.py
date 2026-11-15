"""Windows request-boundary receipts. A saved record does not reconcile a charge."""

import ctypes
import json
import os
import time
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openvegas.emotes._windows_state import FILE_CREATE, WindowsDirectory, _Rename
from openvegas.integrations import inference_router_shutdown_receipt as receipts
from openvegas.integrations.credentials import IntegrationError
from openvegas.integrations.inference_router_state import RouterState


def ambiguous_host():
    state = RouterState(request_boundary=True)
    lease = state.begin("POST", "/sensitive-supplier-path")
    state.finish(lease, "ambiguous")
    for mode in ("v", "host"):
        state.commit(state.prepare(mode), request_boundary_verified=True)
        state.finish(state.begin("POST", "/other-private-path"), "completed")
    return state, lease


class MemoryAPI:
    """Exercise the real WindowsDirectory methods without pretending to test NT ACLs."""

    def __init__(self):
        self.files = {}
        self.handles = {}
        self.next_handle = 10
        self.held = False
        self.fault = ""
        self.flushes = []

    def child(self, parent, name, *, disposition=1, writable=False, delete=False, lock=False):
        assert parent == 1
        if lock:
            assert name == ".lock"
            self.files.setdefault(name, bytearray())
        elif disposition == FILE_CREATE:
            assert writable and delete and self.held
            if name in self.files:
                raise FileExistsError
            self.files[name] = bytearray()
        elif name not in self.files:
            raise FileNotFoundError
        self.next_handle += 1
        self.handles[self.next_handle] = [name, 0]
        return self.next_handle

    def close(self, handle):
        del self.handles[handle]

    def lock(self, handle):
        assert self.handles[handle][0] == ".lock"
        if self.held or self.fault == "busy":
            raise BlockingIOError
        self.held = True
        return handle

    def unlock(self, handle, token):
        assert handle == token and self.held
        self.held = False

    def names(self, parent, limit):
        assert parent == 1 and self.held
        return list(self.files)[: limit + 1]

    def write(self, handle, data):
        assert self.held
        self.files[self.handles[handle][0]][:] = data[:-1] if self.fault == "short" else data

    def private_info(self, handle):
        name = self.handles[handle][0]
        data = self.files[name]
        ino = id(data)
        if self.fault == "replaced" and name.endswith(".json"):
            ino += 1
        return SimpleNamespace(st_size=len(data), st_dev=1, st_ino=ino)

    def read(self, handle, size):
        assert self.held
        name, offset = self.handles[handle]
        data = self.files[name][offset : offset + size]
        self.handles[handle][1] += len(data)
        return bytes(len(data)) if self.fault == "readback" else bytes(data)

    def delete(self, handle):
        name = self.handles[handle][0]
        assert self.held and name.endswith(".tmp")
        del self.files[name]

    def flush(self, handle):
        assert self.held
        self.flushes.append(self.handles[handle][0])
        if self.fault == ("postflush" if self.flushes[-1].endswith(".json") else "preflush"):
            raise OSError("private filesystem diagnostic")

    def _set_native(self, handle, _io, buffer, length, kind):
        assert self.held and kind == 10 and len(buffer) == length
        info = _Rename.from_buffer(buffer)
        assert info.Flags == 0 and info.RootDirectory is None
        name = ctypes.string_at(
            ctypes.addressof(buffer) + _Rename.FileName.offset, info.FileNameLength
        ).decode("utf-16-le")
        if self.fault == "late-collision":
            self.files[name] = bytearray(b"previous receipt")
        if name in self.files:
            return -1
        old = self.handles[handle][0]
        self.files[name] = self.files.pop(old)
        self.handles[handle][0] = name
        return 0


@pytest.fixture
def store(tmp_path, monkeypatch):
    api = MemoryAPI()
    expected = tmp_path / "profile" / "integrations" / "router-unresolved"

    @contextmanager
    def private(path):
        assert path == expected
        if api.fault == "unsafe-directory":
            raise OSError
        yield WindowsDirectory(api, 1)

    monkeypatch.setattr(receipts, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(receipts, "profile", lambda: tmp_path / "profile")
    monkeypatch.setattr(receipts, "private_windows_directory", private)
    monkeypatch.setattr(receipts, "_flush_windows", api.flush)
    return api, expected


def test_only_unresolved_ids_survive_host_wallet_host_teardown(store):
    api, root = store
    state, lease = ambiguous_host()
    before = state.status()
    path = receipts.save_unresolved_dispatches(state, "codex")
    assert path.parent == root
    assert json.loads(api.files[path.name]) == {
        "schema_version": 1,
        "host": "codex",
        "boundary_scope": "requests",
        "unresolved_dispatches": [
            {
                "mode": "host",
                "generation": 0,
                "operation_id": lease.operation_id,
                "attempt_id": lease.attempt_id,
                "outcome": "ambiguous",
            }
        ],
    }
    assert len(api.flushes) == 2 and api.flushes[0].endswith(".tmp") and api.flushes[1] == path.name
    assert set(api.files) == {".lock", path.name} and not api.handles
    assert state.status() == before and before["ambiguous_outcomes"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["", "postflush"])
async def test_router_warns_only_after_verified_windows_receipt(store, monkeypatch, capsys, fault):
    from openvegas.integrations import inference_router_session as router
    from openvegas.integrations.inference_router_adapter import ProviderAdapter

    api, _ = store
    api.fault = fault

    @asynccontextmanager
    async def listener(_):
        yield "http://127.0.0.1:10001"

    monkeypatch.setattr(router, "serve", listener)
    monkeypatch.setattr(router, "native_tcp_peer_auth", Mock)
    adapter = ProviderAdapter(
        "codex", "https://api.openai.com/v1", "https://fixture.invalid/integrations/native"
    )
    prepare = AsyncMock(return_value=router.WalletAccess("ov_native_" + "x" * 32, time.time() + 60))
    revoke = AsyncMock(return_value=True)
    session = router.RouterSession(
        adapter,
        local_auth="peer",
        request_boundary=True,
        prepare_wallet=prepare,
        revoke_wallet=revoke,
    )

    async def launch():
        async with session.launch(
            version="0.166.0", env={}, args=[], effective_policy_verified=True
        ):
            lease = session.state.begin("POST", "/private-host-background-request")
            session.state.finish(lease, "ambiguous")
            for mode in ("v", "host"):
                assert (await session.command("openvegas use " + mode)).changed

    if fault:
        with pytest.raises(IntegrationError, match="could not be saved"):
            await launch()
    else:
        await launch()
    warning = capsys.readouterr().err
    assert ("server financial holds are unchanged" in warning) == (not fault)
    assert session.state.mode == "host" and session.state.generation == 2
    assert session.state.status()["ambiguous_outcomes"] == 1
    prepare.assert_awaited_once()
    revoke.assert_awaited_once_with(prepare.return_value)


@pytest.mark.parametrize(
    "fault",
    [
        "unsafe-directory",
        "busy",
        "full",
        "short",
        "preflush",
        "late-collision",
        "postflush",
        "replaced",
        "readback",
    ],
)
def test_failed_windows_evidence_never_claims_cleanup_or_changes_payer(store, fault, monkeypatch):
    api, _ = store
    api.fault = fault
    api.files["old.json"] = bytearray(b"untouched")
    if fault == "full":
        monkeypatch.setattr(receipts, "MAX_RECEIPTS", 1)
    state, _ = ambiguous_host()
    before = state.status()
    with pytest.raises(IntegrationError, match="No reconciliation or automatic retry") as caught:
        receipts.save_unresolved_dispatches(state, "codex")
    assert "filesystem diagnostic" not in str(caught.value)
    assert state.status() == before and not api.held and not api.handles
    assert api.files["old.json"] == b"untouched" and not any(n.endswith(".tmp") for n in api.files)
    others = [v for k, v in api.files.items() if k not in ("old.json", ".lock")]
    if fault == "late-collision":
        assert others == [b"previous receipt"]
    elif fault in ("postflush", "replaced", "readback"):
        assert len(others) == 1
    else:
        assert not others


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows NT handles and ACLs")
def test_native_windows_private_flushed_receipt_survives_reopen(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts, "profile", lambda: tmp_path / "isolated-profile")
    state, lease = ambiguous_host()
    path = receipts.save_unresolved_dispatches(state, "codex")
    with receipts.private_windows_directory(path.parent) as d, d.locked():
        original = d.read(path.name, receipts.MAX_RECEIPT_BYTES)
        assert json.loads(original)["unresolved_dispatches"][0]["attempt_id"] == lease.attempt_id
        with pytest.raises(IntegrationError, match="could not be saved"):
            receipts.save_unresolved_dispatches(state, "codex")
    monkeypatch.setattr(receipts, "uuid4", lambda: SimpleNamespace(hex=path.stem))
    with pytest.raises(IntegrationError, match="could not be saved"):
        receipts.save_unresolved_dispatches(state, "codex")
    with receipts.private_windows_directory(path.parent) as d, d.locked():
        assert d.read(path.name, receipts.MAX_RECEIPT_BYTES) == original
    assert state.status()["ambiguous_outcomes"] == 1
