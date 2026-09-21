"""No network, real customer files, premium artwork or payment services."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from openvegas.emotes import remote as remote_module
from openvegas.emotes.manifest import PackError
from openvegas.emotes.remote import (
    LEASE_SECONDS,
    MAX_CACHE_BYTES,
    MAX_CACHE_FILES,
    MAX_CACHE_SCAN,
    MAX_ENTITLEMENTS,
    MAX_MANIFEST_BYTES,
    MAX_PACKS,
    MAX_PRIVATE_SHEET_BYTES,
    RemoteError,
    RemoteLibrary,
)
from openvegas.emotes.resources import Catalog, CatalogEntry, PackRepository
from openvegas.emotes.selection import SelectionStore
from openvegas.emotes.spool import _locked


class FakeAPI:
    backend_scope = "https://example.test"

    def __init__(self, pack_dir):
        self.user = str(uuid4())
        self.fingerprint = (self.backend_scope, self.user)
        self.calls = []
        self.rows = [
            {
                "item_id": "test-companion",
                "pack_id": "fixture.pack",
                "slot": "companion",
                "effective_status": "active",
                "activatable": True,
                "acquired_version": "0.5.0",
                "available_version": "1.0.0",
            }
        ]
        self.equipped = {"companion": "test-companion"}
        self.bundle = {
            "schema_version": 1,
            "item_id": "test-companion",
            "pack_id": "fixture.pack",
            "version": "1.0.0",
            "manifest": json.loads((pack_dir / "manifest.json").read_text()),
            "sheet_base64": base64.b64encode((pack_dir / "sheet.png").read_bytes()).decode(),
        }
        self.fail = None
        self.on_pack = None
        self.block_owned = None

    def identity(self):
        return self.fingerprint

    async def owned(self):
        self.calls.append("owned")
        if self.block_owned:
            await self.block_owned.wait()
        if self.fail:
            raise self.fail
        return {
            "account_id": self.user,
            "entitlements": copy.deepcopy(self.rows),
            "equipped": dict(self.equipped),
        }

    async def pack(self, item_id):
        self.calls.append(("pack", item_id))
        if self.on_pack:
            self.on_pack()
        return copy.deepcopy(self.bundle)

    async def equip(self, item_id, slot="companion"):
        self.calls.append(("equip", item_id, slot))
        self.equipped[slot] = item_id
        return {"item_id": item_id, "slot": slot}


@pytest.fixture
def remote(tmp_path, pack_dir, clock):
    api = FakeAPI(pack_dir)
    selection = SelectionStore(tmp_path / "state")
    library = RemoteLibrary(api, tmp_path / "cache", selection, clock=clock)
    return library, api, selection, clock


@pytest.mark.asyncio
async def test_sync_restores_data_and_server_selection_only(remote, pack_dir):
    library, api, selection, _ = remote
    repository = library.repository(PackRepository(pack_dir.parent))
    catalog = library.catalog(Catalog())
    result = await library.sync()
    assert result["selected"] == selection.read() == "fixture.pack"
    assert result["available"] == result["downloaded"] == ["fixture.pack"]
    assert result["needs_sync"] == []
    assert library.authorize("fixture.pack")
    resource = catalog.resource_for("fixture.pack")
    assert resource.startswith("remote-")
    assert repository.load(resource).manifest.pack_id == "fixture.pack"
    assert (
        selection.load_selected(catalog=catalog, repository=repository).manifest.version == "1.0.0"
    )
    assert not any(isinstance(call, tuple) and call[0] == "equip" for call in api.calls)
    cached = next(library.cache.glob("*.json"))
    assert cached.stat().st_mode & 0o777 == 0o600
    assert library.cache.stat().st_mode & 0o777 == 0o700
    assert api.user not in cached.name
    assert "entitlements" not in json.loads(cached.read_text())


@pytest.mark.asyncio
async def test_equip_downloads_before_server_then_prefill(remote):
    library, api, selection, _ = remote
    api.equipped.clear()
    result = await library.equip("fixture.pack")
    assert result["selected"] == selection.read() == "fixture.pack"
    assert api.calls == [
        "owned",
        ("pack", "test-companion"),
        ("equip", "test-companion", "companion"),
        "owned",
    ]
    await library.equip(None)
    assert selection.read() is None
    assert api.equipped["companion"] is None


@pytest.mark.asyncio
async def test_refresh_no_pack_network_and_lease_expires(remote):
    library, api, selection, clock = remote
    await library.sync()
    api.calls.clear()
    clock.advance(25)
    await library.refresh()
    assert api.calls == ["owned"]
    clock.advance(29.99)
    for _ in range(20):
        assert library.authorize("fixture.pack")
    assert api.calls == ["owned"]
    clock.now = 55.0
    assert not library.authorize("fixture.pack")
    assert selection.read() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["revoked", "expired", "unknown"])
async def test_revocation_clears_owned_selection_and_repository(remote, status):
    library, api, selection, _ = remote
    await library.sync()
    catalog = library.catalog(Catalog())
    name = catalog.resource_for("fixture.pack")
    api.rows[0]["effective_status"] = status
    result = await library.refresh()
    assert result["available"] == []
    assert not library.authorize("fixture.pack")
    assert selection.read() is None
    with pytest.raises(PackError):
        library.repository(PackRepository()).load(name)


@pytest.mark.asyncio
async def test_failures_remove_lease_and_no_secrets(remote):
    library, api, selection, _ = remote
    await library.sync()
    api.fail = RuntimeError("bearer SUPER_SECRET upstream body")
    with pytest.raises(RemoteError) as exc:
        await library.refresh()
    assert "SUPER_SECRET" not in str(exc.value)
    assert not library.authorize("fixture.pack")
    assert selection.read() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fingerprint",
    [
        None,
        ("https://example.test", None),
        ("https://other.test", str(uuid4())),
        ("https://example.test", str(uuid4())),
    ],
)
async def test_identity_switch_invalidates_immediately_without_network(remote, fingerprint):
    library, api, selection, _ = remote
    await library.sync()
    api.calls.clear()
    api.fingerprint = fingerprint
    assert not library.authorize("fixture.pack")
    assert selection.read() is None
    assert api.calls == []


@pytest.mark.asyncio
async def test_response_account_must_match_local_authenticated_hint(remote):
    library, api, selection, _ = remote
    api.user = str(uuid4())
    with pytest.raises(RemoteError, match="account mismatch"):
        await library.sync()
    assert api.calls == ["owned"]
    assert not library.authorize("fixture.pack")
    assert selection.read() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity", "invalidate", "revoked", "version"])
async def test_account_or_ownership_change_during_download_never_grants(remote, change):
    library, api, selection, _ = remote
    if change == "identity":
        api.on_pack = lambda: setattr(api, "fingerprint", (api.backend_scope, str(uuid4())))
    elif change == "invalidate":
        api.on_pack = library.invalidate
    else:
        key, value = (
            ("effective_status", "revoked")
            if change == "revoked"
            else ("available_version", "2.0.0")
        )
        api.on_pack = lambda: api.rows[0].update({key: value})
    if change in {"identity", "invalidate"}:
        with pytest.raises(RemoteError):
            await library.sync()
    else:
        result = await library.sync()
        assert result["available"] == []
    assert not library.authorize("fixture.pack")
    assert selection.read() is None


@pytest.mark.asyncio
async def test_offline_new_instance_cannot_use_even_valid_cached_pack(remote):
    library, api, selection, clock = remote
    await library.sync()
    other = RemoteLibrary(api, library.cache, selection, clock=clock)
    assert not other.authorize("fixture.pack")
    api.fail = OSError("offline")
    with pytest.raises(RemoteError):
        await other.sync()
    assert not other.authorize("fixture.pack")


@pytest.mark.asyncio
async def test_tampered_self_hashed_cache_repaired_only_from_online_pack(remote):
    library, api, selection, clock = remote
    await library.sync()
    path = next(library.cache.glob("*.json"))
    tampered = json.loads(path.read_text())
    tampered["manifest"]["display_name"] = "Forgery"
    data = base64.b64decode(tampered["sheet_base64"])
    tampered["manifest"]["sha256"] = hashlib.sha256(data).hexdigest()
    path.write_text(json.dumps(tampered))
    other = RemoteLibrary(api, library.cache, selection, clock=clock)
    await other.sync()
    entry = other.catalog(Catalog()).get("fixture.pack")
    assert entry.display_name == "Test Fixture Only"
    assert json.loads(path.read_text())["manifest"]["display_name"] != "Forgery"


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["symlink", "hardlink", "permissions", "fifo"])
async def test_unsafe_cache_fails_closed(remote, tmp_path, attack):
    library, _, selection, _ = remote
    await library.sync()
    path = next(library.cache.glob("*.json"))
    outside = tmp_path / "outside"
    outside.write_text("do not overwrite")
    outside.chmod(0o600)
    if attack == "permissions":
        path.chmod(0o644)
    else:
        path.unlink()
        if attack == "symlink":
            path.symlink_to(outside)
        elif attack == "hardlink":
            os.link(outside, path)
        else:
            os.mkfifo(path, 0o600)
    with pytest.raises(RemoteError):
        await library.sync()
    assert outside.read_text() == "do not overwrite"
    assert not library.authorize("fixture.pack")
    assert selection.read() is None


@pytest.mark.asyncio
async def test_cache_names_partition_account_and_backend(remote):
    library, api, _, _ = remote
    await library.sync()
    first = {p.name for p in library.cache.glob("*.json")}
    api.user = str(uuid4())
    api.fingerprint = (api.backend_scope, api.user)
    await library.sync()
    assert len(list(library.cache.glob("*.json"))) == 2
    api.backend_scope = "https://another.test"
    api.fingerprint = (api.backend_scope, api.user)
    await library.sync()
    files = {p.name for p in library.cache.glob("*.json")}
    assert len(files) == 3 and first < files


@pytest.mark.asyncio
@pytest.mark.parametrize("activatable", [False, None, 1, "true"])
async def test_preview_only_never_promoted_without_explicit_server_approval(remote, activatable):
    library, api, _, _ = remote
    api.rows[0]["activatable"] = activatable
    catalog = library.catalog(Catalog([CatalogEntry("fixture.pack", "fixture", "Preview")]))
    result = await library.sync()
    assert result["available"] == []
    assert catalog.resource_for("fixture.pack", preview=True) == "fixture"
    with pytest.raises(PackError, match="preview-only"):
        catalog.resource_for("fixture.pack")
    with pytest.raises(RemoteError, match="ownership"):
        await library.equip("fixture.pack")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("item_id", "someone-else"),
        ("pack_id", "other.pack"),
        ("version", "2.0.0"),
        ("sheet_base64", "!notbase64!"),
        ("sheet_base64", "YQ=="),
    ],
)
async def test_invalid_private_bundles_fail_before_equipping(remote, field, value):
    library, api, selection, _ = remote
    api.bundle[field] = value
    with pytest.raises(RemoteError):
        await library.equip("fixture.pack")
    assert not any(isinstance(call, tuple) and call[0] == "equip" for call in api.calls)
    assert selection.read() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["manifest", "sheet", "unknown", "nested-id", "nested-version"])
async def test_bundle_size_shape_identity_bounds(remote, kind):
    library, api, _, _ = remote
    if kind == "manifest":
        api.bundle["manifest"]["display_name"] = "x" * MAX_MANIFEST_BYTES
    elif kind == "sheet":
        api.bundle["sheet_base64"] = "x" * (4 * ((MAX_PRIVATE_SHEET_BYTES + 2) // 3) + 1)
    elif kind == "unknown":
        api.bundle["license"] = "fake-trust"
    else:
        api.bundle["manifest"]["pack_id" if kind == "nested-id" else "version"] = "bad"
    with pytest.raises(RemoteError):
        await library.sync()
    assert not library.authorize("fixture.pack")


@pytest.mark.asyncio
async def test_pack_limit_and_entitlement_limit(remote):
    library, api, _, _ = remote
    original = api.rows[0]
    for count in (MAX_PACKS + 1, MAX_ENTITLEMENTS + 1):
        api.calls.clear()
        api.rows = [
            {**original, "item_id": f"item-{i}", "pack_id": f"pack-{i}"} for i in range(count)
        ]
        with pytest.raises(RemoteError):
            await library.sync()
        assert api.calls == ["owned"]


@pytest.mark.asyncio
async def test_server_equipment_changes_and_new_version_require_sync(remote):
    library, api, selection, _ = remote
    await library.sync()
    api.equipped.clear()
    await library.refresh()
    assert selection.read() is None
    api.rows[0]["available_version"] = "2.0.0"
    report = await library.refresh()
    assert report["needs_sync"] == ["fixture.pack"]
    assert not library.authorize("fixture.pack")


@pytest.mark.asyncio
async def test_completion_does_not_change_companion_preference(remote):
    library, api, selection, _ = remote
    api.rows[0]["slot"] = "completion"
    api.equipped.clear()
    await library.equip("fixture.pack", slot="completion")
    assert api.equipped == {"completion": "test-companion"}
    assert selection.read() is None
    assert library.authorize("fixture.pack")


@pytest.mark.asyncio
async def test_timeout_cancel_and_reentry_are_bounded(remote):
    library, api, selection, _ = remote
    await library.sync()
    library.request_timeout = 0.02
    api.block_owned = asyncio.Event()
    task = asyncio.create_task(library.refresh())
    await asyncio.sleep(0)
    with pytest.raises(RemoteError, match="in progress"):
        await library.sync()
    with pytest.raises(RemoteError):
        await task
    assert selection.read() is None
    task = asyncio.create_task(library.refresh())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    api.block_owned = None
    await library.sync()
    assert library.authorize("fixture.pack")


def test_sequential_operations_work_across_asyncio_run_loops(remote):
    library, _, _, _ = remote
    asyncio.run(library.sync())
    asyncio.run(library.refresh())
    asyncio.run(library.equip(None))
    assert library.authorize("fixture.pack")


@pytest.mark.asyncio
async def test_clock_rollback_fails_closed(remote):
    library, _, selection, clock = remote
    clock.advance(10)
    await library.sync()
    clock.advance(-1)
    assert not library.authorize("fixture.pack")
    assert selection.read() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    [
        "http://remote.test",
        "https://user:pass@example.test",
        "https://example.test/?token=secret",
        "https://example.test/#frag",
    ],
)
async def test_unsafe_backend_scopes_fail_before_network(remote, scope):
    library, api, _, _ = remote
    api.backend_scope = scope
    api.fingerprint = (scope, api.user)
    with pytest.raises(RemoteError):
        await library.sync()
    assert api.calls == []


def test_constant_lease_cannot_be_extended_by_local_settings():
    assert LEASE_SECONDS == 30


@pytest.mark.asyncio
async def test_background_refresh_preserves_local_off_and_selection_revision(remote):
    library, _, selection, _ = remote
    await library.sync()
    revision = selection.revision()
    await library.refresh()
    assert selection.revision() == revision
    selection.write(None)
    revision = selection.revision()
    await library.refresh()
    assert selection.read() is None
    assert selection.revision() == revision
    await library.sync()
    assert selection.read() == "fixture.pack"


@pytest.mark.asyncio
async def test_close_revokes_memory_access(remote):
    library, _, selection, _ = remote
    await library.sync()
    library.close()
    assert not library.authorize("fixture.pack")
    assert selection.read() == "fixture.pack"
    with pytest.raises(RemoteError, match="closed"):
        await library.sync()
    assert selection.read() == "fixture.pack"


@pytest.mark.asyncio
async def test_cache_directory_symlink_rejected(remote, tmp_path):
    library, _, _, _ = remote
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    library.cache.symlink_to(target, target_is_directory=True)
    with pytest.raises(RemoteError):
        await library.sync()
    assert not list(target.iterdir())


@pytest.mark.asyncio
async def test_failure_to_equip_does_not_grant_offline_access(remote):
    library, api, selection, _ = remote

    async def fail_equip(*args, **kwargs):
        raise RuntimeError("server refused")

    api.equip = fail_equip
    with pytest.raises(RemoteError):
        await library.equip("fixture.pack")
    assert not library.authorize("fixture.pack")
    assert selection.read() is None


@pytest.mark.asyncio
async def test_invalid_duplicate_ownership_fails_closed(remote):
    library, api, _, _ = remote
    api.rows.append(dict(api.rows[0]))
    with pytest.raises(RemoteError, match="Duplicate"):
        await library.sync()


@pytest.mark.asyncio
async def test_missing_available_version_cannot_assume_acquired_version(remote):
    library, api, _, _ = remote
    api.rows[0].pop("available_version")
    with pytest.raises(RemoteError):
        await library.sync()
    assert not library.authorize("fixture.pack")


@pytest.mark.asyncio
async def test_late_refresh_cannot_reopen_closed_library_or_clear_preference(remote):
    library, api, selection, _ = remote
    await library.sync()
    api.block_owned = asyncio.Event()
    task = asyncio.create_task(library.refresh())
    await asyncio.sleep(0)
    library.close()
    api.block_owned.set()
    with pytest.raises(RemoteError):
        await task
    assert not library.authorize("fixture.pack")
    assert selection.read() == "fixture.pack"


@pytest.mark.asyncio
@pytest.mark.parametrize("new_choice", ["different.pack", "fixture.pack", None])
async def test_failure_does_not_clobber_new_selection_from_another_process(remote, new_choice):
    library, api, selection, _ = remote
    await library.sync()
    selection.write(new_choice)
    api.fail = OSError("offline")
    with pytest.raises(RemoteError):
        await library.refresh()
    assert selection.read() == new_choice


@pytest.mark.asyncio
async def test_authorize_during_initial_sync_cannot_cancel_inflight_request(remote):
    library, api, _, _ = remote
    api.block_owned = asyncio.Event()
    task = asyncio.create_task(library.sync())
    await asyncio.sleep(0)
    for _ in range(20):
        assert not library.authorize("fixture.pack")
    api.block_owned.set()
    await task
    assert library.authorize("fixture.pack")


@pytest.mark.asyncio
async def test_owned_refresh_on_new_library_is_read_only_for_saved_preference(remote):
    library, _, selection, _ = remote
    selection.write("fixture.pack")
    revision = selection.revision()
    report = await library.refresh()
    assert report["available"] == []
    assert report["needs_sync"] == ["fixture.pack"]
    assert selection.revision() == revision
    library.close()
    assert selection.revision() == revision


@pytest.mark.asyncio
async def test_successful_refresh_cannot_adopt_newer_preference_for_later_cleanup(
    remote,
):
    library, api, selection, _ = remote
    await library.sync()
    selection.write("different.pack")
    revision = selection.revision()
    await library.refresh()
    assert selection.revision() == revision
    api.fail = OSError("offline")
    with pytest.raises(RemoteError):
        await library.refresh()
    assert selection.read() == "different.pack"
    assert selection.revision() == revision


@pytest.mark.asyncio
@pytest.mark.parametrize("new_choice", ["different.pack", None])
async def test_stale_server_revocation_does_not_overwrite_new_local_choice(remote, new_choice):
    library, api, selection, _ = remote
    await library.sync()
    selection.write(new_choice)
    revision = selection.revision()
    api.rows[0]["effective_status"] = "revoked"
    await library.refresh()
    assert selection.read() == new_choice
    assert selection.revision() == revision


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", [False, True])
async def test_private_catalog_resolution_never_downgrades_to_public_after_expiry(remote, preview):
    library, _, _, clock = remote
    await library.sync()
    base = Catalog(
        [CatalogEntry("fixture.pack", "public-variant", "Public variant", access="free")]
    )
    catalog = library.catalog(base)
    private_name = catalog.resource_for("fixture.pack", preview=preview)
    assert private_name.startswith("remote-")
    clock.advance(LEASE_SECONDS)
    with pytest.raises(RemoteError, match="verification"):
        catalog.resource_for("fixture.pack", preview=preview)
    assert base.resource_for("fixture.pack", preview=True) == "public-variant"


@pytest.mark.asyncio
async def test_close_then_failed_owned_refresh_preserves_saved_preference(remote):
    library, _, selection, _ = remote
    selection.write("fixture.pack")
    await library.refresh()
    library.close()
    with pytest.raises(RemoteError, match="closed"):
        await library.refresh()
    assert selection.read() == "fixture.pack"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slots", [("theme",), ("victory",), ("horse_skin",), ("theme", "victory", "horse_skin")]
)
async def test_mixed_store_library_restores_only_terminal_packs(remote, slots):
    library, api, selection, _ = remote
    for slot in slots:
        api.rows.append(
            {
                "item_id": f"owned-{slot}",
                "slot": slot,
                "effective_status": "active",
                "activatable": True,
                "pack_id": None,
                "available_version": None,
            }
        )
        api.equipped[slot] = f"owned-{slot}"
    result = await library.sync()
    assert result["available"] == result["downloaded"] == ["fixture.pack"]
    assert result["needs_sync"] == []
    assert selection.read() == "fixture.pack"
    assert [call for call in api.calls if isinstance(call, tuple)] == [("pack", "test-companion")]
    assert set(result["owned"]["equipped"]) == {"companion", *slots}
    await library.refresh()
    assert library.authorize("fixture.pack")
    await library.equip(None)
    assert selection.read() is None
    for slot in slots:
        assert api.equipped[slot] == f"owned-{slot}"


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [True, False])
async def test_unknown_store_entitlement_slot_still_fails_closed(remote, active):
    library, api, _, _ = remote
    api.rows.append(
        {
            "item_id": "invalid-slot",
            "slot": "unknown-slot",
            "effective_status": "active" if active else "revoked",
            "activatable": active,
        }
    )
    with pytest.raises(RemoteError, match="slot"):
        await library.sync()
    assert api.calls == ["owned"]
    assert not library.authorize("fixture.pack")


@pytest.mark.asyncio
async def test_unknown_store_equipment_slot_still_fails_closed(remote):
    library, api, _, _ = remote
    api.equipped["unknown-slot"] = "some-item"
    with pytest.raises(RemoteError, match="slot"):
        await library.sync()
    assert not library.authorize("fixture.pack")


def _multiple_packs(api, count):
    base_row, base_bundle = api.rows[0], copy.deepcopy(api.bundle)
    api.rows = [{**base_row, "item_id": f"item-{i}", "pack_id": f"pack-{i}"} for i in range(count)]
    api.equipped = {"companion": "item-0"}

    async def pack(item_id):
        row = next(row for row in api.rows if row["item_id"] == item_id)
        bundle = copy.deepcopy(base_bundle)
        bundle.update(item_id=item_id, pack_id=row["pack_id"])
        bundle["manifest"]["pack_id"] = row["pack_id"]
        return bundle

    api.pack = pack


def _track_decoded(monkeypatch):
    original = remote_module.decode_pack
    references = []

    def decode(*args):
        assert sum(ref() is not None for ref in references) < MAX_PACKS
        pack = original(*args)
        references.append(weakref.ref(pack))
        return pack

    monkeypatch.setattr(remote_module, "decode_pack", decode)
    return references


@pytest.mark.asyncio
async def test_twenty_five_equips_evict_before_decode_keep_new_selection(remote, monkeypatch):
    library, api, selection, _ = remote
    _multiple_packs(api, 25)
    refs = _track_decoded(monkeypatch)
    for i in range(25):
        await library.equip(f"pack-{i}")
        assert len(library._resources) <= MAX_PACKS
        assert sum(ref() is not None for ref in refs) <= MAX_PACKS
        assert selection.read() == f"pack-{i}"
        assert library.authorize(f"pack-{i}")
    assert not library.authorize("pack-0")
    api.rows[20]["effective_status"] = "expired"
    api.rows[21]["effective_status"] = "revoked"
    api.rows = [row for row in api.rows if row["pack_id"] != "pack-22"]
    await library.equip("pack-0")
    assert all(not library.authorize(f"pack-{i}") for i in (20, 21, 22))
    assert selection.read() == "pack-0"


@pytest.mark.asyncio
async def test_resync_releases_previous_decoded_library_before_loading(remote, monkeypatch):
    library, api, _, _ = remote
    _multiple_packs(api, MAX_PACKS)
    refs = _track_decoded(monkeypatch)
    await library.sync()
    await library.sync()
    assert sum(ref() is not None for ref in refs) == MAX_PACKS


@pytest.mark.asyncio
@pytest.mark.parametrize("versions", [25, 100])
async def test_version_and_account_churn_stays_inside_aggregate_cache_quota(remote, versions):
    old, api, selection, clock = remote
    one_size = len(json.dumps(api.bundle, separators=(",", ":")).encode())
    library = RemoteLibrary(
        api,
        old.cache,
        selection,
        clock=clock,
        cache_max_files=5,
        cache_max_bytes=one_size * 3 + 100,
    )
    for i in range(versions):
        version = f"1.0.{i}"
        api.rows[0]["available_version"] = version
        api.bundle["version"] = api.bundle["manifest"]["version"] = version
        if i % 7 == 0:
            api.user = str(uuid4())
            api.fingerprint = (api.backend_scope, api.user)
        await library.sync()
        files = list(library.cache.glob("remote-*.json"))
        assert len(files) <= library.cache_max_files
        assert sum(path.stat().st_size for path in files) <= library.cache_max_bytes
        name = next(iter(library._resources.values())).name + ".json"
        assert (library.cache / name).is_file()
        assert library.authorize("fixture.pack")


def _cache_name(index):
    return f"remote-{index:064x}.json"


def test_cache_file_quota_and_oldest_eviction_preserve_current_artifact(remote):
    library, _, _, _ = remote
    library.cache_max_files = 2
    for i in range(3):
        library._cache_bundle(_cache_name(i), b'"fixture"')
        os.utime(library.cache / _cache_name(i), ns=(i + 1, i + 1))
    assert not (library.cache / _cache_name(0)).exists()
    assert (library.cache / _cache_name(1)).exists()
    assert (library.cache / _cache_name(2)).exists()
    # Writing the oldest surviving artifact still protects it from eviction.
    library.cache_max_files = 1
    library._cache_bundle(_cache_name(1), b'"fixture"')
    assert (library.cache / _cache_name(1)).exists()
    assert not (library.cache / _cache_name(2)).exists()


def test_cache_byte_quota_uses_projected_replacement_size(remote):
    library, _, _, _ = remote
    library.cache_max_bytes = 30
    library._cache_bundle(_cache_name(0), b"x" * 10)
    library._cache_bundle(_cache_name(1), b"x" * 10)
    library._cache_bundle(_cache_name(0), b"x" * 25)
    files = list(library.cache.glob("*.json"))
    assert [path.name for path in files] == [_cache_name(0)]
    assert files[0].stat().st_size == 25
    with pytest.raises(RemoteError, match="limits"):
        library._cache_bundle(_cache_name(2), b"x" * 31)
    assert files[0].read_bytes() == b"x" * 25


@pytest.mark.asyncio
async def test_busy_cache_lock_fails_closed_without_waiting_then_can_retry(remote):
    library, _, _, _ = remote
    with _locked(library.cache):
        with pytest.raises(RemoteError):
            await library.sync()
        assert not library.authorize("fixture.pack")
    await library.sync()
    assert library.authorize("fixture.pack")


def test_hundred_concurrent_cache_publications_use_one_lock_and_obey_quotas(remote):
    library, _, selection, clock = remote
    with _locked(library.cache):
        pass
    barrier = threading.Barrier(4)

    def write_batch(worker):
        local = RemoteLibrary(
            library.api,
            library.cache,
            selection,
            clock=clock,
            cache_max_files=7,
            cache_max_bytes=1024,
        )
        succeeded = 0
        barrier.wait(timeout=2)
        for offset in range(25):
            try:
                local._cache_bundle(_cache_name(worker * 25 + offset), b'"fixture"')
                succeeded += 1
            except BlockingIOError:
                pass
        return succeeded

    with ThreadPoolExecutor(max_workers=4) as executor:
        successes = list(executor.map(write_batch, range(4)))
    assert sum(successes) > 0
    files = list(library.cache.glob("*.json"))
    assert 0 < len(files) <= 7
    assert sum(path.stat().st_size for path in files) <= 1024
    assert {path.name for path in library.cache.iterdir()} == {
        ".lock",
        *(path.name for path in files),
    }
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)


def test_concurrent_first_cache_creation_fails_closed_for_losing_writers(remote, tmp_path):
    library, api, _, clock = remote
    barrier = threading.Barrier(4)

    def restore(worker):
        selection = SelectionStore(tmp_path / f"state-{worker}")
        local = RemoteLibrary(api, library.cache, selection, clock=clock)
        barrier.wait(timeout=2)
        try:
            asyncio.run(local.sync())
            assert local.authorize("fixture.pack")
            return True
        except RemoteError:
            # Creation races and nonblocking flock contention both fail closed.
            assert not local.authorize("fixture.pack")
            return False
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        successes = list(executor.map(restore, range(4)))
    assert any(successes)
    assert len(list(library.cache.glob("*.json"))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["unrelated.txt", ".crashed.tmp", "remote-not-a-hash.json"])
async def test_unexpected_cache_names_rejected_before_any_eviction(remote, name):
    library, _, _, _ = remote
    library._cache_bundle(_cache_name(0), b'"fixture"')
    unrelated = library.cache / name
    unrelated.write_bytes(b"do not delete")
    unrelated.chmod(0o600)
    before = set(library.cache.iterdir())
    library.cache_max_files = 1
    with pytest.raises(RemoteError):
        await library.sync()
    assert set(library.cache.iterdir()) == before
    assert unrelated.read_bytes() == b"do not delete"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "lock-symlink"])
async def test_unsafe_eviction_candidates_never_touch_external_targets(remote, tmp_path, kind):
    library, _, _, _ = remote
    library.cache.mkdir(mode=0o700)
    outside = tmp_path / "outside-cache"
    outside.write_bytes(b"outside data")
    outside.chmod(0o600)
    victim = library.cache / (".lock" if kind == "lock-symlink" else _cache_name(0))
    if kind in {"symlink", "lock-symlink"}:
        victim.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, victim)
    else:
        victim.mkdir(mode=0o700)
    with pytest.raises(RemoteError):
        await library.sync()
    assert outside.read_bytes() == b"outside data"
    assert victim.exists()
    assert not library.authorize("fixture.pack")


@pytest.mark.asyncio
async def test_excessive_scan_fails_closed_without_eviction(remote):
    library, _, _, _ = remote
    library.cache_scan_limit = 5
    library.cache.mkdir(mode=0o700)
    for i in range(5):
        path = library.cache / _cache_name(i)
        path.write_bytes(b'"fixture"')
        path.chmod(0o600)
    with pytest.raises(RemoteError, match="scan"):
        await library.sync()
    assert len(list(library.cache.glob("*.json"))) == 5


@pytest.mark.parametrize(
    "name,value",
    [
        ("cache_max_files", MAX_CACHE_FILES + 1),
        ("cache_max_bytes", MAX_CACHE_BYTES + 1),
        ("cache_scan_limit", MAX_CACHE_SCAN + 1),
    ],
)
def test_cache_limit_injection_can_only_lower_production_ceiling(remote, name, value):
    library, api, selection, _ = remote
    with pytest.raises(ValueError):
        RemoteLibrary(api, library.cache, selection, **{name: value})


@pytest.mark.asyncio
async def test_interrupted_atomic_write_stale_temp_is_recovered(remote):
    library, _, _, _ = remote
    library.cache.mkdir(mode=0o700)
    abandoned = library.cache / ("." + "a" * 32 + ".tmp")
    abandoned.write_bytes(b"interrupted upload data")
    abandoned.chmod(0o600)
    past = time.time() - 65
    os.utime(abandoned, (past, past))
    await library.sync()
    assert not abandoned.exists()
    assert library.authorize("fixture.pack")
    assert len(list(library.cache.glob("*.json"))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["fresh", "symlink", "hardlink", "permissions", "oversized", "unknown"]
)
async def test_unsafe_or_fresh_interrupted_temp_is_not_deleted(remote, tmp_path, kind):
    library, _, _, _ = remote
    library.cache.mkdir(mode=0o700)
    outside = tmp_path / "external-temp-target"
    outside.write_bytes(b"external data")
    outside.chmod(0o600)
    name = ".not-a-uuid.tmp" if kind == "unknown" else "." + "b" * 32 + ".tmp"
    abandoned = library.cache / name
    if kind == "symlink":
        abandoned.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, abandoned)
    else:
        abandoned.write_bytes(b"partial data")
        abandoned.chmod(0o644 if kind == "permissions" else 0o600)
        if kind == "oversized":
            # Sparse file: size guard without allocating a multi-megabyte fixture.
            with abandoned.open("ab") as stream:
                stream.truncate(remote_module.MAX_BUNDLE_BYTES + 1)
    if kind != "fresh":
        past = time.time() - 65
        os.utime(abandoned, (past, past), follow_symlinks=False)
    with pytest.raises(RemoteError):
        await library.sync()
    assert abandoned.exists()
    assert outside.read_bytes() == b"external data"
    assert not library.authorize("fixture.pack")


@pytest.mark.asyncio
async def test_unknown_cache_entry_prevents_even_safe_stale_temp_cleanup(remote):
    library, _, _, _ = remote
    library.cache.mkdir(mode=0o700)
    stale = library.cache / ("." + "c" * 32 + ".tmp")
    stale.write_bytes(b"interrupted")
    stale.chmod(0o600)
    past = time.time() - 65
    os.utime(stale, (past, past))
    unknown = library.cache / "keep-me.txt"
    unknown.write_bytes(b"not a cache entry")
    unknown.chmod(0o600)
    with pytest.raises(RemoteError):
        await library.sync()
    assert stale.exists() and unknown.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["sync", "equip"])
@pytest.mark.parametrize("new_choice", [None, "newer.pack"])
async def test_late_foreground_download_cannot_overwrite_user_choice(remote, method, new_choice):
    library, api, selection, _ = remote
    selection.write("old.pack")
    api.on_pack = lambda: selection.write(new_choice)
    with pytest.raises(RemoteError, match="selection changed"):
        if method == "sync":
            await library.sync()
        else:
            await library.equip("fixture.pack")
    assert selection.read() == new_choice
    assert not library.authorize("fixture.pack")
