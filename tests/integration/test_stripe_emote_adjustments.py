"""Real PostgreSQL + signed HTTP webhooks. No Stripe API, cash actions, or mock ledger."""

import asyncio
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from openvegas.payments import adjustments
from openvegas.store.catalog import STORE_CATALOG
from openvegas.store.refunds import refund_cosmetic
from openvegas.store.service import EntitlementDenied, StoreError, StoreService
from openvegas.wallet.ledger import WalletService
from server.routes import payments
from tests.integration.test_restoration_db import _billing, _checkout_event, _signed, _topup, _user

pytestmark = pytest.mark.asyncio


def cash_event(
    topup, *, kind="dispute", state="needs_response", amount=1000, object_id=None, created=100
):
    pi = "pi_local_" + str(topup[1])
    charge = "ch_local_" + str(topup[1])
    prefix = {"refund": "re_", "dispute": "du_", "charge_refund": "ch_"}[kind]
    obj = {
        "object": "charge" if kind == "charge_refund" else kind,
        "id": charge if kind == "charge_refund" else object_id or prefix + uuid4().hex,
        "payment_intent": pi,
        "charge": charge,
        "currency": "usd",
        "amount": amount,
        "status": state,
        "livemode": False,
    }
    if kind == "dispute":
        obj.update(
            created=created,
            reason="product_not_received",
            evidence={},
            evidence_details={
                "due_by": 2000000000, "has_evidence": False,
                "past_due": False, "submission_count": 0,
            },
            balance_transactions=[],
            is_charge_refundable=False,
            metadata={},
        )
    event_type = "refund.updated" if kind == "refund" else "charge.dispute.updated"
    if kind == "charge_refund":
        obj.update(amount=1000, amount_refunded=amount, paid=True, customer=topup[3])
        event_type = "charge.refunded"
    if kind == "dispute" and state in adjustments.DISPUTE_CLOSED:
        event_type = "charge.dispute.closed"
    return {
        "id": "evt_" + uuid4().hex,
        "object": "event",
        "type": event_type,
        "created": created,
        "livemode": False,
        "data": {"object": obj},
    }


async def post(db, monkeypatch, event):
    app = FastAPI()
    app.include_router(payments.router)
    monkeypatch.setattr(payments, "get_billing_service", lambda: _billing(db))
    signed = _signed(event)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            "/billing/webhook/stripe",
            content=signed["raw_body"],
            headers={"stripe-signature": signed["signature"]},
        )


async def purchase(db, monkeypatch, *, amount=1000, mixed=False):
    topup = await _topup(db)
    await _billing(db).handle_webhook(**_signed(_checkout_event(topup)))
    user = str(topup[0])
    wallet = WalletService(db)
    if mixed:
        await wallet.mint(f"user:{user}", Decimal(100), "mixed:" + user)
    service = StoreService(db, wallet)
    order = await buy(db, monkeypatch, service, user, amount)
    await service.equip(user, "theme", order.item_id)
    return topup, user, service, wallet, order


async def buy(db, monkeypatch, service, user, amount):
    sku = "local.adjustment-" + uuid4().hex
    monkeypatch.setitem(
        STORE_CATALOG,
        sku,
        {
            "name": "SQL fixture",
            "type": "cosmetic",
            "slot": "theme",
            "cost_v": Decimal(amount),
            "approval_status": "approved",
            "sale_enabled": True,
            "asset": {"pack_id": sku, "version": "1.0.0"},
        },
    )
    return await service.buy(user, sku, "local-" + uuid4().hex)


async def assert_no_money_movement(db, user, before):
    assert await db.fetchval("SELECT count(*) FROM ledger_entries") == before
    assert (
        await db.fetchval("SELECT count(*) FROM ledger_entries WHERE entry_type='store_refund'")
        == 0
    )
    assert await db.fetchval("SELECT sum(balance) FROM wallet_accounts") == 0


