"""Real local PostgreSQL, synthetic signed Stripe event, no external payment calls."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from openvegas.emotes import manifest
from openvegas.store.catalog import STORE_CATALOG
from openvegas.store.service import EntitlementDenied, StoreService
from openvegas.wallet.ledger import WalletService
from tests.integration.test_restoration_db import _billing, _checkout_event, _signed, _topup, _user

pytestmark = pytest.mark.asyncio


async def test_signed_test_topup_concurrent_buy_equip_and_revoke(database_factory, monkeypatch, tmp_path):
    # This priced SKU exists only in this test process, never in the sale catalog.
    monkeypatch.setitem(STORE_CATALOG, "openvegas.test-only", {
        "name": "Test only", "type": "cosmetic", "slot": "companion",
        "cost_v": Decimal("2.50"), "approval_status": "approved", "sale_enabled": True,
        "asset": {"pack_id": "openvegas.test-only", "version": "0.0.1", "delivery_resource": "test-only"},
    })
    public = Path(manifest.__file__).parent / "assets" / "pixel-courier"
    root = tmp_path.resolve() / "private-fixtures"
    target = root / "test-only"
    target.mkdir(parents=True)
    raw = json.loads((public / "manifest.json").read_bytes())
    raw.update(pack_id="openvegas.test-only", version="0.0.1")
    (target / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    (target / "sheet.png").write_bytes((public / "sheet.png").read_bytes())
    monkeypatch.setenv("OPENVEGAS_EMOTE_PACK_ROOT", str(root))
    async with database_factory() as sandbox:
        db = sandbox.db
        topup = await _topup(db)
        user = str(topup[0])
        event = _checkout_event(topup)
        await _billing(db).handle_webhook(**_signed(event))
        await _billing(db).handle_webhook(**_signed(event))
        wallet = WalletService(db)
        assert await wallet.get_balance(f"user:{user}") == Decimal(1000)
        service = StoreService(db, wallet)
        orders = await asyncio.wait_for(asyncio.gather(*(
            service.buy(user, "openvegas.test-only", f"concurrent-{n}") for n in range(6)
        )), timeout=15)
        assert len({str(order.order_id) for order in orders}) == 1
        assert await wallet.get_balance(f"user:{user}") == Decimal("997.50")
        assert await db.fetchval("SELECT count(*) FROM cosmetic_entitlements") == 1
        assert await db.fetchval("SELECT count(*) FROM ledger_entries WHERE entry_type='redeem'") == 1
        assert await db.fetchval("SELECT sum(balance) FROM wallet_accounts") == 0

        db = await sandbox.reconnect()
        service = StoreService(db, WalletService(db))
        owned = await service.list_owned(user)
        assert len(owned["entitlements"]) == 1
        await service.equip(user, "companion", "openvegas.test-only")
        assert (await service.list_owned(user))["equipped"]["companion"] == "openvegas.test-only"
        foreign = str(await _user(db))
        with pytest.raises(EntitlementDenied):
            await service.equip(foreign, "companion", "openvegas.test-only")
        async with db.transaction() as tx:
            await service._lock_scope(tx, "sku", user, "openvegas.test-only")
            await tx.execute("UPDATE cosmetic_entitlements SET status='revoked', revoked_at=now(), revocation_reason='local_test' WHERE user_id=$1", user)
        assert (await service.list_owned(user))["equipped"] == {}
        with pytest.raises(EntitlementDenied):
            await service.equip(user, "companion", "openvegas.test-only")
        assert await db.fetchval("SELECT count(*) FROM cosmetic_entitlement_events") == 2


@pytest.mark.parametrize("role", ["anon", "authenticated"])
async def test_emote_tables_deny_browser_roles(database_factory, role):
    async with database_factory() as sandbox:
        for table in ("cosmetic_entitlements", "cosmetic_equipment", "store_purchase_requests", "cosmetic_entitlement_events"):
            assert await sandbox.db.fetchval("SELECT relrowsecurity FROM pg_class WHERE oid=$1::regclass", table)
            async with sandbox.db.transaction() as tx:
                await tx.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
                await tx.execute(f"SET LOCAL ROLE {role}")
                with pytest.raises(Exception, match="permission denied"):
                    await tx.fetch(f"SELECT * FROM public.{table}")
