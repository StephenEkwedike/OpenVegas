"""Private delivery contracts; synthetic ownership/art, no credentials or network."""

import asyncio
import base64
import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jose import jwt

import server.routes.store as routes
from openvegas.emotes import manifest
from openvegas.store.catalog import STORE_CATALOG
from openvegas.store.service import EntitlementDenied
from server.middleware import auth
from server.services import emote_delivery as delivery

SKU = "test_emote"
PACK_PATH = f"/store/emotes/{SKU}/pack"


def credentials(user="alice", *, expired=False):
    expires = datetime.now(UTC) + timedelta(minutes=-1 if expired else 10)
    token = jwt.encode(
        {"sub": user, "aud": "authenticated", "exp": expires},
        "local-unit-delivery-signature-only",
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def assert_private(response):
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["vary"] == "Authorization"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "etag" not in response.headers


@pytest.fixture
def pack_files(tmp_path, approved, monkeypatch):
    # Copy public art ONLY into a temporary synthetic SKU. No real catalog changes.
    public = Path(manifest.__file__).parent / "assets" / "pixel-courier"
    raw = json.loads((public / "manifest.json").read_bytes())
    raw.update(pack_id=approved["asset"]["pack_id"], version=approved["asset"]["version"])
    sheet = (public / "sheet.png").read_bytes()
    root = tmp_path.resolve() / "private-packs"
    target = root / "test-private-delivery"
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    (target / "sheet.png").write_bytes(sheet)
    approved["asset"]["delivery_resource"] = target.name
    monkeypatch.setenv("OPENVEGAS_EMOTE_PACK_ROOT", str(root))
    return target, raw, sheet


@pytest.fixture
def client(monkeypatch, service, pack_files):
    app = FastAPI()
    app.include_router(routes.router)
    monkeypatch.setattr(routes, "get_store_service", lambda: service)
    monkeypatch.setattr(auth, "_supabase_jwt_secret", lambda: "local-unit-delivery-signature-only")

    async def reject_fallback(token):
        raise HTTPException(401, "Invalid or expired token")

    monkeypatch.setattr(auth, "_validate_with_supabase", reject_fallback)
    with TestClient(app) as result:
        yield result


@pytest.fixture
def owned(client):
    result = client.post(
        "/store/buy",
        headers=credentials(),
        json={"item_id": SKU, "idempotency_key": "local-delivery-fixture"},
    )
    assert result.status_code == 200
    return client


def test_private_pack_is_strict_validated_bounded_and_matches_exact_bytes(owned, pack_files):
    _, raw, sheet = pack_files
    response = owned.get(PACK_PATH, headers=credentials())
    assert response.status_code == 200
    assert_private(response)
    payload = response.json()
    assert set(payload) == {
        "schema_version",
        "item_id",
        "pack_id",
        "version",
        "manifest",
        "sheet_base64",
    }
    assert payload["schema_version"] == 1
    assert payload["item_id"] == SKU
    assert payload["pack_id"] == raw["pack_id"]
    assert payload["version"] == raw["version"]
    assert payload["manifest"] == raw
    assert payload["sheet_base64"].isascii()
    decoded = base64.b64decode(payload["sheet_base64"], validate=True)
    assert decoded == sheet
    assert hashlib.sha256(decoded).hexdigest() == raw["sha256"]
    loaded = manifest.decode_pack(manifest.validate_manifest(payload["manifest"]), decoded)
    assert loaded.frame(0).size == (64, 80)
    assert len(decoded) <= delivery.MAX_PRIVATE_SHEET_BYTES
    assert len(json.dumps(raw).encode()) <= delivery.MAX_PRIVATE_MANIFEST_BYTES
    assert len(response.content) < 3 * 1024 * 1024
    for forbidden in (
        "delivery_resource",
        "private_storage_key",
        "MUST_NOT_LEAK",
        str(pack_files[0]),
    ):
        assert forbidden not in response.text


def test_validation_uses_load_pack_on_exact_bounded_snapshot(owned, pack_files, monkeypatch):
    original = manifest.load_pack
    seen = []

    def validate(snapshot):
        assert snapshot != pack_files[0]
        assert (snapshot / "sheet.png").read_bytes() == pack_files[2]
        seen.append(snapshot)
        return original(snapshot)

    monkeypatch.setattr(manifest, "load_pack", validate)
    assert owned.get(PACK_PATH, headers=credentials()).status_code == 200
    assert len(seen) == 1
    assert not seen[0].exists()


def test_snapshot_validation_cannot_be_swapped_for_unvalidated_source_bytes(
    owned,
    pack_files,
    monkeypatch,
):
    original = manifest.load_pack

    def validate(snapshot):
        # An operator replaces the source while validation runs. Delivery must
        # still use the pinned bounded snapshot, not reopen the changed source.
        (pack_files[0] / "sheet.png").write_bytes(b"replaced after bounded read")
        return original(snapshot)

    monkeypatch.setattr(manifest, "load_pack", validate)
    response = owned.get(PACK_PATH, headers=credentials())
    assert response.status_code == 200
    assert base64.b64decode(response.json()["sheet_base64"], validate=True) == pack_files[2]
    assert owned.get(PACK_PATH, headers=credentials()).status_code == 503


def test_valid_nested_sheet_path_is_supported(owned, pack_files):
    target, raw, sheet = pack_files
    (target / "frames").mkdir()
    (target / "sheet.png").rename(target / "frames/dance.png")
    raw["sheet"] = "frames/dance.png"
    (target / "manifest.json").write_text(json.dumps(raw))
    response = owned.get(PACK_PATH, headers=credentials())
    assert response.status_code == 200
    assert response.json()["manifest"]["sheet"] == "frames/dance.png"
    assert base64.b64decode(response.json()["sheet_base64"], validate=True) == sheet


def test_owned_has_authenticated_account_id_and_preserves_previous_shape(owned):
    alice = owned.get("/store/emotes/owned?account_id=bob", headers=credentials())
    assert alice.status_code == 200
    assert_private(alice)
    payload = alice.json()
    assert set(payload) == {"account_id", "entitlements", "equipped"}
    assert payload["account_id"] == "alice"
    assert len(payload["entitlements"]) == 1
    assert payload["entitlements"][0]["activatable"] is True
    assert payload["entitlements"][0]["available_version"] == "1.0.0"
    assert payload["equipped"] == {}
    assert owned.get("/store/emotes/owned", headers=credentials("bob")).json() == {
        "account_id": "bob",
        "entitlements": [],
        "equipped": {},
    }


@pytest.mark.parametrize("path", [PACK_PATH, "/store/emotes/owned"])
@pytest.mark.parametrize("token", ["missing", "invalid", "expired"])
def test_private_endpoints_fail_closed_auth_and_never_cache(client, path, token):
    headers = {
        "missing": {},
        "invalid": {"Authorization": "Bearer invalid-local-token"},
        "expired": credentials(expired=True),
    }[token]
    response = client.get(path, headers={**headers, "X-Admin-Preview": "true"})
    assert response.status_code == 401
    assert_private(response)


@pytest.mark.parametrize("sku", [SKU, "unknown-pack", "openvegas.pixel-courier"])
def test_cross_user_and_unowned_packs_do_not_touch_files(owned, monkeypatch, sku):
    def forbidden(*args):
        pytest.fail("Must authorize ownership before inspecting files or root configuration")

    monkeypatch.setattr(routes, "load_delivery_pack", forbidden)
    monkeypatch.delenv("OPENVEGAS_EMOTE_PACK_ROOT")
    response = owned.get(
        f"/store/emotes/{sku}/pack?account_id=alice&preview=true",
        headers={**credentials("bob"), "X-Admin-Preview": "true"},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "COSMETIC_NOT_OWNED_OR_REVOKED"}
    assert_private(response)


@pytest.mark.parametrize("state,code", [("revoked", 403), ("expired", 410), ("reversed", 403)])
def test_revoked_expired_and_reversed_ownership_denied_before_file_read(
    owned, service, monkeypatch, state, code
):
    entitlement = service.db.state["entitlements"]["alice", SKU]
    if state == "expired":
        entitlement["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    elif state == "reversed":
        service.db.state["orders"][entitlement["source_order_id"]]["status"] = "reversed"
    else:
        entitlement["status"] = "revoked"

    def forbidden(*args):
        pytest.fail("Inactive ownership must not read private files")

    monkeypatch.setattr(routes, "load_delivery_pack", forbidden)
    response = owned.get(PACK_PATH, headers=credentials())
    assert response.status_code == code
    assert_private(response)


@pytest.mark.parametrize("change", ["withdrawn", "deleted", "identity", "slot", "type"])
def test_only_approved_equippable_catalog_pack_can_deliver(owned, approved, monkeypatch, change):
    if change == "deleted":
        monkeypatch.delitem(STORE_CATALOG, SKU)
    elif change == "identity":
        approved["asset"]["pack_id"] = "different.identity"
    else:
        key, value = {
            "withdrawn": ("approval_status", "pending"),
            "slot": ("slot", "companion"),
            "type": ("type", "ai_pack"),
        }[change]
        approved[key] = value

    def forbidden(*args):
        pytest.fail("Unavailable catalog entry must not read private files")

    monkeypatch.setattr(routes, "load_delivery_pack", forbidden)
    response = owned.get(PACK_PATH, headers=credentials())
    assert response.status_code == 409
    assert response.json() == {"detail": "COSMETIC_NOT_EQUIPPABLE"}
    assert_private(response)


def test_sales_can_remain_disabled_while_existing_owner_restores(owned, approved):
    approved["sale_enabled"] = False
    assert owned.get(PACK_PATH, headers=credentials()).status_code == 200


@pytest.mark.parametrize("change", ["withdrawn", "deleted", "expired", "revoked"])
def test_owned_activation_metadata_is_fail_closed(owned, service, approved, monkeypatch, change):
    if change == "withdrawn":
        approved["approval_status"] = "pending"
    elif change == "deleted":
        monkeypatch.delitem(STORE_CATALOG, SKU)
    elif change == "expired":
        service.db.state["entitlements"]["alice", SKU]["expires_at"] = datetime.now(
            UTC
        ) - timedelta(seconds=1)
    else:
        service.db.state["entitlements"]["alice", SKU]["status"] = "revoked"
    response = owned.get("/store/emotes/owned", headers=credentials())
    entry = response.json()["entitlements"][0]
    assert entry["activatable"] is False
    assert entry["available_version"] is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "relative/path",
        "/missing-private-fixture",
        "https://example.invalid/packs",
    ],
)
def test_missing_or_invalid_root_configuration_fails_closed(owned, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("OPENVEGAS_EMOTE_PACK_ROOT")
    else:
        monkeypatch.setenv("OPENVEGAS_EMOTE_PACK_ROOT", value)
    response = owned.get(PACK_PATH, headers=credentials())
    assert response.status_code == 503
    assert response.json() == {"detail": "COSMETIC_DELIVERY_UNAVAILABLE"}
    assert_private(response)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "..",
        "../escape",
        "/absolute",
        "https://example.invalid/a",
        "two/parts",
        "a\\b",
        "a..b",
        "x" * 97,
    ],
)
def test_delivery_resource_must_be_single_operator_slug(owned, approved, value):
    approved["asset"]["delivery_resource"] = value
    response = owned.get(PACK_PATH, headers=credentials())
    assert response.status_code == 503
    assert response.json() == {"detail": "COSMETIC_DELIVERY_UNAVAILABLE"}