async def test_signed_full_refund_revokes_once_without_cash_or_v_refund(
    database_factory, monkeypatch
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, user, store, wallet, order = await purchase(db, monkeypatch)
        count = await db.fetchval("SELECT count(*) FROM ledger_entries")
        event = cash_event(topup, kind="refund", state="succeeded")
        replies = await asyncio.wait_for(
            asyncio.gather(*(post(db, monkeypatch, event) for _ in range(6))), 20
        )
        assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
        assert sum(r.json()["status"] == "duplicate" for r in replies) == 5
        assert await db.fetchval("SELECT count(*) FROM stripe_emote_funding") == 1
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "revoked"
        assert await db.fetchval("SELECT count(*) FROM cosmetic_equipment") == 0
        assert await wallet.get_balance(f"user:{user}") == 0
        assert (await store.list_owned(user))["entitlements"][0]["effective_status"] == "revoked"
        await assert_no_money_movement(db, user, count)
        db = await sandbox.reconnect()
        assert (await post(db, monkeypatch, event)).json()["status"] == "duplicate"
        assert (
            await db.fetchval("SELECT status FROM store_orders WHERE id=$1", order.order_id)
            == "fulfilled"
        )


@pytest.mark.parametrize("terminal", ["won", "lost"])
async def test_signed_du_dispute_two_purchase_lifecycle_preserves_money(
    database_factory, monkeypatch, terminal
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, user, service, wallet, first = await purchase(db, monkeypatch, amount=500)
        second = await buy(db, monkeypatch, service, user, 500)
        orders = (first, second)
        ledger = await db.fetch("SELECT * FROM ledger_entries ORDER BY id")
        assert len(ledger) == 3
        assert await db.fetchval("SELECT count(*) FROM stripe_emote_funding") == 2
        assert await wallet.get_balance(f"user:{user}") == 0

        opened = cash_event(topup, object_id="du_1MtJUT2eZvKYlo2CNaw2HvEv")
        opened["type"] = "charge.dispute.created"
        response = await post(db, monkeypatch, opened)
        assert response.status_code == 200, response.text
        assert response.json()["attributed_orders"] == 2
        assert await db.fetchval(
            "SELECT count(*) FROM cosmetic_entitlements WHERE status='suspended'"
        ) == 2
        assert await db.fetchval("SELECT count(*) FROM cosmetic_equipment") == 0
        for order in orders:
            with pytest.raises(EntitlementDenied):
                await service.equip(user, "theme", order.item_id)
            with pytest.raises(EntitlementDenied):
                async with service.delivery_asset(user, order.item_id):
                    pytest.fail("Suspended asset delivered")

        closed = cash_event(
            topup, object_id=opened["data"]["object"]["id"], state=terminal, created=101
        )
        closed["type"] = "charge.dispute.closed"
        response = await post(db, monkeypatch, closed)
        assert response.status_code == 200, response.text
        target = "active" if terminal == "won" else "revoked"
        assert await db.fetchval(
            "SELECT count(*) FROM cosmetic_entitlements WHERE status=$1", target
        ) == 2
        assert await db.fetchval("SELECT count(*) FROM cosmetic_equipment") == 0
        assert await db.fetchval("SELECT state FROM stripe_emote_adjustments") == terminal
        assert await db.fetchval("SELECT count(*) FROM store_orders WHERE status='fulfilled'") == 2
        assert await db.fetchval(
            "SELECT count(*) FROM stripe_emote_adjustment_audit WHERE outcome='manual_review'"
        ) == 0

        audit_count = await db.fetchval("SELECT count(*) FROM stripe_emote_adjustment_audit")
        assert (await post(db, monkeypatch, closed)).json()["status"] == "duplicate"
        assert await db.fetchval("SELECT count(*) FROM stripe_emote_adjustment_audit") == audit_count
        late = cash_event(topup, object_id=opened["data"]["object"]["id"], created=999)
        assert (await post(db, monkeypatch, late)).json()["outcome"] == "stale"
        assert await db.fetchval(
            "SELECT count(*) FROM cosmetic_entitlements WHERE status=$1", target
        ) == 2
        for order in orders:
            if terminal == "won":
                await service.equip(user, "theme", order.item_id)
                async with service.delivery_asset(user, order.item_id) as asset:
                    assert asset["pack_id"] == order.item_id
            else:
                with pytest.raises(EntitlementDenied):
                    await service.equip(user, "theme", order.item_id)
                with pytest.raises(EntitlementDenied):
                    async with service.delivery_asset(user, order.item_id):
                        pytest.fail("Revoked asset delivered")
        assert await db.fetch("SELECT * FROM ledger_entries ORDER BY id") == ledger
        assert await wallet.get_balance(f"user:{user}") == 0
        await assert_no_money_movement(db, user, len(ledger))


@pytest.mark.parametrize(
    "event_type,state",
    [
        ("charge.dispute.created", "needs_response"),
        ("charge.dispute.updated", "under_review"),
        ("charge.dispute.closed", "won"),
        ("charge.dispute.closed", "lost"),
    ],
)
async def test_signed_dp_dispute_rejected_without_committing_changes(
    database_factory, monkeypatch, event_type, state
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, user, _, _, _ = await purchase(db, monkeypatch)
        tables = (
            "stripe_webhook_events", "stripe_emote_adjustments", "stripe_emote_funding",
            "stripe_emote_adjustment_audit", "stripe_emote_suspensions", "ledger_entries",
            "cosmetic_entitlements", "cosmetic_equipment", "store_orders", "wallet_accounts",
        )
        before = {table: await db.fetch(f"SELECT * FROM {table}") for table in tables}
        event = cash_event(topup, object_id="dp_1MtJUT2eZvKYlo2CNaw2HvEv", state=state)
        event["type"] = event_type
        for _ in range(2):
            response = await post(db, monkeypatch, event)
            assert response.status_code == 503
            assert response.json() == {
                "detail": "Unable to process Stripe webhook; retry later"
            }
        for table in tables:
            assert await db.fetch(f"SELECT * FROM {table}") == before[table], table
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "active"
        await assert_no_money_movement(db, user, len(before["ledger_entries"]))


async def test_multiple_disputes_won_restore_only_after_all_close_and_not_equipment(
    database_factory, monkeypatch
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, _user_id, _, _, _ = await purchase(db, monkeypatch)
        first = cash_event(topup, object_id="du_first")
        second = cash_event(topup, object_id="du_second")
        for event in (first, second):
            assert (await post(db, monkeypatch, event)).status_code == 200
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "suspended"
        assert (
            await post(
                db, monkeypatch, cash_event(topup, object_id="du_first", state="won", created=102)
            )
        ).status_code == 200
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "suspended"
        assert (
            await post(
                db, monkeypatch, cash_event(topup, object_id="du_second", state="won", created=103)
            )
        ).status_code == 200
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "active"
        assert await db.fetchval("SELECT count(*) FROM cosmetic_equipment") == 0
        late = cash_event(topup, object_id="du_first", created=999)
        assert (await post(db, monkeypatch, late)).json()["outcome"] == "stale"
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "active"
        assert await db.fetchval("SELECT count(*) FROM stripe_emote_suspensions") == 0


async def test_lost_and_won_disputes_race_never_restore_lost_funding(database_factory, monkeypatch):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, _, _, _, _ = await purchase(db, monkeypatch)
        events = [
            cash_event(topup, object_id="du_lost", state="lost"),
            cash_event(topup, object_id="du_won", state="won"),
        ]
        replies = await asyncio.wait_for(
            asyncio.gather(*(post(db, monkeypatch, e) for e in events)), 15
        )
        assert [r.status_code for r in replies] == [200, 200]
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "revoked"


async def test_partial_refunds_do_not_guess_multi_order_allocation_then_full_revokes(
    database_factory, monkeypatch
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, user, service, _, _ = await purchase(db, monkeypatch, amount=500)
        await buy(db, monkeypatch, service, user, 500)
        first = cash_event(
            topup, kind="refund", state="succeeded", amount=300, object_id="re_first"
        )
        assert (await post(db, monkeypatch, first)).status_code == 200
        assert await db.fetchval("SELECT count(*) FROM stripe_emote_funding") == 2
        assert (
            await db.fetchval("SELECT count(*) FROM cosmetic_entitlements WHERE status='active'")
            == 2
        )
        assert (
            await db.fetchval(
                "SELECT count(*) FROM stripe_emote_adjustment_audit WHERE outcome='manual_review'"
            )
            >= 1
        )
        aggregate = cash_event(topup, kind="charge_refund", amount=300)
        assert (await post(db, monkeypatch, aggregate)).status_code == 200
        assert (
            await db.fetchval("SELECT count(*) FROM cosmetic_entitlements WHERE status='active'")
            == 2
        )
        second = cash_event(topup, kind="refund", state="succeeded", amount=700)
        assert (await post(db, monkeypatch, second)).status_code == 200
        assert (
            await db.fetchval("SELECT count(*) FROM cosmetic_entitlements WHERE status='revoked'")
            == 2
        )
        assert (
            await post(db, monkeypatch, cash_event(topup, kind="charge_refund", amount=1000))
        ).status_code == 200
        assert (
            await db.fetchval(
                "SELECT count(*) FROM cosmetic_entitlement_events WHERE event_type='revoked'"
            )
            == 2
        )


async def test_partial_refund_of_exclusive_indivisible_purchase_revokes(
    database_factory, monkeypatch
):
    async with database_factory(through=43) as sandbox:
        topup, _, _, _, _ = await purchase(sandbox.db, monkeypatch)
        event = cash_event(topup, kind="refund", state="succeeded", amount=1)
        assert (await post(sandbox.db, monkeypatch, event)).status_code == 200
        assert await sandbox.db.fetchval("SELECT status FROM cosmetic_entitlements") == "revoked"


async def test_mixed_wallet_is_audited_without_fabricating_provenance(
    database_factory, monkeypatch
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, user, _, _, _ = await purchase(db, monkeypatch, mixed=True, amount=100)
        count = await db.fetchval("SELECT count(*) FROM ledger_entries")
        reply = await post(db, monkeypatch, cash_event(topup, state="lost"))
        assert reply.status_code == 200
        assert reply.json()["attributed_orders"] == 0
        assert await db.fetchval("SELECT count(*) FROM stripe_emote_funding") == 0
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "active"
        assert (
            await db.fetchval(
                "SELECT count(*) FROM stripe_emote_adjustment_audit WHERE outcome='manual_review'"
            )
            == 1
        )
        await assert_no_money_movement(db, user, count)


async def test_verified_link_survives_later_mixed_funding(database_factory, monkeypatch):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, user, _, wallet, order = await purchase(db, monkeypatch)
        assert str(await db.fetchval("SELECT order_id FROM stripe_emote_funding")) == order.order_id
        await wallet.mint(f"user:{user}", Decimal(100), "later-credits")
        assert (await post(db, monkeypatch, cash_event(topup))).status_code == 200
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "suspended"


async def test_starter_grant_plus_topup_has_provable_minimum_not_fifo(
    database_factory, monkeypatch
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, user, _, wallet, _ = await purchase(db, monkeypatch, mixed=True)
        assert await wallet.get_balance(f"user:{user}") == 100
        assert (
            await post(db, monkeypatch, cash_event(topup, kind="refund", state="succeeded"))
        ).status_code == 200
        assert await db.fetchval("SELECT funded_v FROM stripe_emote_funding") == 900
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "revoked"
        assert await wallet.get_balance(f"user:{user}") == 100


async def test_cash_event_before_settlement_rolls_back_journal_then_retries(
    database_factory, monkeypatch
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup = await _topup(db)
        event = cash_event(topup, kind="refund", state="succeeded")
        assert (await post(db, monkeypatch, event)).status_code == 503
        assert await db.fetchval("SELECT count(*) FROM stripe_webhook_events") == 0
        await _billing(db).handle_webhook(**_signed(_checkout_event(topup)))
        assert (await post(db, monkeypatch, event)).status_code == 200
        assert await db.fetchval("SELECT count(*) FROM stripe_webhook_events") == 2


async def test_conflicting_duplicate_payload_does_not_modify_original(
    database_factory, monkeypatch
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, _, _, _, _ = await purchase(db, monkeypatch)
        event = cash_event(topup)
        assert (await post(db, monkeypatch, event)).status_code == 200
        event["data"]["object"]["status"] = "won"
        assert (await post(db, monkeypatch, event)).status_code == 503
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "suspended"
        assert await db.fetchval("SELECT state FROM stripe_emote_adjustments") == "needs_response"


async def test_fault_after_revocation_rolls_back_evidence_projection_audit_and_journal(
    database_factory, monkeypatch
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, user, _, _, _ = await purchase(db, monkeypatch)
        count = await db.fetchval("SELECT count(*) FROM ledger_entries")
        original = adjustments._audit

        async def fail_after_change(tx, event_id, topup_id, outcome, facts):
            if outcome == "entitlement_evaluated":
                raise RuntimeError("injected fault after entitlement write")
            await original(tx, event_id, topup_id, outcome, facts)

        monkeypatch.setattr(adjustments, "_audit", fail_after_change)
        event = cash_event(topup, state="lost")
        assert (await post(db, monkeypatch, event)).status_code == 503
        for table in (
            "stripe_emote_adjustments",
            "stripe_emote_adjustment_audit",
        ):
            assert await db.fetchval(f"SELECT count(*) FROM {table}") == 0
        # Original committed purchase proof survives; webhook changes do not.
        assert await db.fetchval("SELECT count(*) FROM stripe_emote_funding") == 1
        assert await db.fetchval("SELECT count(*) FROM stripe_webhook_events") == 1
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "active"
        assert await db.fetchval("SELECT count(*) FROM cosmetic_equipment") == 1
        await assert_no_money_movement(db, user, count)
        monkeypatch.setattr(adjustments, "_audit", original)
        assert (await post(db, monkeypatch, event)).status_code == 200
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "revoked"


async def test_foreign_user_and_reused_charge_cannot_change_ownership(
    database_factory, monkeypatch
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        first, first_user, _, _, _ = await purchase(db, monkeypatch)
        second, second_user, _, _, _ = await purchase(db, monkeypatch)
        assert (await post(db, monkeypatch, cash_event(first))).status_code == 200
        assert (
            await db.fetchval(
                "SELECT status FROM cosmetic_entitlements WHERE user_id=$1", second_user
            )
            == "active"
        )
        forged = cash_event(second, state="lost")
        forged["data"]["object"]["charge"] = "ch_local_" + str(first[1])
        assert (await post(db, monkeypatch, forged)).status_code == 503
        assert (
            await db.fetchval(
                "SELECT status FROM cosmetic_entitlements WHERE user_id=$1", first_user
            )
            == "suspended"
        )
        assert (
            await db.fetchval(
                "SELECT status FROM cosmetic_entitlements WHERE user_id=$1", second_user
            )
            == "active"
        )


async def test_won_dispute_cannot_restore_independent_v_refund(database_factory, monkeypatch):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, user, _, _, order = await purchase(db, monkeypatch)
        async with db.transaction() as tx:
            assert await adjustments.attribute_order(tx, user_id=user, order_id=order.order_id)
        await refund_cosmetic(
            db,
            user_id=user,
            order_id=order.order_id,
            operator_id=str(await _user(db)),
            reason="customer_request",
        )
        assert (await post(db, monkeypatch, cash_event(topup, state="won"))).status_code == 200
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "revoked"
        assert (
            await db.fetchval("SELECT count(*) FROM ledger_entries WHERE entry_type='store_refund'")
            == 1
        )


async def test_adjustment_history_is_append_only_and_private(database_factory, monkeypatch):
    import asyncpg

    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, _, _, _, _ = await purchase(db, monkeypatch)
        assert (await post(db, monkeypatch, cash_event(topup))).status_code == 200
        for table in ("stripe_emote_funding", "stripe_emote_adjustment_audit"):
            with pytest.raises(asyncpg.RaiseError, match="append-only"):
                await db.execute(f"DELETE FROM {table}")
        for role in ("anon", "authenticated"):
            for table in (
                "stripe_emote_funding",
                "stripe_emote_adjustment_audit",
                "stripe_emote_adjustments",
                "stripe_emote_suspensions",
            ):
                async with db.transaction() as tx:
                    await tx.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
                    await tx.execute(f"SET LOCAL ROLE {role}")
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await tx.fetch(f"SELECT * FROM {table}")


@pytest.mark.parametrize("state", ["pending", "requires_action", "failed", "canceled"])
async def test_unconfirmed_or_failed_refund_does_not_revoke(database_factory, monkeypatch, state):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, _, _, _, _ = await purchase(db, monkeypatch)
        assert (
            await post(db, monkeypatch, cash_event(topup, kind="refund", state=state))
        ).status_code == 200
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "active"


async def test_terminal_refund_conflict_stays_restricted_and_requires_review(
    database_factory, monkeypatch
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, _, _, _, _ = await purchase(db, monkeypatch)
        assert (
            await post(
                db,
                monkeypatch,
                cash_event(topup, kind="refund", state="failed", object_id="re_conflict"),
            )
        ).status_code == 200
        result = await post(
            db,
            monkeypatch,
            cash_event(
                topup, kind="refund", state="succeeded", object_id="re_conflict", created=999
            ),
        )
        assert result.status_code == 200
        assert result.json()["outcome"] == "conflict"
        assert await db.fetchval("SELECT needs_review FROM stripe_emote_adjustments")
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "suspended"


@pytest.mark.parametrize("change", ["mode", "amount", "currency", "charge_amount", "customer"])
async def test_mismatched_charge_facts_never_commit(database_factory, monkeypatch, change):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, _, _, _, _ = await purchase(db, monkeypatch)
        event = cash_event(topup, kind="charge_refund")
        if change == "mode":
            event["livemode"] = True
        else:
            field, value = {
                "amount": ("amount_refunded", 1001),
                "currency": ("currency", "eur"),
                "charge_amount": ("amount", 999),
                "customer": ("customer", "cus_other"),
            }[change]
            event["data"]["object"][field] = value
        assert (await post(db, monkeypatch, event)).status_code == 503
        assert await db.fetchval("SELECT count(*) FROM stripe_webhook_events") == 1
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "active"


async def test_won_dispute_does_not_clear_independent_suspension(database_factory, monkeypatch):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, _, _, _, _ = await purchase(db, monkeypatch)
        await db.execute("UPDATE cosmetic_entitlements SET status='suspended'")
        assert (await post(db, monkeypatch, cash_event(topup, state="won"))).status_code == 200
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "suspended"


async def test_existing_funding_cannot_be_assigned_to_another_user(database_factory, monkeypatch):
    import asyncpg

    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, _, _, _, _ = await purchase(db, monkeypatch)
        assert (await post(db, monkeypatch, cash_event(topup))).status_code == 200
        other = str(await _user(db))
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await db.execute("UPDATE stripe_emote_funding SET user_id=$1", other)


async def test_guard_blocks_new_purchase_while_cash_issue_is_unresolved(
    database_factory, monkeypatch
):
    # Retain the direct hook contract as well as the full StoreService tests below.
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, user, _, _, order = await purchase(db, monkeypatch)
        async with db.transaction() as tx:
            await adjustments.lock_user_adjustments(tx, user)
            await adjustments.check_purchase_adjustments(tx, user_id=user, order_id=order.order_id)
        assert (await post(db, monkeypatch, cash_event(topup))).status_code == 200
        with pytest.raises(adjustments.AdjustmentError, match="PAYMENT_ADJUSTMENT"):
            async with db.transaction() as tx:
                await adjustments.lock_user_adjustments(tx, user)
                await adjustments.check_purchase_adjustments(
                    tx, user_id=user, order_id=order.order_id
                )


async def test_changed_ledger_evidence_fails_closed_without_clearing_hold(
    database_factory, monkeypatch
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, _, _, _, _ = await purchase(db, monkeypatch)
        assert (
            await post(db, monkeypatch, cash_event(topup, object_id="du_evidence"))
        ).status_code == 200
        await db.execute("UPDATE ledger_entries SET amount=1 WHERE entry_type='redeem'")
        event = cash_event(topup, state="won", object_id="du_evidence")
        assert (await post(db, monkeypatch, event)).status_code == 503
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "suspended"
        assert await db.fetchval("SELECT state FROM stripe_emote_adjustments") == "needs_response"


async def test_store_suspension_denies_access_and_won_dispute_restores_it(
    database_factory, monkeypatch
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, user, service, wallet, order = await purchase(db, monkeypatch)
        assert (
            await post(db, monkeypatch, cash_event(topup, object_id="du_access"))
        ).status_code == 200
        owned = await service.list_owned(user)
        assert owned["equipped"] == {}
        assert owned["entitlements"][0]["effective_status"] == "suspended"
        assert owned["entitlements"][0]["activatable"] is False
        with pytest.raises(EntitlementDenied):
            await service.equip(user, "theme", order.item_id)
        with pytest.raises(EntitlementDenied):
            async with service.delivery_asset(user, order.item_id):
                pytest.fail("Suspended private asset was delivered")
        with pytest.raises(EntitlementDenied):
            await service.buy(user, order.item_id, "new-request")
        key = await db.fetchval(
            "SELECT idempotency_key FROM store_orders WHERE id=$1", order.order_id
        )
        replay = await service.buy(user, order.item_id, key)
        assert replay.replayed
        assert replay.entitlement["effective_status"] == "suspended"
        assert await wallet.get_balance(f"user:{user}") == 0
        assert (
            await post(
                db, monkeypatch, cash_event(topup, object_id="du_access", state="won", created=101)
            )
        ).status_code == 200
        await service.equip(user, "theme", order.item_id)
        async with service.delivery_asset(user, order.item_id) as asset:
            assert asset["pack_id"] == order.item_id


async def test_purchase_proof_rolls_back_with_postcheck_failure(database_factory, monkeypatch):
    from openvegas.store import service as module

    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup = await _topup(db)
        user = str(topup[0])
        await _billing(db).handle_webhook(**_signed(_checkout_event(topup)))
        wallet = WalletService(db)
        service = StoreService(db, wallet)
        real_check = module.check_purchase_adjustments

        async def fail_after_proof(tx, **kwargs):
            await real_check(tx, **kwargs)
            assert await tx.fetchval("SELECT count(*) FROM stripe_emote_funding") == 1
            raise adjustments.AdjustmentError("injected postcheck fault")

        monkeypatch.setattr(module, "check_purchase_adjustments", fail_after_proof)
        with pytest.raises(StoreError, match="injected postcheck"):
            await buy(db, monkeypatch, service, user, 500)
        for table in (
            "store_orders",
            "cosmetic_entitlements",
            "stripe_emote_funding",
            "cosmetic_entitlement_events",
        ):
            assert await db.fetchval(f"SELECT count(*) FROM {table}") == 0
        assert (
            await db.fetchval("SELECT count(*) FROM ledger_entries WHERE entry_type='redeem'") == 0
        )
        assert await wallet.get_balance(f"user:{user}") == 1000


@pytest.mark.parametrize("first_actor", ["purchase", "webhook"])
async def test_purchase_and_webhook_race_serializes_before_sku_and_wallet(
    database_factory, monkeypatch, first_actor
):
    from openvegas.store import service as module

    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, user, service, wallet, _ = await purchase(db, monkeypatch, amount=500)
        entered, release = asyncio.Event(), asyncio.Event()
        event = cash_event(topup)
        buy_task = webhook_task = None
        if first_actor == "purchase":
            real = module.check_purchase_adjustments

            async def paused(tx, **kwargs):
                entered.set()
                await release.wait()
                return await real(tx, **kwargs)

            monkeypatch.setattr(module, "check_purchase_adjustments", paused)
        else:
            real = adjustments._apply_entitlements

            async def paused(tx, **kwargs):
                entered.set()
                await release.wait()
                return await real(tx, **kwargs)

            monkeypatch.setattr(adjustments, "_apply_entitlements", paused)
        try:
            if first_actor == "purchase":
                buy_task = asyncio.create_task(buy(db, monkeypatch, service, user, 500))
            else:
                webhook_task = asyncio.create_task(post(db, monkeypatch, event))
            await asyncio.wait_for(entered.wait(), 5)
            if first_actor == "purchase":
                webhook_task = asyncio.create_task(post(db, monkeypatch, event))
                blocked = webhook_task
            else:
                buy_task = asyncio.create_task(buy(db, monkeypatch, service, user, 500))
                blocked = buy_task
            await asyncio.sleep(0.05)
            assert not blocked.done(), "Second operation bypassed the account policy lock"
            release.set()
            bought, delivered = await asyncio.wait_for(
                asyncio.gather(buy_task, webhook_task, return_exceptions=True), 12
            )
        finally:
            release.set()
            for task in (buy_task, webhook_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(t for t in (buy_task, webhook_task) if t is not None), return_exceptions=True
            )
        assert delivered.status_code == 200
        if first_actor == "purchase":
            assert not isinstance(bought, BaseException), repr(bought)
            count, balance = 2, 0
        else:
            assert isinstance(bought, StoreError), repr(bought)
            assert "PAYMENT_ADJUSTMENT" in str(bought)
            count, balance = 1, 500
        assert (
            await db.fetchval("SELECT count(*) FROM cosmetic_entitlements WHERE status='suspended'")
            == count
        )
        assert await db.fetchval("SELECT count(*) FROM store_orders") == count
        assert await db.fetchval("SELECT count(*) FROM stripe_emote_funding") == count
        assert (
            await db.fetchval("SELECT count(*) FROM ledger_entries WHERE entry_type='redeem'")
            == count
        )
        assert await wallet.get_balance(f"user:{user}") == balance
        assert await db.fetchval("SELECT count(*) FROM cosmetic_equipment") == 0


async def test_unattributed_dispute_blocks_new_cosmetics_without_revoking_old(
    database_factory, monkeypatch
):
    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        topup, user, service, wallet, _ = await purchase(db, monkeypatch, mixed=True, amount=100)
        assert (await post(db, monkeypatch, cash_event(topup))).status_code == 200
        with pytest.raises(StoreError, match="PAYMENT_ADJUSTMENT"):
            await buy(db, monkeypatch, service, user, 50)
        assert await db.fetchval("SELECT count(*) FROM store_orders") == 1
        assert await db.fetchval("SELECT count(*) FROM stripe_emote_funding") == 0
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "active"
        assert await wallet.get_balance(f"user:{user}") == 1000


async def test_pre_hook_legacy_purchase_can_be_attributed_only_from_proven_facts(
    database_factory, monkeypatch
):
    from openvegas.store import service as module

    async def pre_043_no_funding_record(*args, **kwargs):
        return None

    async with database_factory(through=43) as sandbox:
        db = sandbox.db
        with monkeypatch.context() as legacy:
            legacy.setattr(module, "check_purchase_adjustments", pre_043_no_funding_record)
            topup, _, _, _, _ = await purchase(db, monkeypatch)
        assert await db.fetchval("SELECT count(*) FROM stripe_emote_funding") == 0
        assert (await post(db, monkeypatch, cash_event(topup, state="lost"))).status_code == 200
        assert await db.fetchval("SELECT count(*) FROM stripe_emote_funding") == 1
        assert await db.fetchval("SELECT status FROM cosmetic_entitlements") == "revoked"
