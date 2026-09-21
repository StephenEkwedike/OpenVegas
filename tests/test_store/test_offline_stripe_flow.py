"""Offline Stripe test-event -> real billing/ledger -> cosmetic service integration.

Only the existing DB fake and existing local signing fixture are used. This is
not a Stripe sandbox API/checkout proof or a PostgreSQL transaction/RLS proof.
"""

import asyncio
import os
import socket
import uuid
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import asyncpg
import pytest
import stripe

from openvegas.payments.service import BillingError, BillingService, WebhookVerificationError
from openvegas.payments.stripe_gateway import StripeGateway
from openvegas.store.catalog import STORE_CATALOG, public_catalog
from openvegas.store.service import CosmeticUnavailable, StoreService
from openvegas.wallet.ledger import InsufficientBalance, WalletService
from tests.integration.test_restoration_db import _checkout_event, _signed
from tests.test_store.fakes import StoreFakeDB, StoreFakeTx, normalized


class OfflineDB(StoreFakeDB):
    def __init__(self):
        super().__init__()
        self.state.update(topups={}, webhooks={}, ledger={})

    def transaction(self):
        return OfflineTx(self)

    async def fetchrow(self, query, *args):
        return await OfflineTx(self).fetchrow(query, *args)


class OfflineTx(StoreFakeTx):
    def __init__(self, db):
        super().__init__(db)
        self.savepoints = {}
        self.lock_names = set()

    async def lock(self, name):
        if name not in self.lock_names:
            lock = self.db.locks.setdefault(name, asyncio.Lock())
            await lock.acquire()
            self.held.append(lock)
            self.lock_names.add(name)

    async def execute(self, query, *args):
        q = normalized(query)
        if q.startswith("SAVEPOINT "):
            self.savepoints[q.split()[-1]] = deepcopy(self.db.state)
            return "SAVEPOINT"
        if q.startswith("ROLLBACK TO SAVEPOINT "):
            self.db.state = deepcopy(self.savepoints[q.split()[-1]])
            return "ROLLBACK"
        if q.startswith("RELEASE SAVEPOINT "):
            self.savepoints.pop(q.split()[-1])
            return "RELEASE"
        if q.startswith("SELECT account_id FROM wallet_accounts"):
            assert "ORDER BY account_id FOR UPDATE" in q
            for account in args[0]:
                await self.lock(("wallet", account))
            return "SELECT 1"
        if q.startswith("INSERT INTO stripe_webhook_events"):
            await self.lock(("webhook", args[0]))
            self.record(q, args)
            if args[0] in self.db.state["webhooks"]:
                return "INSERT 0 0"
            self.write()
            self.db.state["webhooks"][args[0]] = {"payload_hash": args[2]}
            return "INSERT 0 1"
        if q.startswith("INSERT INTO wallet_accounts"):
            self.record(q, args)
            self.write()
            self.db.state["balances"].setdefault(args[0], Decimal(0))
            return "INSERT 0 1"
        if q.startswith("INSERT INTO ledger_entries"):
            self.record(q, args)
            self.write()
            entry_id, debit, credit, amount, entry_type, reference = args
            key = (reference, entry_type, debit, credit)
            if key in self.db.state["ledger"]:
                return "INSERT 0 0"
            self.db.state["ledger"][key] = {
                "id": entry_id,
                "debit_account": debit,
                "credit_account": credit,
                "amount": amount,
                "entry_type": entry_type,
                "reference_id": reference,
            }
            return "INSERT 0 1"
        if q.startswith("UPDATE wallet_accounts SET balance = balance"):
            self.record(q, args)
            self.write()
            amount, account = args
            value = self.db.state["balances"][account]
            value += -amount if "balance -" in q else amount
            if account.startswith(("user:", "agent:")) and value < 0:
                error = ValueError("fake SQL nonnegative account constraint")
                error.sqlstate = "23514"
                error.constraint_name = "ck_wallet_nonnegative_user_agent"
                raise error
            self.db.state["balances"][account] = value
            return "UPDATE 1"
        return await super().execute(query, *args)

    async def fetchrow(self, query, *args):
        q = normalized(query)
        if q.startswith("SELECT payload_hash FROM stripe_webhook_events"):
            row = deepcopy(self.db.state["webhooks"].get(args[0]))
            await asyncio.sleep(0)
            return row
        if "FROM fiat_topups" in q and "stripe_checkout_session_id = $1" in q:
            row = next(
                (
                    r
                    for r in self.db.state["topups"].values()
                    if r["stripe_checkout_session_id"] == args[0]
                ),
                None,
            )
            if row:
                await self.lock(("topup", row["id"]))
                return deepcopy(self.db.state["topups"][row["id"]])
            return None
        if "FROM fiat_topups" in q and "stripe_payment_intent_id = $1" in q:
            return next(
                (
                    deepcopy(r)
                    for r in self.db.state["topups"].values()
                    if r.get("stripe_payment_intent_id") == args[0] and r["id"] != args[1]
                ),
                None,
            )
        if q.startswith("UPDATE fiat_topups SET status = 'paid'"):
            self.record(q, args)
            self.write()
            row = self.db.state["topups"][args[0]]
            row.update(status="paid", stripe_payment_intent_id=args[1])
            return deepcopy(row)
        if "FROM user_continuation_credit" in q:
            return None
        if q.startswith("SELECT balance FROM wallet_accounts"):
            return {"balance": self.db.state["balances"].get(args[0], Decimal(0))}
        if q.startswith("SELECT amount FROM ledger_entries"):
            return deepcopy(self.db.state["ledger"].get(tuple(args)))
        return await super().fetchrow(query, *args)


