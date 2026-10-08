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
from tests.integration.test_stripe_emote_adjustments import buy, cash_event, post
from tests.integration.test_stripe_emote_adjustments import purchase as funded_purchase

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


async def test_partial_cash_refund_blocks_manual_v_credit(database_factory, monkeypatch):
    async with database_factory() as sandbox:
        db = sandbox.db
        topup, user, service, wallet, order = await funded_purchase(db, monkeypatch, amount=500)
        await buy(db, monkeypatch, service, user, 500)
        event = cash_event(topup, kind="refund", state="succeeded", amount=300)
        assert (await post(db, monkeypatch, event)).status_code == 200
        before = await db.fetch("SELECT * FROM ledger_entries ORDER BY id")
        assert await db.fetchval(
            "SELECT status FROM cosmetic_entitlements WHERE source_order_id=$1", order.order_id
        ) == "active"
        with pytest.raises(StoreError, match="REFUND_REQUIRES_RECONCILIATION"):
            await refund_cosmetic(
                db, user_id=user, order_id=order.order_id,
                operator_id=str(uuid4()), reason="customer_request",
            )
        assert await db.fetch("SELECT * FROM ledger_entries ORDER BY id") == before
        assert await wallet.get_balance(f"user:{user}") == 0
        async with sandbox.pool.acquire() as conn, conn.transaction(readonly=True):
            inspected = await inspect_refund(conn, user_id=user, order_id=order.order_id)
        assert inspected["cash_adjustment_review"] == {
            "required": True,
            "funding_attributed": True,
            "funding_review_required": False,
            "reasons": ["partial_refund_allocation_unknown"],
        }
        assert inspected["status"] == "fulfilled"
        assert inspected["entitlement_status"] == "active"


@pytest.mark.parametrize(
    "kind,state,mixed,expected_reason",
    [
        ("dispute", "needs_response", True, "open_dispute"),
        ("dispute", "lost", True, "confirmed_full_cash_reversal"),
        ("refund", "pending", False, "pending_cash_refund"),
        ("refund", "requires_action", False, "pending_cash_refund"),
    ],
)
async def test_unresolved_cash_refund_guard_preserves_all_state(
    database_factory, monkeypatch, kind, state, mixed, expected_reason
):
    async with database_factory() as sandbox:
        db = sandbox.db
        topup, user, _, _, order = await funded_purchase(
            db, monkeypatch, amount=100 if mixed else 500, mixed=mixed
        )
        assert (await post(db, monkeypatch, cash_event(topup, kind=kind, state=state))).status_code == 200
        inspected = await inspect_refund(db, user_id=user, order_id=order.order_id)
        assert inspected["entitlement_status"] == "active"
        review = inspected["cash_adjustment_review"]
        assert review["required"] and review["reasons"] == [expected_reason]
        assert review["funding_review_required"] == mixed
        tables = (
            "ledger_entries", "wallet_accounts", "store_orders", "cosmetic_entitlements",
            "cosmetic_equipment", "cosmetic_entitlement_events", "stripe_emote_funding",
            "stripe_emote_adjustments", "stripe_emote_adjustment_audit",
        )
        before = {table: await db.fetch(f"SELECT * FROM {table}") for table in tables}
        for reason in ("customer_request", "operator_correction"):
            with pytest.raises(StoreError, match="REFUND_REQUIRES_RECONCILIATION"):
                await refund_cosmetic(
                    db, user_id=user, order_id=order.order_id,
                    operator_id=str(uuid4()), reason=reason,
                )
        for table in tables:
            assert await db.fetch(f"SELECT * FROM {table}") == before[table], table


async def test_resolved_review_allows_refund_and_later_cash_event_does_not_break_replay(
    database_factory, monkeypatch
):
    async with database_factory() as sandbox:
        db = sandbox.db
        topup, user, _, wallet, order = await funded_purchase(db, monkeypatch, amount=100, mixed=True)
        opened = cash_event(topup)
        assert (await post(db, monkeypatch, opened)).status_code == 200
        assert (await inspect_refund(db, user_id=user, order_id=order.order_id))[
            "cash_adjustment_review"
        ]["funding_review_required"]
        won = cash_event(
            topup, object_id=opened["data"]["object"]["id"], state="won", created=101
        )
        assert (await post(db, monkeypatch, won)).status_code == 200
        assert await db.fetchval(
            "SELECT count(*) FROM stripe_emote_adjustment_audit WHERE outcome='manual_review'"
        ) > 0
        result = await refund_cosmetic(
            db, user_id=user, order_id=order.order_id,
            operator_id=str(uuid4()), reason="customer_request",
        )
        assert not result["replayed"] and not result["cash_adjustment_review"]["required"]
        assert await wallet.get_balance(f"user:{user}") == 1100
        assert (await post(db, monkeypatch, cash_event(topup, kind="refund", state="succeeded"))).status_code == 200
        before = await db.fetch("SELECT * FROM ledger_entries ORDER BY id")
        db = await sandbox.reconnect()
        replay = await refund_cosmetic(
            db, user_id=user, order_id=order.order_id,
            operator_id=str(uuid4()), reason="customer_request",
        )
        assert replay["replayed"] and replay["cash_adjustment_review"]["required"]
        assert await db.fetch("SELECT * FROM ledger_entries ORDER BY id") == before


