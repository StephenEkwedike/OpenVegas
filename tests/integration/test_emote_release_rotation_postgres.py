"""Real SQL rollback on release rotation; synthetic artwork, no Stripe calls."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

import openvegas.store.service as store
from openvegas.emotes import manifest
from openvegas.store.catalog import STORE_CATALOG
from openvegas.wallet.ledger import WalletService
from tests.emote_release_fixture import pin_delivery_release
from tests.integration.test_restoration_db import _user


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["wallet", "policy"])
@pytest.mark.parametrize("change", ["pin", "catalog", "artwork_approved",
                                    "native_compatibility_verified", "sale_enabled"])
async def test_late_release_rotation_rolls_back_real_order_debit_and_entitlement(
    database_factory, monkeypatch, tmp_path, phase, change,
):
    sku = "openvegas.release-rotation-fixture"
    public = Path(manifest.__file__).parent / "assets" / "pixel-courier"
    root = tmp_path.resolve() / "private-fixtures"
    target = root / "rotation-fixture"
    target.mkdir(parents=True)
    raw = json.loads((public / "manifest.json").read_bytes())
    raw.update(pack_id=sku, version="0.0.1")
    (target / "manifest.json").write_text(json.dumps(raw))
    (target / "sheet.png").write_bytes((public / "sheet.png").read_bytes())
    monkeypatch.setenv("OPENVEGAS_EMOTE_PACK_ROOT", str(root))
    pin_delivery_release(root, monkeypatch)
    item = {
        "name": "SQL fixture", "type": "cosmetic", "slot": "companion",
        "cost_v": Decimal("2.50"), "approval_status": "approved", "sale_enabled": True,
        "artwork_approved": True, "native_compatibility_verified": True,
        "asset": {"pack_id": sku, "version": "0.0.1", "delivery_resource": target.name},
    }
    monkeypatch.setitem(STORE_CATALOG, sku, item)

    def rotate():
        if change == "pin":
            monkeypatch.setenv("OPENVEGAS_EMOTE_RELEASE_SHA256", "0" * 64)
        elif change == "catalog":
            item["native_compatibility_verified"] = False
        else:
            item[change] = 1

    async with database_factory() as sandbox:
        db = sandbox.db
        user = str(await _user(db))
        wallet = WalletService(db)
        await wallet.mint(f"user:{user}", Decimal(10), "rotation-test:" + user)
        service = store.StoreService(db, wallet)
        if phase == "wallet":
            original = wallet.redeem

            async def redeem(*args, **kwargs):
                result = await original(*args, **kwargs)
                rotate()
                return result

            monkeypatch.setattr(wallet, "redeem", redeem)
        else:
            original = store.check_purchase_adjustments

            async def check(*args, **kwargs):
                await original(*args, **kwargs)
                rotate()

            monkeypatch.setattr(store, "check_purchase_adjustments", check)
        with pytest.raises(store.CosmeticUnavailable, match="^COSMETIC_DELIVERY_UNAVAILABLE$"):
            await service.buy(user, sku, "rotation-once")
        assert await wallet.get_balance(f"user:{user}") == Decimal(10)
        assert await db.fetchval("SELECT count(*) FROM store_orders") == 0
        assert await db.fetchval("SELECT count(*) FROM cosmetic_entitlements") == 0
        assert await db.fetchval("SELECT count(*) FROM ledger_entries WHERE entry_type='redeem'") == 0
        assert await db.fetchval("SELECT sum(balance) FROM wallet_accounts") == 0