@pytest.mark.parametrize(
    "prefix",
    [
        "ui/assets/emotes",
        "public/emotes",
        "openvegas/emotes/assets",
        "UI/ASSETS",
        "PUBLIC",
    ],
)
def test_public_and_bundled_directories_are_never_private_sources(
    owned, pack_files, tmp_path, monkeypatch, prefix
):
    root = tmp_path.resolve() / prefix
    target = root / pack_files[0].name
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(json.dumps(pack_files[1]))
    (target / "sheet.png").write_bytes(pack_files[2])
    monkeypatch.setenv("OPENVEGAS_EMOTE_PACK_ROOT", str(root))
    assert owned.get(PACK_PATH, headers=credentials()).status_code == 503


@pytest.mark.parametrize(
    "attack",
    [
        "root-symlink",
        "ancestor-symlink",
        "resource-symlink",
        "manifest-symlink",
        "sheet-symlink",
        "nested-symlink",
        "manifest-fifo",
        "sheet-fifo",
        "manifest-directory",
        "sheet-directory",
        "missing-manifest",
        "missing-sheet",
        "missing-resource",
    ],
)
def test_symlinks_special_files_and_missing_assets_are_rejected(
    owned, pack_files, monkeypatch, attack
):
    target, raw, _ = pack_files
    root = target.parent
    if attack in {"root-symlink", "ancestor-symlink"}:
        link = root.parent / "link"
        if attack == "root-symlink":
            link.symlink_to(root, target_is_directory=True)
            configured = link
        else:
            link.symlink_to(root.parent, target_is_directory=True)
            configured = link / root.name
        monkeypatch.setenv("OPENVEGAS_EMOTE_PACK_ROOT", str(configured))
    elif attack == "resource-symlink":
        moved = root / "moved"
        target.rename(moved)
        target.symlink_to(moved, target_is_directory=True)
    elif attack == "nested-symlink":
        (target / "nested").symlink_to(target, target_is_directory=True)
        raw["sheet"] = "nested/sheet.png"
        (target / "manifest.json").write_text(json.dumps(raw))
    elif attack == "missing-resource":
        target.rename(root / "removed")
    else:
        field = "manifest.json" if "manifest" in attack else "sheet.png"
        source = target / field
        if "symlink" in attack:
            moved = root / ("moved-" + field)
            source.rename(moved)
            source.symlink_to(moved)
        else:
            source.unlink()
            if "fifo" in attack:
                os.mkfifo(source)
            elif "directory" in attack:
                source.mkdir()
    response = owned.get(PACK_PATH, headers=credentials())
    assert response.status_code == 503
    assert response.json() == {"detail": "COSMETIC_DELIVERY_UNAVAILABLE"}
    assert_private(response)


