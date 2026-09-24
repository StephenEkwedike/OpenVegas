"""Shared isolated store fixtures."""

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from openvegas.emotes import manifest
from openvegas.store.catalog import STORE_CATALOG
from openvegas.store.service import StoreService
from tests.emote_release_fixture import pin_delivery_release
from tests.test_store.fakes import StoreFakeDB, StoreFakeTx, StoreFakeWallet


@pytest.fixture(autouse=True)
def isolated_payment_policy_boundary(monkeypatch):
    """Store-only fakes do not model Stripe history/UUIDs; real SQL tests do.

    Preserve call order/transaction and failure propagation here, without claiming
    to prove the payment policy. Non-fake transactions still use the real hooks.
    """
    from openvegas.payments.adjustments import AdjustmentError
    from openvegas.store import service as module

    real_lock = module.lock_user_adjustments
    real_check = module.check_purchase_adjustments

    async def lock(tx, user_id):
        if not isinstance(tx, StoreFakeTx):
            return await real_lock(tx, user_id)
        await tx.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            json.dumps(["stripe-emote", user_id], separators=(",", ":")),
        )

    async def check(tx, *, user_id, order_id):
        if not isinstance(tx, StoreFakeTx):
            return await real_check(tx, user_id=user_id, order_id=order_id)
        tx.record("payment_policy_check", (user_id, order_id))
        assert tx.db.state["orders"][order_id]["status"] == "fulfilled"
        if tx.db.state.get("cash_adjustment_blocked"):
            raise AdjustmentError("COSMETIC_PAYMENT_ADJUSTMENT_REQUIRES_REVIEW")

    monkeypatch.setattr(module, "lock_user_adjustments", lock)
    monkeypatch.setattr(module, "check_purchase_adjustments", check)


@pytest.fixture
def approved(monkeypatch, tmp_path):
    item = {
        "name": "Local fixture",
        "description": "Not approved production artwork",
        "type": "cosmetic",
        "slot": "completion",
        "cost_v": Decimal("2.5"),
        "approval_status": "approved",
        "sale_enabled": True,
        "artwork_approved": True,
        "native_compatibility_verified": True,
        "asset": {
            "pack_id": "openvegas.test-pack",
            "version": "1.0.0",
            "preview_url": "/ui/assets/emotes/previews/test.gif",
            "thumbnail_url": "/ui/assets/emotes/previews/test.png",
            "compatibility": ["half-block"],
            "cost_v": "0",
            "private_storage_key": "MUST_NOT_LEAK",
        },
    }
    monkeypatch.setitem(STORE_CATALOG, "test_emote", item)
    other = deepcopy(item)
    other["asset"]["pack_id"] = "openvegas.test-other"
    monkeypatch.setitem(STORE_CATALOG, "test_other", other)
    # Test-only private copies: no production SKU is approved or made purchasable.
    public = Path(manifest.__file__).parent / "assets" / "pixel-courier"
    root = tmp_path.resolve() / "private-fixtures"
    for sku, fixture in (("test_emote", item), ("test_other", other)):
        fixture["asset"]["delivery_resource"] = sku
        target = root / sku
        target.mkdir(parents=True)
        raw = json.loads((public / "manifest.json").read_bytes())
        raw.update(pack_id=fixture["asset"]["pack_id"], version=fixture["asset"]["version"],
                   tags=[fixture["slot"]])
        (target / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
        (target / "sheet.png").write_bytes((public / "sheet.png").read_bytes())
    monkeypatch.setenv("OPENVEGAS_EMOTE_PACK_ROOT", str(root))
    pin_delivery_release(root, monkeypatch)
    return item


@pytest.fixture
def service():
    db = StoreFakeDB()
    return StoreService(db, StoreFakeWallet(db))
