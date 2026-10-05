"""Opt-in actual private bytes, synthetic JWT/wallet, real disposable SQL.

No provider calls, native UX certification, or persistent catalog activation.
Private responses must never appear in assertions, logs, or retained artifacts.
"""

import base64
import hashlib
import json
import os
import socket
import stat
import time
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from jose import jwt

from openvegas.emotes.manifest import PackError
from openvegas.emotes.remote import RemoteLibrary
from openvegas.emotes.resources import Catalog, PackRepository
from openvegas.emotes.selection import SelectionStore
from openvegas.emotes.transport import EmoteAPI
from openvegas.store.catalog import STORE_CATALOG, cosmetic_purchasable
from openvegas.store.refunds import refund_cosmetic
from openvegas.store.service import StoreError, StoreService
from openvegas.wallet.ledger import WalletService
from server.routes import store
from tests.integration.test_restoration_db import _user

REPO = Path(__file__).resolve().parents[2]
APPROVAL = REPO / "evidence/emotes/followthrough-20261002/private-provisioning/artwork-approval.json"
PRIVATE_ENV = "OPENVEGAS_APPROVED_PREMIUM_PRIVATE_ROOT"
PACKS = (
    "openvegas.beat-maker", "openvegas.bicycle-finish", "openvegas.pixel-courier",
    "openvegas.skyline-dunk", "openvegas.three-point-glow", "openvegas.visor-explorer",
)
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get(PRIVATE_ENV), reason="Explicit private root required"),
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sealed_inventory(root):
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        assert not path.is_symlink()
        assert info.st_uid == os.getuid()
        assert stat.S_IMODE(info.st_mode) == (0o500 if path.is_dir() else 0o400)
        result[str(path.relative_to(root))] = {
            "mode": oct(stat.S_IMODE(info.st_mode)),
            "sha256": sha(path.read_bytes()) if path.is_file() else None,
            "bytes": info.st_size if path.is_file() else None,
        }
    return result


@pytest.fixture
def candidate(monkeypatch, integration_environment):
    root = Path(os.environ[PRIVATE_ENV]).expanduser()
    assert root.is_absolute() and root.resolve() == root
    assert not root.is_relative_to(REPO)
    before = sealed_inventory(root)
    approval = json.loads(APPROVAL.read_text())
    assert approval["artwork_approved"] is True
    raw = (root / "release-manifest.json").read_bytes()
    assert sha(raw) == approval["release_manifest_sha256"]
    release = json.loads(raw)
    assert release["sale_enabled"] is False
    assert release["native_compatibility_verified"] is False
    assert release["release_approval_required"] is True
    approved = {entry["pack_id"]: entry for entry in approval["packs"]}
    assert set(approved) == set(PACKS)
    assert {entry["pack_id"] for entry in release["packs"]} == set(PACKS)
    assert len(release["packs"]) == 6 and len(before) == 26
    for entry in release["packs"]:
        for name, digest in approved[entry["pack_id"]]["hashes"].items():
            info = before[entry["delivery_resource"] + "/" + name]
            assert info["sha256"] == digest == entry["files"][name]["sha256"]
            assert info["bytes"] == entry["files"][name]["bytes"]

    monkeypatch.setenv("OPENVEGAS_EMOTE_PACK_ROOT", str(root))
    monkeypatch.setenv("OPENVEGAS_EMOTE_RELEASE_SHA256", approval["release_manifest_sha256"])
    # Only this test cluster's socket may connect; HTTP stays inside ASGITransport.
    port = urlsplit(integration_environment).port
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex

    def guard(sock, address, *, method=original):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and address != ("127.0.0.1", port):
            raise AssertionError("Non-disposable-PostgreSQL connection forbidden")
        return method(sock, address)

    def deny_http(*args, **kwargs):
        raise AssertionError("Real HTTP transport forbidden")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda s, a: guard(s, a, method=original_ex))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_http)
    try:
        yield root, release, before
    finally:
        assert sealed_inventory(root) == before


def private_response(response, status):
    assert response.status_code == status
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Authorization"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "etag" not in response.headers


def verify_payload(payload, root, entry):
    assert payload["pack_id"] == entry["pack_id"]
    assert payload["version"] == entry["version"]
    sheet_hash = sha(base64.b64decode(payload["sheet_base64"], validate=True))
    assert sheet_hash == entry["files"]["sheet.png"]["sha256"]
    # JSON transport preserves semantic manifest identity, not source whitespace.
    expected = json.loads((root / entry["delivery_resource"] / "manifest.json").read_bytes())
    identical = payload["manifest"] == expected
    assert identical, "Delivered manifest differs from sealed manifest"