@pytest.mark.parametrize(
    "attack",
    [
        "manifest-oversize",
        "sheet-oversize",
        "manifest-empty",
        "sheet-empty",
        "duplicate-key",
        "invalid-json",
        "unknown-field",
        "traversal-sheet",
        "absolute-sheet",
        "invalid-sha",
        "identity",
        "version",
        "bad-frame",
        "bad-png-with-valid-sha",
    ],
)
def test_manifest_sheet_bounds_identity_and_hash_enforced(owned, pack_files, monkeypatch, attack):
    target, raw, _ = pack_files
    if attack in {"manifest-oversize", "sheet-oversize"}:
        field = "manifest.json" if attack.startswith("manifest") else "sheet.png"
        limit = (
            delivery.MAX_PRIVATE_MANIFEST_BYTES
            if field == "manifest.json"
            else delivery.MAX_PRIVATE_SHEET_BYTES
        )
        with (target / field).open("wb") as output:
            output.truncate(limit + 1)

        def forbidden(*args):
            pytest.fail("Oversize input must be rejected before PNG loading")

        monkeypatch.setattr(manifest, "load_pack", forbidden)
    elif attack.endswith("empty"):
        (target / ("manifest.json" if attack.startswith("manifest") else "sheet.png")).write_bytes(
            b""
        )
    elif attack == "duplicate-key":
        (target / "manifest.json").write_text('{"version":"0",' + json.dumps(raw)[1:])
    elif attack == "invalid-json":
        (target / "manifest.json").write_bytes(b"\xffnot-json")
    else:
        if attack == "bad-frame":
            raw["animations"]["waiting"]["frames"] = [4095]
        elif attack == "bad-png-with-valid-sha":
            bad = b"not a PNG"
            (target / "sheet.png").write_bytes(bad)
            raw["sha256"] = hashlib.sha256(bad).hexdigest()
        else:
            key, value = {
                "unknown-field": ("private_path", "MUST_NOT_LEAK"),
                "traversal-sheet": ("sheet", "../sheet.png"),
                "absolute-sheet": ("sheet", "/private/sheet.png"),
                "invalid-sha": ("sha256", "0" * 64),
                "identity": ("pack_id", "wrong.identity"),
                "version": ("version", "9.0.0"),
            }[attack]
            raw[key] = value
        (target / "manifest.json").write_text(json.dumps(raw))
    response = owned.get(PACK_PATH, headers=credentials())
    assert response.status_code == 503
    assert response.json() == {"detail": "COSMETIC_DELIVERY_UNAVAILABLE"}
    assert_private(response)