async def test_zero_cost_retirement_still_mints_nothing_during_cash_review(database_factory, monkeypatch):
    async with database_factory() as sandbox:
        db = sandbox.db
        topup, user, _, _, order = await funded_purchase(db, monkeypatch, amount=0)
        assert (await post(db, monkeypatch, cash_event(topup))).status_code == 200
        before = await db.fetch("SELECT * FROM ledger_entries ORDER BY id")
        for replayed in (False, True):
            result = await refund_cosmetic(
                db, user_id=user, order_id=order.order_id,
                operator_id=str(uuid4()), reason="customer_request",
            )
            assert result["replayed"] == replayed
            assert result["entitlement_status"] == "revoked"
        assert await db.fetch("SELECT * FROM ledger_entries ORDER BY id") == before


async def test_won_dispute_and_failed_refund_allow_original_price_v_refund(
    database_factory, monkeypatch
):
    async with database_factory() as sandbox:
        db = sandbox.db
        topup, user, _, wallet, order = await funded_purchase(db, monkeypatch, amount=500)
        opened = cash_event(topup)
        assert (await post(db, monkeypatch, opened)).status_code == 200
        won = cash_event(
            topup, object_id=opened["data"]["object"]["id"], state="won", created=101
        )
        assert (await post(db, monkeypatch, won)).status_code == 200
        pending = cash_event(topup, kind="refund", state="pending", amount=300, created=102)
        assert (await post(db, monkeypatch, pending)).status_code == 200
        assert (await inspect_refund(db, user_id=user, order_id=order.order_id))[
            "cash_adjustment_review"
        ]["required"]
        failed = cash_event(
            topup, kind="refund", state="failed", amount=300, created=103,
            object_id=pending["data"]["object"]["id"],
        )
        assert (await post(db, monkeypatch, failed)).status_code == 200
        monkeypatch.setitem(STORE_CATALOG[order.item_id], "cost_v", Decimal(10000))
        result = await refund_cosmetic(
            db, user_id=user, order_id=order.order_id,
            operator_id=str(uuid4()), reason="customer_request",
        )
        assert not result["replayed"] and not result["cash_adjustment_review"]["required"]
        assert result["cost_v"] == "500.000000"
        assert await wallet.get_balance(f"user:{user}") == 1000
        assert await db.fetchval(
            "SELECT amount FROM ledger_entries WHERE entry_type='store_refund'"
        ) == 500


@pytest.mark.parametrize("first_actor", ["refund", "webhook"])
async def test_refund_and_cash_webhook_serialize_before_sku_and_recheck(
    database_factory, monkeypatch, first_actor
):
    from openvegas.payments import adjustments
    from openvegas.store import refunds

    async with database_factory() as sandbox:
        db = sandbox.db
        topup, user, service, wallet, order = await funded_purchase(db, monkeypatch, amount=500)
        await buy(db, monkeypatch, service, user, 500)
        entered, release, inspected = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_inspect = refunds.inspect_refund

        async def inspected_refund(*args, **kwargs):
            result = await original_inspect(*args, **kwargs)
            assert not result["cash_adjustment_review"]["required"]
            inspected.set()
            return result

        monkeypatch.setattr(refunds, "inspect_refund", inspected_refund)
        if first_actor == "refund":
            original_lock = refunds.lock_user_adjustments

            async def paused_lock(*args, **kwargs):
                await original_lock(*args, **kwargs)
                entered.set()
                await release.wait()

            monkeypatch.setattr(refunds, "lock_user_adjustments", paused_lock)
        else:
            original_apply = adjustments._apply_entitlements

            async def paused_apply(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original_apply(*args, **kwargs)

            monkeypatch.setattr(adjustments, "_apply_entitlements", paused_apply)
        refund_task = webhook_task = None

        def start_refund():
            return asyncio.create_task(refund_cosmetic(
                db, user_id=user, order_id=order.order_id,
                operator_id=str(uuid4()), reason="customer_request",
            ))

        def start_webhook():
            return asyncio.create_task(post(
                db, monkeypatch, cash_event(topup, kind="refund", state="succeeded", amount=300)
            ))

        try:
            if first_actor == "refund":
                refund_task = start_refund()
            else:
                webhook_task = start_webhook()
            await asyncio.wait_for(entered.wait(), 5)
            if first_actor == "refund":
                webhook_task = blocked = start_webhook()
            else:
                refund_task = blocked = start_refund()
            await asyncio.wait_for(inspected.wait(), 5)
            await asyncio.sleep(0.05)
            assert not blocked.done(), "Second operation bypassed the shared adjustment lock"
            release.set()
            refunded, delivered = await asyncio.wait_for(
                asyncio.gather(refund_task, webhook_task, return_exceptions=True), 12
            )
        finally:
            release.set()
            tasks = [task for task in (refund_task, webhook_task) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert delivered.status_code == 200
        if first_actor == "webhook":
            assert isinstance(refunded, StoreError)
            assert "RECONCILIATION" in str(refunded)
            expected = 0
        else:
            assert not isinstance(refunded, BaseException), repr(refunded)
            assert not refunded["replayed"]
            expected = 500
        assert await wallet.get_balance(f"user:{user}") == expected
        assert await db.fetchval(
            "SELECT count(*) FROM ledger_entries WHERE entry_type='store_refund'"
        ) == (1 if expected else 0)