@pytest.fixture
def offline(monkeypatch):
    # Replace, do not read/copy, the ambient environment. Reuse the repository's
    # existing nonfunctional local signature-fixture strings; no credentials issued.
    monkeypatch.setattr(
        os,
        "environ",
        {
            "OPENVEGAS_RUNTIME_ENV": "test",
            "OPENVEGAS_DEMO_ADMIN_AUTOFUND_ENABLED": "0",
            "OPENVEGAS_BILLING_PROVIDER": "stripe",
            "STRIPE_SECRET_KEY": "sk_test_local_signature_only_no_network",
            "STRIPE_WEBHOOK_SECRET": "whsec_local_integration_signature_only",
        },
    )

    def blocked(*args, **kwargs):
        raise AssertionError("Offline store test forbids network and real database access")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(asyncpg, "connect", blocked)
    monkeypatch.setattr(asyncpg, "create_pool", blocked)
    # No Stripe Customer/Checkout/PaymentIntent API surface is provided at all.
    sdk = SimpleNamespace(Webhook=stripe.Webhook)
    gateway = StripeGateway(stripe_mod=sdk)
    assert sdk.api_key == "sk_test_local_signature_only_no_network"
    db = OfflineDB()
    wallet = WalletService(db)
    billing = BillingService(db, wallet, gateway)
    store = StoreService(db, wallet)
    topup_id = str(uuid.uuid4())
    row = {
        "id": topup_id,
        "user_id": "alice",
        "amount_usd": Decimal(10),
        "v_credit": Decimal(1000),
        "status": "checkout_created",
        "mode": "stripe",
        "stripe_checkout_session_id": "cs_test_local_store",
        "stripe_customer_id": "cus_local_store",
        "expires_at": None,
    }
    db.state["topups"][topup_id] = row
    event = _checkout_event(
        ("alice", topup_id, row["stripe_checkout_session_id"], row["stripe_customer_id"])
    )

    async def deliver(payload=event):
        assert os.environ["OPENVEGAS_RUNTIME_ENV"] == "test"
        assert os.environ["STRIPE_SECRET_KEY"] == "sk_test_local_signature_only_no_network"
        assert payload.get("livemode") is False, "Offline harness refuses live-mode events"
        return await billing.handle_webhook(**_signed(payload))

    return SimpleNamespace(
        db=db,
        wallet=wallet,
        store=store,
        billing=billing,
        topup_id=topup_id,
        event=event,
        deliver=deliver,
    )


def entries(flow, kind):
    return [r for r in flow.db.state["ledger"].values() if r["entry_type"] == kind]


@pytest.mark.asyncio
async def test_offline_signed_test_topup_wallet_entitlement_replays(offline, approved):
    flow = offline
    with pytest.raises(InsufficientBalance):
        await flow.store.buy("alice", "test_emote", "purchase")
    assert await flow.wallet.get_balance("user:alice") == 0
    assert (await flow.deliver())["status"] == "paid"
    assert await flow.wallet.get_balance("user:alice") == Decimal(1000)
    assert (await flow.deliver())["status"] == "duplicate"
    second = deepcopy(flow.event)
    second["id"] += "_different_delivery"
    assert (await flow.deliver(second))["idempotent"] is True
    results = await asyncio.gather(
        *(flow.store.buy("alice", "test_emote", f"purchase-{i}") for i in range(12))
    )
    assert len({r.order_id for r in results}) == 1
    assert len(entries(flow, "fiat_topup")) == len(entries(flow, "redeem")) == 1
    assert await flow.wallet.get_balance("user:alice") == Decimal("997.5")
    assert sum(flow.db.state["balances"].values()) == 0  # double-entry conservation
    await flow.store.equip("alice", None, "test_emote")
    assert (await flow.store.list_owned("alice"))["equipped"] == {"completion": "test_emote"}
    assert (await flow.store.list_owned("bob"))["entitlements"] == []