def test_catalog_upgrade_delivers_current_validated_version_not_stale_acquisition(
    owned, approved, pack_files
):
    target, raw, _ = pack_files
    approved["asset"]["version"] = raw["version"] = "2.0.0"
    (target / "manifest.json").write_text(json.dumps(raw))
    response = owned.get(PACK_PATH, headers=credentials())
    assert response.status_code == 200
    assert response.json()["version"] == "2.0.0"
    entry = owned.get("/store/emotes/owned", headers=credentials()).json()["entitlements"][0]
    assert entry["activatable"] is True
    assert entry["available_version"] == "2.0.0"
    assert entry["acquired_version"] == "1.0.0"


def test_private_paths_and_raw_filesystem_errors_never_leak(owned, monkeypatch):
    def denied(*args):
        raise PermissionError("/operator/secret/premium-path MUST_NOT_LEAK")

    monkeypatch.setattr(delivery, "_private_location", denied)
    response = owned.get(PACK_PATH, headers=credentials())
    assert response.status_code == 503
    assert response.json() == {"detail": "COSMETIC_DELIVERY_UNAVAILABLE"}
    assert_private(response)


def test_no_public_catalog_response_exposes_delivery_resource(owned):
    for path in (
        "/store/emotes/catalog",
        f"/store/emotes/catalog/{SKU}/preview",
        "/store/list",
    ):
        response = owned.get(path, headers=credentials())
        assert response.status_code == 200
        assert "delivery_resource" not in response.text
        assert "test-private-delivery" not in response.text


