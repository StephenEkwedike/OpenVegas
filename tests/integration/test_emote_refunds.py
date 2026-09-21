"""No Stripe calls: original $V debit reversal and entitlement retirement in real SQL."""

import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest

from openvegas.store.catalog import STORE_CATALOG
from openvegas.store.refunds import inspect_refund, refund_cosmetic
from openvegas.store.service import EntitlementDenied, StoreError, StoreService
from openvegas.wallet.ledger import WalletService
from tests.integration.test_restoration_db import _user

pytestmark = pytest.mark.asyncio
SKU = "local.refund-fixture"


async def purchase(db, monkeypatch, *, amount=Decimal(500)):
    # Theme fixture exercises commerce without changing any public sprite approval.
    monkeypatch.setitem(
        STORE_CATALOG,
        SKU,
        {
            "name": "Refund fixture",
            "type": "cosmetic",
            "slot": "theme",
            "cost_v": amount,
            "approval_status": "approved",
            "sale_enabled": True,
            "asset": {"pack_id": SKU, "version": "1.0.0"},
        },
    )
    user = str(await _user(db))
    wallet = WalletService(db)
    await wallet.mint(f"user:{user}", Decimal(1000), f"local-refund-fixture:{user}")
    service = StoreService(db, wallet)
    bought = await service.buy(user, SKU, "purchase")
    await service.equip(user, "theme", SKU)
    return user, bought.order_id, service, wallet


async def test_concurrent_refunds_restore_original_debit_once_and_retire_access(
    database_factory, monkeypatch
):
    async with database_factory() as sandbox:
        db = sandbox.db
        user, order, service, wallet = await purchase(db, monkeypatch)
        # Later catalog price must never influence a refund.
        monkeypatch.setitem(STORE_CATALOG[SKU], "cost_v", Decimal(10000))
        before = await inspect_refund(db, user_id=user, order_id=order)
        assert before["status"] == "fulfilled"
        assert await wallet.get_balance(f"user:{user}") == 500
        replies = await asyncio.wait_for(
            asyncio.gather(
                *(
                    refund_cosmetic(
                        db,
                        user_id=user,
                        order_id=order,
                        operator_id=str(uuid4()),
                        reason="customer_request",
                    )
                    for _ in range(6)
                )
            ),
            timeout=12,
        )
        assert sum(not r["replayed"] for r in replies) == 1
        assert all(Decimal(r["cost_v"]) == 500 for r in replies)
        assert await wallet.get_balance(f"user:{user}") == 1000
        assert (
            await db.fetchval("SELECT count(*) FROM ledger_entries WHERE entry_type='store_refund'")
            == 1
        )
        assert await db.fetchval("SELECT sum(balance) FROM wallet_accounts") == 0
        assert await db.fetchval("SELECT count(*) FROM cosmetic_equipment") == 0
        assert await db.fetchval("SELECT count(*) FROM cosmetic_entitlement_events") == 2
        assert (await service.list_owned(user))["equipped"] == {}
        with pytest.raises(EntitlementDenied):
            await service.equip(user, "theme", SKU)
        with pytest.raises(EntitlementDenied):
            async with service.delivery_asset(user, SKU):
                pytest.fail("Revoked pack was delivered")
        db = await sandbox.reconnect()
        replay = await refund_cosmetic(
            db, user_id=user, order_id=order, operator_id=str(uuid4()), reason="defective_pack"
        )
        assert replay["replayed"]
        assert await WalletService(db).get_balance(f"user:{user}") == 1000


async def test_refund_never_credits_another_account(database_factory, monkeypatch):
    async with database_factory() as sandbox:
        user, order, _, wallet = await purchase(sandbox.db, monkeypatch)
        foreign = str(await _user(sandbox.db))
        with pytest.raises(StoreError, match="NOT_FOUND"):
            await refund_cosmetic(
                sandbox.db,
                user_id=foreign,
                order_id=order,
                operator_id=str(uuid4()),
                reason="customer_request",
            )
        assert await wallet.get_balance(f"user:{user}") == 500
        assert (
            await sandbox.db.fetchval(
                "SELECT count(*) FROM ledger_entries WHERE entry_type='store_refund'"
            )
            == 0
        )


async def test_failure_after_credit_rolls_back_ledger_revocation_and_equipment(
    database_factory, monkeypatch
):
    async with database_factory() as sandbox:
        db = sandbox.db
        user, order, _, wallet = await purchase(db, monkeypatch)

        async def fail(*a, **k):
            raise RuntimeError("injected order update failure")

        monkeypatch.setattr(StoreService, "_transition_order", fail)
        with pytest.raises(RuntimeError, match="injected"):
            await refund_cosmetic(
                db,
                user_id=user,
                order_id=order,
                operator_id=str(uuid4()),
                reason="customer_request",
            )
        assert await wallet.get_balance(f"user:{user}") == 500
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "active"
        assert await db.fetchval("SELECT count(*) FROM cosmetic_equipment") == 1
        assert (
            await db.fetchval("SELECT count(*) FROM ledger_entries WHERE entry_type='store_refund'")
            == 0
        )


async def test_zero_cost_refund_revokes_without_minting_value(database_factory, monkeypatch):
    async with database_factory() as sandbox:
        user, order, _, wallet = await purchase(sandbox.db, monkeypatch, amount=Decimal(0))
        for _ in range(2):
            await refund_cosmetic(
                sandbox.db,
                user_id=user,
                order_id=order,
                operator_id=str(uuid4()),
                reason="operator_correction",
            )
        assert await wallet.get_balance(f"user:{user}") == 1000
        assert (
            await sandbox.db.fetchval(
                "SELECT count(*) FROM ledger_entries WHERE entry_type='store_refund'"
            )
            == 0
        )


@pytest.mark.parametrize("change", ["revoked", "reversed", "missing_debit", "changed_debit"])
async def test_unknown_prior_reversal_requires_reconciliation(
    database_factory, monkeypatch, change
):
    async with database_factory() as sandbox:
        db = sandbox.db
        user, order, _, wallet = await purchase(db, monkeypatch)
        if change == "revoked":
            await db.execute(
                "UPDATE cosmetic_entitlements SET status='revoked', revoked_at=now(), revocation_reason='manual'"
            )
        elif change == "reversed":
            await db.execute("UPDATE store_orders SET status='reversed'")
        elif change == "missing_debit":
            await db.execute("DELETE FROM ledger_entries WHERE entry_type='redeem'")
        else:
            await db.execute("UPDATE ledger_entries SET amount=1 WHERE entry_type='redeem'")
        with pytest.raises(StoreError, match="RECONCILIATION"):
            await refund_cosmetic(
                db,
                user_id=user,
                order_id=order,
                operator_id=str(uuid4()),
                reason="customer_request",
            )
        assert await wallet.get_balance(f"user:{user}") == 500


async def test_equip_racing_refund_leaves_no_stale_slot(database_factory, monkeypatch):
    async with database_factory() as sandbox:
        user, order, service, _ = await purchase(sandbox.db, monkeypatch)
        results = await asyncio.wait_for(
            asyncio.gather(
                service.equip(user, "theme", SKU),
                refund_cosmetic(
                    sandbox.db,
                    user_id=user,
                    order_id=order,
                    operator_id=str(uuid4()),
                    reason="customer_request",
                ),
                return_exceptions=True,
            ),
            timeout=12,
        )
        assert isinstance(results[1], dict)
        assert await sandbox.db.fetchval("SELECT count(*) FROM cosmetic_equipment") == 0