@pytest.mark.asyncio
async def test_offline_topup_credit_failure_rolls_back_journal_status_and_ledger(offline):
    flow = offline
    flow.db.fail = "UPDATE wallet_accounts SET balance = balance +"
    with pytest.raises(RuntimeError, match="injected"):
        await flow.deliver()
    assert flow.db.state["webhooks"] == {}
    assert flow.db.state["ledger"] == {}
    assert flow.db.state["balances"] == {}
    assert flow.db.state["topups"][flow.topup_id]["status"] == "checkout_created"
    flow.db.fail = None
    assert (await flow.deliver())["status"] == "paid"
    assert len(entries(flow, "fiat_topup")) == 1


@pytest.mark.asyncio
async def test_offline_cosmetic_failure_preserves_committed_topup_and_retries_once(
    offline, approved
):
    flow = offline
    await flow.deliver()
    flow.db.fail = "final_transition"
    with pytest.raises(RuntimeError, match="injected"):
        await flow.store.buy("alice", "test_emote", "purchase")
    assert await flow.wallet.get_balance("user:alice") == 1000
    assert len(entries(flow, "fiat_topup")) == 1
    assert entries(flow, "redeem") == []
    assert flow.db.state["orders"] == flow.db.state["entitlements"] == {}
    flow.db.fail = None
    await flow.store.buy("alice", "test_emote", "purchase")
    await flow.store.buy("alice", "test_emote", "purchase")
    assert len(entries(flow, "redeem")) == 1
    assert (await flow.deliver())["status"] == "duplicate"


@pytest.mark.asyncio
async def test_offline_unpaid_canceled_and_bad_signature_never_credit(offline):
    flow = offline
    for suffix, event_type, payment_status in (
        ("_pending", "checkout.session.completed", "unpaid"),
        ("_canceled", "checkout.session.expired", "unpaid"),
    ):
        event = deepcopy(flow.event)
        event.update(id=event["id"] + suffix, type=event_type)
        event["data"]["object"]["payment_status"] = payment_status
        assert (await flow.deliver(event))["status"] in {"ignored", "not-paid"}
    signed = _signed(flow.event)
    signed["raw_body"] += b" "
    with pytest.raises(WebhookVerificationError):
        await flow.billing.handle_webhook(**signed)
    assert await flow.wallet.get_balance("user:alice") == 0
    assert flow.db.state["ledger"] == flow.db.state["entitlements"] == {}
    live = deepcopy(flow.event)
    live["livemode"] = True
    with pytest.raises(AssertionError, match="refuses live-mode"):
        await flow.deliver(live)


@pytest.mark.asyncio
async def test_offline_event_payload_conflict_cannot_credit_twice(offline):
    await offline.deliver()
    conflict = deepcopy(offline.event)
    conflict["data"]["object"]["amount_total"] = 2000
    with pytest.raises(BillingError, match="payload hash mismatch"):
        await offline.deliver(conflict)
    assert len(entries(offline, "fiat_topup")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("same_event", [True, False])
async def test_offline_concurrent_webhooks_credit_once(offline, same_event):
    events = [deepcopy(offline.event) for _ in range(12)]
    if not same_event:
        for index, event in enumerate(events):
            event["id"] += f"_{index}"
    await asyncio.gather(*(offline.deliver(event) for event in events))
    assert len(entries(offline, "fiat_topup")) == 1
    assert await offline.wallet.get_balance("user:alice") == 1000
    assert offline.db.state["entitlements"] == {}


@pytest.mark.asyncio
async def test_funded_wallet_cannot_buy_real_preview_only_catalog(offline):
    await offline.deliver()
    catalog = public_catalog(cosmetics_only=True)
    for sku, item in catalog.items():
        assert item["cost_v"] is None
        assert item["purchasable"] is False
        assert STORE_CATALOG[sku]["sale_enabled"] is False
        with pytest.raises(CosmeticUnavailable, match="PREVIEW_ONLY"):
            await offline.store.buy("alice", sku, f"preview-{sku}")
    assert len(entries(offline, "fiat_topup")) == 1
    assert entries(offline, "redeem") == []
    assert offline.db.state["entitlements"] == {}