def test_actual_preview_skus_remain_unapproved_and_unsellable(owned):
    for slug in ("pixel-courier", "beat-maker", "visor-explorer"):
        item = STORE_CATALOG[f"openvegas.{slug}"]
        assert item["approval_status"] == "concept_approved"
        assert item["sale_enabled"] is False
        assert item["price_usd"] == Decimal("5.00")
        assert item["cost_v"] == Decimal("500.00")
        assert "delivery_resource" not in item["asset"]
        assert (
            owned.get(f"/store/emotes/openvegas.{slug}/pack", headers=credentials()).status_code
            == 403
        )


def test_transaction_spans_offloaded_validation_then_releases_locks(owned, service, monkeypatch):
    original = routes.load_delivery_pack
    seen = []

    def validate(*args):
        assert any(lock.locked() for lock in service.db.locks.values())
        seen.append(True)
        return original(*args)

    monkeypatch.setattr(routes, "load_delivery_pack", validate)
    before = len(service.db.calls)
    assert owned.get(PACK_PATH, headers=credentials()).status_code == 200
    assert seen == [True]
    assert not any(lock.locked() for lock in service.db.locks.values())
    queries = [query for query, _ in service.db.calls[before:]]
    assert "pg_advisory_xact_lock" in queries[0]
    checks = [query for query in queries if "FROM cosmetic_entitlements e" in query]
    assert len(checks) == 2
    assert all("FOR UPDATE OF e, o" in query for query in checks)


@pytest.mark.parametrize("change,code", [("expire", 410), ("withdraw", 409), ("version", 409)])
def test_expiration_or_withdrawal_during_validation_prevents_response(
    owned, service, approved, monkeypatch, change, code
):
    original = routes.load_delivery_pack

    def validate(*args):
        result = original(*args)
        if change == "expire":
            service.db.state["entitlements"]["alice", SKU]["expires_at"] = datetime.now(
                UTC
            ) - timedelta(seconds=1)
        elif change == "withdraw":
            approved["approval_status"] = "pending"
        else:
            approved["asset"]["version"] = "2.0.0"
        return result

    monkeypatch.setattr(routes, "load_delivery_pack", validate)
    response = owned.get(PACK_PATH, headers=credentials())
    assert response.status_code == code
    assert "sheet_base64" not in response.text
    assert not any(lock.locked() for lock in service.db.locks.values())
    assert_private(response)


@pytest.mark.asyncio
async def test_delivery_lock_released_on_cancellation(service, approved):
    await service.buy("alice", SKU, "local-cancel-test")
    entered = asyncio.Event()

    async def deliver():
        async with service.delivery_asset("alice", SKU):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(deliver())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not any(lock.locked() for lock in service.db.locks.values())
    # A failed attempt cannot poison the next ownership check.
    async with service.delivery_asset("alice", SKU) as asset:
        assert asset["pack_id"] == approved["asset"]["pack_id"]
    with pytest.raises(EntitlementDenied):
        async with service.delivery_asset("bob", SKU):
            pytest.fail("Cross-account delivery must never yield assets")