@pytest.mark.parametrize("pack_id", PACKS)
async def test_actual_premium_purchase_restore_refund(
    candidate, database_factory, monkeypatch, tmp_path, pack_id,
):
    root, release, inventory = candidate
    entry = next(item for item in release["packs"] if item["pack_id"] == pack_id)
    original_item = deepcopy(STORE_CATALOG[pack_id])
    assert not cosmetic_purchasable(original_item)
    slot = entry["slot"]
    secret = "synthetic-local-premium-acceptance-" + uuid4().hex
    monkeypatch.setenv("SUPABASE_JWT_SECRET", secret)

    def token(user):
        return jwt.encode(
            {"sub": user, "aud": "authenticated", "exp": int(time.time()) + 600},
            secret, algorithm="HS256",
        )

    async with database_factory() as sandbox:
        db = sandbox.db
        user, other = str(await _user(db)), str(await _user(db))
        wallet = WalletService(db)
        await wallet.mint(f"user:{user}", Decimal(1000), f"synthetic-premium:{user}")
        service = StoreService(db, wallet)
        monkeypatch.setattr(store, "get_store_service", lambda: service)
        app = FastAPI()
        app.include_router(store.router)
        headers = {"Authorization": "Bearer " + token(user)}
        other_headers = {"Authorization": "Bearer " + token(other)}
        path = f"/store/emotes/{pack_id}/pack"
        buy = {"item_id": pack_id, "idempotency_key": "actual-premium-once"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
        ) as http:
            assert (await http.post("/store/buy", json=buy)).status_code == 401
            private_response(await http.get(path), 401)
            private_response(await http.get(path, headers=other_headers), 403)
            assert (await http.post("/store/buy", headers=headers, json=buy)).status_code == 409
            assert await db.fetchval("SELECT count(*) FROM store_orders") == 0

            # Test-process catalog only: these flags are prerequisites, not certification.
            item = deepcopy(original_item)
            item.update(approval_status="approved", sale_enabled=True,
                        artwork_approved=True, native_compatibility_verified=True)
            item["asset"] = {
                "pack_id": pack_id, "version": entry["version"],
                "delivery_resource": entry["delivery_resource"],
            }
            monkeypatch.setitem(STORE_CATALOG, pack_id, item)
            response = await http.post("/store/buy", headers=headers, json=buy)
            assert response.status_code == 200
            bought = response.json()
            assert bought["status"] == "fulfilled"
            assert Decimal(bought["cost_v"]) == 500
            order = bought["order_id"]
            replay = await http.post("/store/buy", headers=headers, json=buy)
            assert replay.status_code == 200 and replay.json()["replayed"] is True
            assert replay.json()["order_id"] == order
            assert await wallet.get_balance(f"user:{user}") == 500
            assert await db.fetchval("SELECT count(*) FROM ledger_entries WHERE entry_type='redeem'") == 1
            owned_response = await http.get("/store/emotes/owned", headers=headers)
            private_response(owned_response, 200)
            owned = owned_response.json()
            assert owned["account_id"] == user and len(owned["entitlements"]) == 1
            entitlement = owned["entitlements"][0]
            assert entitlement["pack_id"] == pack_id and entitlement["acquired_version"] == "1.0.0"
            assert entitlement["available_version"] == "1.0.0"
            assert entitlement["source_order_id"] == order and entitlement["activatable"] is True
            downloaded = await http.get(path, headers=headers)
            private_response(downloaded, 200)
            verify_payload(downloaded.json(), root, entry)

            api = EmoteAPI(credential_reader=lambda: ("http://127.0.0.1", user, token(user)), http=http)
            state = SelectionStore(tmp_path / "device-one")
            library = RemoteLibrary(api, tmp_path / "cache-one", state)
            try:
                await library.equip(pack_id, slot=slot)
                assert state.read_slots()[slot] == pack_id
                assert library.authorize(pack_id)
            finally:
                library.close()

            db = await sandbox.reconnect()
            wallet = WalletService(db)
            service = StoreService(db, wallet)
            # Existing ownership must restore even while new sales are disabled.
            monkeypatch.setitem(item, "sale_enabled", False)
            fresh_state = SelectionStore(tmp_path / "device-two")
            restored = RemoteLibrary(api, tmp_path / "cache-two", fresh_state)
            try:
                result = await restored.sync()
                assert result["available"] == [pack_id] and result["downloaded"] == [pack_id]
                assert fresh_state.read_slots()[slot] == pack_id
                resource = restored.catalog(Catalog()).resource_for(pack_id)
                loaded = restored.repository(PackRepository()).load(resource)
                assert loaded.manifest.pack_id == pack_id and loaded.manifest.version == "1.0.0"
                cached = list((tmp_path / "cache-two").glob("remote-*.json"))
                assert len(cached) == 1
                verify_payload(json.loads(cached[0].read_bytes()), root, entry)
                private_response(await http.get(path, headers=other_headers), 403)
                assert (await http.post("/store/emotes/equip", headers=other_headers,
                                        json={"item_id": pack_id, "slot": slot})).status_code == 403
                assert (await http.get("/store/emotes/owned", headers=other_headers)).json()["entitlements"] == []
                with pytest.raises(StoreError, match="NOT_FOUND"):
                    await refund_cosmetic(db, user_id=other, order_id=order,
                                          operator_id=str(uuid4()), reason="customer_request")
                # Refund must reverse the original debit, never a revised price.
                monkeypatch.setitem(item, "price_usd", Decimal(99))
                refund = await refund_cosmetic(db, user_id=user, order_id=order,
                                              operator_id=str(uuid4()), reason="customer_request")
                assert refund["replayed"] is False and Decimal(refund["cost_v"]) == 500
                assert await wallet.get_balance(f"user:{user}") == 1000
                assert await db.fetchval("SELECT count(*) FROM cosmetic_equipment") == 0
                assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "revoked"
                assert await db.fetchval("SELECT status FROM store_orders") == "reversed"
                assert await db.fetchval("SELECT count(*) FROM cosmetic_entitlement_events") == 2
                private_response(await http.get(path, headers=headers), 403)
                assert (await http.post("/store/emotes/equip", headers=headers,
                                        json={"item_id": pack_id, "slot": slot})).status_code == 403
                await restored.refresh()
                assert not restored.authorize(pack_id)
                assert fresh_state.read_slots()[slot] is None
                with pytest.raises(PackError):
                    restored.repository(PackRepository()).load(resource)
                assert (await restored.sync())["available"] == []
                assert cached[0].exists(), "Retained cached bytes must not grant entitlement"
            finally:
                restored.close()

            db = await sandbox.reconnect()
            service = StoreService(db, WalletService(db))
            replay = await refund_cosmetic(db, user_id=user, order_id=order,
                                          operator_id=str(uuid4()), reason="customer_request")
            assert replay["replayed"] is True
            assert await WalletService(db).get_balance(f"user:{user}") == 1000
            assert await db.fetchval("SELECT count(*) FROM ledger_entries WHERE entry_type='store_refund'") == 1
            assert await db.fetchval("SELECT sum(balance) FROM wallet_accounts") == 0
            assert await db.fetchval("SELECT count(*) FROM fiat_topups") == 0
            assert await db.fetchval("SELECT count(*) FROM stripe_webhook_events") == 0
            private_response(await http.get(path, headers=headers), 403)
        monkeypatch.setitem(STORE_CATALOG, pack_id, original_item)
        assert not cosmetic_purchasable(STORE_CATALOG[pack_id])

    report_dir = os.environ.get("OPENVEGAS_PREMIUM_REPORT_DIR")
    if report_dir:
        destination = Path(report_dir).resolve()
        assert destination.is_relative_to(REPO / "evidence/emotes/remaining38/premium")
        (destination / (pack_id + ".json")).write_text(json.dumps({
            "status": "passed", "pack_id": pack_id, "slot": slot,
            "release_sha256": inventory["release-manifest.json"]["sha256"],
            "files": entry["files"], "debit_v": "500", "refund_v": "500",
            "criteria": [
                "sealed_files_match_explicit_artwork_approval",
                "unmodified_catalog_purchase_denied",
                "local_hs256_authenticated_purchase_and_single_debit",
                "owned_entitlement_bound_to_order_pack_version",
                "private_download_exact_png_and_semantic_manifest",
                "anonymous_and_other_account_denied",
                "product_client_equip_and_new_device_restore_after_sql_reconnect",
                "restore_with_sales_disabled_and_exact_cached_png",
                "foreign_refund_denied_original_debit_refunded_once",
                "revoked_order_equipment_and_client_authorization",
                "cached_bytes_do_not_reauthorize_after_revocation",
                "refund_replay_after_reconnect_wallet_conservation",
                "no_fiat_topups_or_stripe_webhook_events",
            ],
            "native_certification": False, "sales_activation": False,
            "new_stripe_proof": False,
        }, indent=2) + "\n")