@pytest.mark.asyncio
async def test_file_validation_is_not_run_on_the_request_event_loop(monkeypatch):
    loop = asyncio.get_running_loop()

    class Service:
        @asynccontextmanager
        async def delivery_asset(self, user, item):
            yield {}

    def load(item, asset):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return {"item_id": item}

    monkeypatch.setattr(routes, "get_store_service", Service)
    monkeypatch.setattr(routes, "load_delivery_pack", load)
    assert await routes.cosmetic_pack(SKU, {"user_id": "alice"}) == {"item_id": SKU}
    assert asyncio.get_running_loop() is loop


@pytest.mark.parametrize("slot", ["companion", "completion"])
@pytest.mark.parametrize(
    "fault",
    [
        "missing-resource",
        "missing-root",
        "missing-sheet",
        "bad-manifest",
        "bad-hash",
        "identity",
        "public-root",
    ],
)
def test_new_purchase_preflight_prevents_any_charge(
    client, service, approved, pack_files, monkeypatch, slot, fault
):
    target, raw, _ = pack_files
    approved["slot"] = slot
    if fault == "missing-resource":
        approved["asset"].pop("delivery_resource")
    elif fault == "missing-root":
        monkeypatch.delenv("OPENVEGAS_EMOTE_PACK_ROOT")
    elif fault == "missing-sheet":
        (target / "sheet.png").unlink()
    elif fault == "bad-manifest":
        (target / "manifest.json").write_bytes(b"invalid fixture")
    elif fault == "bad-hash":
        (target / "sheet.png").write_bytes(b"corrupted fixture")
    elif fault == "identity":
        raw["pack_id"] = "wrong.identity"
        (target / "manifest.json").write_text(json.dumps(raw))
    else:
        monkeypatch.setenv("OPENVEGAS_EMOTE_PACK_ROOT", str(target.parent / "public"))
    response = client.post(
        "/store/buy",
        headers=credentials(),
        json={"item_id": SKU, "idempotency_key": "preflight-no-charge"},
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "COSMETIC_DELIVERY_UNAVAILABLE"}
    assert service.db.state["debits"] == []
    assert service.db.state["orders"] == {}
    assert service.db.state["entitlements"] == {}
    assert service.db.state["balances"] == {}
    assert not any(query.startswith("INSERT") for query, _ in service.db.calls)


@pytest.mark.parametrize("same_key", [True, False])
def test_existing_order_replay_works_without_files_and_never_recharges(
    owned, service, approved, monkeypatch, same_key
):
    original_id = next(iter(service.db.state["orders"]))
    approved["asset"].pop("delivery_resource")
    monkeypatch.delenv("OPENVEGAS_EMOTE_PACK_ROOT")

    def forbidden(*args):
        pytest.fail("Replays must precede delivery preflight")

    monkeypatch.setattr(delivery, "load_delivery_pack", forbidden)
    response = owned.post(
        "/store/buy",
        headers=credentials(),
        json={
            "item_id": SKU,
            "idempotency_key": "local-delivery-fixture" if same_key else "new-alias",
        },
    )
    assert response.status_code == 200
    assert response.json()["order_id"] == original_id
    assert response.json()["replayed"] if same_key else response.json()["already_owned"]
    assert len(service.db.state["debits"]) == 1
    assert len(service.db.state["orders"]) == 1


def test_legacy_reconciliation_precedes_delivery_preflight(owned, service, monkeypatch):
    service.db.state["entitlements"].clear()
    monkeypatch.delenv("OPENVEGAS_EMOTE_PACK_ROOT")
    response = owned.post(
        "/store/buy",
        headers=credentials(),
        json={"item_id": SKU, "idempotency_key": "new-legacy-attempt"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "COSMETIC_LEGACY_ORDER_REQUIRES_RECONCILIATION"
    replay = owned.post(
        "/store/buy",
        headers=credentials(),
        json={"item_id": SKU, "idempotency_key": "local-delivery-fixture"},
    )
    assert replay.status_code == 200
    assert replay.json()["entitlement"] is None
    assert len(service.db.state["debits"]) == 1


def test_catalog_change_during_preflight_cannot_charge(client, service, approved, monkeypatch):
    original = delivery.load_delivery_pack

    def validate(*args):
        result = original(*args)
        approved["sale_enabled"] = False
        return result

    monkeypatch.setattr(delivery, "load_delivery_pack", validate)
    response = client.post("/store/buy", headers=credentials(), json={"item_id": SKU})
    assert response.status_code == 409
    assert service.db.state["debits"] == []
    assert service.db.state["orders"] == {}
