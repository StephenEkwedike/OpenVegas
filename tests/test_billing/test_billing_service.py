from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from openvegas.payments.service import BillingError, BillingService
from openvegas.payments.stripe_gateway import StripeGateway


class _DummyWallet:
    def __init__(self, balance: Decimal = Decimal("0")):
        self.calls = []
        self._balance = Decimal(str(balance))

    async def fund_from_card(self, *, account_id, amount_v, reference_id, tx=None):
        self.calls.append((account_id, amount_v, reference_id, tx))

    async def get_balance(self, account_id: str) -> Decimal:
        _ = account_id
        return self._balance


class _TxCtx:
    def __init__(self, tx):
        self.tx = tx

    async def __aenter__(self):
        return self.tx

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeDB:
    def __init__(self, tx=None):
        self.tx = tx or _FakeTx()

    def transaction(self):
        return _TxCtx(self.tx)

    async def fetchrow(self, query: str, *args):
        return await self.tx.fetchrow(query, *args)

    async def execute(self, query: str, *args):
        return await self.tx.execute(query, *args)


class _FakeTx:
    def __init__(self):
        self.fetchrow_calls = []
        self.execute_calls = []
        self.mode = {}

    async def fetchrow(self, query: str, *args):
        self.fetchrow_calls.append((query, args))
        if "SELECT payload_hash FROM stripe_webhook_events" in query:
            return self.mode.get("existing_event")
        if "SELECT *" in query and "WHERE stripe_checkout_session_id" in query:
            return self.mode.get("checkout_row")
        if "SELECT *" in query and "WHERE id = $1" in query and "FOR UPDATE" in query:
            return self.mode.get("topup_row_for_update")
        if "SELECT * FROM fiat_topups WHERE id = $1" in query:
            return self.mode.get("topup_row_latest")
        if "WHERE user_id = $1 AND idempotency_key = $2" in query:
            return self.mode.get("existing_topup_by_idem")
        if "WHERE user_id = $1" in query and "status IN ('created', 'checkout_created')" in query:
            return self.mode.get("pending_row")
        if "UPDATE fiat_topups" in query and "RETURNING id, user_id, v_credit, status" in query:
            return self.mode.get("paid_update_row")
        if "SELECT org_id FROM org_sponsorships WHERE stripe_subscription_id" in query:
            return self.mode.get("org_from_subscription")
        if "FROM user_subscriptions" in query and "stripe_customer_id" in query:
            return self.mode.get("subscription_customer_row")
        if "SELECT id" in query and "WHERE stripe_payment_intent_id" in query:
            return self.mode.get("provider_conflict_row")
        return None

    async def execute(self, query: str, *args):
        self.execute_calls.append((query, args))
        return "OK"


def _svc(db=None, wallet=None, gateway=None):
    return BillingService(
        db or _FakeDB(),
        wallet or _DummyWallet(),
        stripe_gateway=gateway or types.SimpleNamespace(mode="stripe"),
    )


def test_canonical_payload_hash_normalizes_decimal_and_key_order():
    a = {"amount_usd": Decimal("10.00"), "currency": "usd"}
    b = {"currency": "usd", "amount_usd": Decimal("10")}
    assert BillingService.canonical_payload_hash(a) == BillingService.canonical_payload_hash(b)


def test_compute_has_active_subscription_with_period_end():
    future = int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
    past = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
    assert BillingService.compute_has_active_subscription({"status": "active", "current_period_end": future})
    assert not BillingService.compute_has_active_subscription({"status": "active", "current_period_end": past})
    assert not BillingService.compute_has_active_subscription({"status": "past_due", "current_period_end": future})


@pytest.mark.asyncio
async def test_resolve_org_id_prefers_subscription_metadata():
    svc = _svc()
    tx = _FakeTx()
    org_id = await svc.resolve_org_id_from_subscription(
        {"id": "sub_1", "metadata": {"org_id": "org_123"}},
        tx=tx,
    )
    assert org_id == "org_123"
    assert not tx.fetchrow_calls


@pytest.mark.asyncio
async def test_resolve_org_id_fallbacks_to_subscription_lookup():
    tx = _FakeTx()
    tx.mode["org_from_subscription"] = {"org_id": "org_fallback"}
    svc = _svc()
    org_id = await svc.resolve_org_id_from_subscription(
        {"id": "sub_2", "metadata": {}},
        tx=tx,
    )
    assert org_id == "org_fallback"


@pytest.mark.asyncio
async def test_handle_event_rejects_payload_hash_mismatch():
    tx = _FakeTx()
    tx.mode["existing_event"] = {"payload_hash": "different"}
    svc = _svc(db=_FakeDB(tx))

    event = {"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {"id": "cs_1"}}}
    with pytest.raises(BillingError):
        await svc.handle_event(event)


@pytest.mark.asyncio
async def test_settle_topup_requires_paid_status():
    wallet = _DummyWallet()
    svc = _svc(wallet=wallet)
    tx = _FakeTx()
    res = await svc._settle_topup_from_checkout(
        tx=tx,
        session={"id": "cs_123", "payment_status": "unpaid"},
    )
    assert res["status"] == "not-paid"
    assert wallet.calls == []


@pytest.mark.asyncio
async def test_settle_topup_paid_credits_wallet():
    wallet = _DummyWallet()
    svc = _svc(wallet=wallet)
    tx = _FakeTx()
    tx.mode["checkout_row"] = {
        "id": "top_1",
        "user_id": "u1",
        "v_credit": "12.500000",
        "status": "checkout_created",
        "mode": "stripe",
        "expires_at": None,
    }
    tx.mode["paid_update_row"] = {"id": "top_1", "user_id": "u1", "v_credit": "12.500000", "status": "paid"}

    res = await svc._settle_topup_from_checkout(
        tx=tx,
        session={"id": "cs_paid", "payment_status": "paid", "payment_intent": "pi_1"},
    )
    assert res["status"] == "paid"
    assert wallet.calls[0][0] == "user:u1"
    assert wallet.calls[0][1] == Decimal("12.500000")
    assert wallet.calls[0][2] == "fiat_topup:top_1"


@pytest.mark.asyncio
async def test_settle_expired_simulated_topup_rejected():
    svc = _svc()
    tx = _FakeTx()
    with pytest.raises(BillingError, match="SIMULATED_TOPUP_EXPIRED_CANNOT_SETTLE"):
        await svc._settle_topup_paid(
            tx=tx,
            row={
                "id": "top_sim_exp",
                "user_id": "u1",
                "status": "expired",
                "mode": "simulated",
                "expires_at": datetime.now(timezone.utc) - timedelta(hours=1),
                "v_credit": "10.000000",
            },
            provider_ref="fake:top_sim_exp",
            settlement_surface="simulated",
            provider_paid_at=datetime.now(timezone.utc),
        )


@pytest.mark.asyncio
async def test_suggest_above_floor_returns_low_balance_false(monkeypatch):
    monkeypatch.setenv("V_PER_USD", "100")
    monkeypatch.setenv("TOPUP_LOW_BALANCE_FLOOR_USD", "5.00")
    wallet = _DummyWallet(balance=Decimal("900.000000"))
    svc = _svc(wallet=wallet)
    out = await svc.create_topup_suggestion(user_id="u1")
    assert out["low_balance"] is False
    assert out["topup_id"] is None
    assert out["checkout_url"] is None


@pytest.mark.asyncio
async def test_suggest_low_balance_reuses_existing_pending_topup(monkeypatch):
    monkeypatch.setenv("V_PER_USD", "100")
    monkeypatch.setenv("TOPUP_LOW_BALANCE_FLOOR_USD", "5.00")
    wallet = _DummyWallet(balance=Decimal("200.000000"))
    tx = _FakeTx()
    tx.mode["pending_row"] = {
        "id": "top_pending",
        "user_id": "u1",
        "amount_usd": "20.00",
        "v_credit": "2000.000000",
        "status": "checkout_created",
        "mode": "simulated",
        "stripe_checkout_session_id": "fake_cs_1",
        "stripe_checkout_url": "https://checkout.openvegas.local/topup/top_pending",
        "stripe_payment_intent_id": None,
        "updated_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
    }

    svc = _svc(db=_FakeDB(tx=tx), wallet=wallet, gateway=types.SimpleNamespace(mode="simulated"))
    out = await svc.create_topup_suggestion(user_id="u1")
    assert out["low_balance"] is True
    assert out["topup_id"] == "top_pending"
    assert out["checkout_url"] == "https://checkout.openvegas.local/topup/top_pending"


def test_stripe_gateway_falls_back_to_options_idempotency(monkeypatch):
    class _FakeSessionAPI:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            if "idempotency_key" in kwargs:
                raise TypeError("unexpected keyword")
            self.kwargs = kwargs
            return {"id": "cs_1", "url": "https://checkout", "payment_intent": "pi_1"}

    session_api = _FakeSessionAPI()
    fake_stripe = types.SimpleNamespace(
        api_key="",
        checkout=types.SimpleNamespace(Session=session_api),
        Customer=types.SimpleNamespace(create=lambda **kwargs: {"id": "cus_1"}),
        billing_portal=types.SimpleNamespace(Session=types.SimpleNamespace(create=lambda **kwargs: {"url": "https://portal"})),
        Webhook=types.SimpleNamespace(construct_event=lambda **kwargs: {"id": "evt"}),
    )
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setenv("CHECKOUT_SUCCESS_URL", "http://localhost/success")
    monkeypatch.setenv("CHECKOUT_CANCEL_URL", "http://localhost/cancel")

    gw = StripeGateway(stripe_mod=fake_stripe)
    out = gw.create_topup_checkout(
        customer_id="cus_1",
        amount_usd=Decimal("5"),
        topup_id="topup_1",
    )
    assert out["id"] == "cs_1"
    assert session_api.kwargs["options"]["idempotency_key"] == "topup-checkout:topup_1"
    assert session_api.kwargs["client_reference_id"] == "topup_1"


def test_render_qr_svg_includes_runtime_reason_when_dependency_missing(monkeypatch):
    monkeypatch.setattr(
        "openvegas.payments.service.ensure_qrcode_available",
        lambda: (False, "ModuleNotFoundError: No module named 'qrcode'"),
    )
    payload = BillingService._render_qr_svg("https://checkout.stripe.com/c/pay/cs_live_demo")
    text = payload.decode("utf-8")
    assert "QR unavailable in this runtime" in text
    assert "reason: ModuleNotFoundError: No module named 'qrcode'" in text


@pytest.mark.asyncio
async def test_ensure_user_customer_uses_only_real_stripe_customer_ids():
    class _Tx(_FakeTx):
        async def fetchrow(self, query: str, *args):
            self.fetchrow_calls.append((query, args))
            if "SELECT stripe_customer_id" in query and "mode = 'stripe'" in query:
                # Simulated ids should never be reused for live Stripe checkouts.
                return None
            if "SELECT email FROM auth.users" in query:
                return {"email": "qa@example.com"}
            return None

    tx = _Tx()
    gateway = types.SimpleNamespace(
        mode="stripe",
        create_customer=lambda **kwargs: {"id": "cus_new_123"},
    )
    svc = BillingService(_FakeDB(tx=tx), _DummyWallet(), gateway)
    customer_id = await svc._ensure_user_customer("u1")
    assert customer_id == "cus_new_123"


@pytest.mark.asyncio
async def test_create_topup_checkout_recovers_from_legacy_sim_customer_id(monkeypatch):
    monkeypatch.setenv("TOPUP_MIN_USD", "1")
    monkeypatch.setenv("TOPUP_MAX_USD", "500")
    monkeypatch.setenv("V_PER_USD", "100")

    class _Tx(_FakeTx):
        async def fetchrow(self, query: str, *args):
            self.fetchrow_calls.append((query, args))
            if "WHERE user_id = $1 AND idempotency_key = $2" in query:
                return None
            if "UPDATE fiat_topups" in query and "RETURNING *" in query:
                return {
                    "id": "top_retry_1",
                    "status": "checkout_created",
                    "mode": "stripe",
                    "amount_usd": Decimal("20.00"),
                    "v_credit": Decimal("2000.000000"),
                    "stripe_checkout_session_id": "cs_retry_1",
                    "stripe_checkout_url": "https://checkout.stripe.com/c/pay/cs_retry_1",
                    "stripe_payment_intent_id": None,
                    "updated_at": datetime.now(timezone.utc),
                }
            return None

    class _RetryGateway:
        mode = "stripe"

        def __init__(self):
            self.calls = 0
            self.seen_customers: list[str] = []

        def create_topup_checkout(self, *, customer_id: str, amount_usd: Decimal, topup_id: str):
            _ = (amount_usd, topup_id)
            self.calls += 1
            self.seen_customers.append(customer_id)
            if self.calls == 1:
                raise RuntimeError("Request req_1: No such customer: 'sim_u1'")
            return {"id": "cs_retry_1", "url": "https://checkout.stripe.com/c/pay/cs_retry_1"}

    tx = _Tx()
    gateway = _RetryGateway()
    svc = BillingService(_FakeDB(tx=tx), _DummyWallet(), gateway)

    async def _fake_ensure_user_customer(user_id: str) -> str:
        _ = user_id
        return "sim_u1"

    async def _fake_create_user_customer(user_id: str) -> str:
        _ = user_id
        return "cus_live_1"

    svc._ensure_user_customer = _fake_ensure_user_customer  # type: ignore[method-assign]
    svc._create_user_customer = _fake_create_user_customer  # type: ignore[method-assign]

    out = await svc.create_topup_checkout(
        user_id="u1",
        amount_usd=Decimal("20.00"),
        idempotency_key="idem-retry-1",
    )

    assert out["status"] == "checkout_created"
    assert out["mode"] == "stripe"
    assert gateway.calls == 2
    assert gateway.seen_customers == ["sim_u1", "cus_live_1"]


@pytest.mark.asyncio
async def test_create_user_subscription_checkout_uses_user_endpoint_flow(monkeypatch):
    monkeypatch.setenv("USER_SUBSCRIPTION_MIN_USD", "1")
    monkeypatch.setenv("USER_SUBSCRIPTION_MAX_USD", "500")

    class _Gateway:
        mode = "stripe"

        def __init__(self):
            self.calls = []

        def create_user_subscription_checkout(self, **kwargs):
            self.calls.append(kwargs)
            return {"id": "cs_sub_1", "url": "https://checkout.stripe.com/c/pay/cs_sub_1"}

    tx = _FakeTx()
    gateway = _Gateway()
    svc = BillingService(_FakeDB(tx=tx), _DummyWallet(), gateway)

    async def _fake_ensure_user_subscription_customer(user_id: str) -> str:
        assert user_id == "u-sub-1"
        return "cus_sub_1"

    svc._ensure_user_subscription_customer = _fake_ensure_user_subscription_customer  # type: ignore[method-assign]

    out = await svc.create_user_subscription_checkout(
        user_id="u-sub-1",
        monthly_amount_usd=Decimal("20.00"),
    )

    assert out["user_id"] == "u-sub-1"
    assert out["checkout_session_id"] == "cs_sub_1"
    assert out["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_sub_1"
    assert out["monthly_amount_usd"] == "20.00"
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["customer_id"] == "cus_sub_1"
    assert gateway.calls[0]["user_id"] == "u-sub-1"
    assert gateway.calls[0]["monthly_amount_usd"] == Decimal("20.00")
    assert any("INSERT INTO user_subscriptions" in q for q, _ in tx.execute_calls)


@pytest.mark.asyncio
async def test_get_saved_payment_method_status_returns_card_details():
    tx = _FakeTx()
    tx.mode["subscription_customer_row"] = {"stripe_customer_id": "cus_saved_1"}
    gateway = types.SimpleNamespace(
        mode="stripe",
        retrieve_customer=lambda **kwargs: {"id": kwargs["customer_id"], "invoice_settings": {"default_payment_method": "pm_saved_1"}},
        retrieve_payment_method=lambda **kwargs: {
            "id": kwargs["payment_method_id"],
            "card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030},
        },
        list_customer_card_payment_methods=lambda **kwargs: {"data": []},
    )
    svc = BillingService(_FakeDB(tx=tx), _DummyWallet(), gateway)

    out = await svc.get_saved_payment_method_status(user_id="u1")

    assert out["available"] is True
    assert out["payment_method_id"] == "pm_saved_1"
    assert out["brand"] == "visa"
    assert out["last4"] == "4242"


@pytest.mark.asyncio
async def test_charge_saved_topup_settles_paid_topup(monkeypatch):
    monkeypatch.setenv("TOPUP_MIN_USD", "1")
    monkeypatch.setenv("TOPUP_MAX_USD", "500")
    monkeypatch.setenv("V_PER_USD", "100")
    wallet = _DummyWallet()
    tx = _FakeTx()
    tx.mode["subscription_customer_row"] = {"stripe_customer_id": "cus_saved_1"}
    tx.mode["topup_row_for_update"] = {
        "id": "top_saved_1",
        "user_id": "u1",
        "v_credit": "1200.000000",
        "status": "created",
        "mode": "stripe",
        "expires_at": None,
    }
    tx.mode["paid_update_row"] = {"id": "top_saved_1", "user_id": "u1", "v_credit": "1200.000000", "status": "paid"}
    tx.mode["topup_row_latest"] = {
        "id": "top_saved_1",
        "user_id": "u1",
        "status": "paid",
        "mode": "stripe",
        "amount_usd": Decimal("12.00"),
        "v_credit": Decimal("1200.000000"),
        "stripe_checkout_session_id": None,
        "stripe_checkout_url": None,
        "stripe_payment_intent_id": "pi_saved_1",
        "updated_at": datetime.now(timezone.utc),
    }

    gateway = types.SimpleNamespace(
        mode="stripe",
        retrieve_customer=lambda **kwargs: {"id": kwargs["customer_id"], "invoice_settings": {"default_payment_method": "pm_saved_1"}},
        retrieve_payment_method=lambda **kwargs: {
            "id": kwargs["payment_method_id"],
            "card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030},
        },
        list_customer_card_payment_methods=lambda **kwargs: {"data": []},
        create_saved_card_topup_payment_intent=lambda **kwargs: {"id": "pi_saved_1", "status": "succeeded"},
    )
    svc = BillingService(_FakeDB(tx=tx), wallet, gateway)

    out = await svc.charge_saved_topup(
        user_id="u1",
        amount_usd=Decimal("12.00"),
        idempotency_key="idem-saved-1",
    )

    assert out["status"] == "paid"
    assert wallet.calls
    assert wallet.calls[0][0] == "user:u1"


@pytest.mark.asyncio
async def test_invoice_paid_credits_user_wallet_for_user_subscription():
    class _InvoiceTx(_FakeTx):
        async def fetchrow(self, query: str, *args):
            self.fetchrow_calls.append((query, args))
            if "FROM user_subscriptions" in query and "WHERE stripe_subscription_id = $1" in query:
                return {"user_id": "u-invoice", "stripe_customer_id": "cus_invoice"}
            if "FROM fiat_topups" in query and "idempotency_key = $2" in query:
                return None
            return None

    wallet = _DummyWallet()
    tx = _InvoiceTx()
    svc = _svc(db=_FakeDB(tx=tx), wallet=wallet)

    out = await svc._apply_invoice_credit_once(
        tx=tx,
        invoice={
            "id": "in_user_1",
            "subscription": "sub_user_1",
            "amount_paid": 2000,
            "currency": "usd",
            "payment_intent": "pi_user_1",
        },
    )

    assert out["status"] == "credited"
    assert out["user_id"] == "u-invoice"
    assert wallet.calls
    assert wallet.calls[0][0] == "user:u-invoice"
    assert wallet.calls[0][2].startswith("fiat_topup:")
    assert any("INSERT INTO fiat_topups" in q for q, _ in tx.execute_calls)


@pytest.mark.asyncio
async def test_invoice_paid_falls_back_to_customer_lookup_when_subscription_not_synced():
    class _FallbackTx(_FakeTx):
        async def fetchrow(self, query: str, *args):
            self.fetchrow_calls.append((query, args))
            if "FROM user_subscriptions" in query and "WHERE stripe_subscription_id = $1" in query:
                return None
            if "FROM user_subscriptions" in query and "WHERE stripe_customer_id = $1" in query:
                return {"user_id": "u-fallback", "stripe_customer_id": "cus_fallback"}
            if "FROM fiat_topups" in query and "idempotency_key = $2" in query:
                return None
            return None

    wallet = _DummyWallet()
    tx = _FallbackTx()
    svc = _svc(db=_FakeDB(tx=tx), wallet=wallet)

    out = await svc._apply_invoice_credit_once(
        tx=tx,
        invoice={
            "id": "in_user_fb_1",
            "subscription": "sub_user_fb_1",
            "customer": "cus_fallback",
            "amount_paid": 2000,
            "currency": "usd",
            "payment_intent": "pi_user_fb_1",
        },
    )

    assert out["status"] == "credited"
    assert out["user_id"] == "u-fallback"
    assert wallet.calls and wallet.calls[0][0] == "user:u-fallback"
    assert any(
        "UPDATE user_subscriptions" in q and "stripe_subscription_id = COALESCE" in q
        for q, _ in tx.execute_calls
    )


@pytest.mark.asyncio
async def test_invoice_payment_failed_marks_user_subscription_past_due():
    class _PastDueTx(_FakeTx):
        async def fetchrow(self, query: str, *args):
            self.fetchrow_calls.append((query, args))
            if "UPDATE user_subscriptions" in query and "RETURNING user_id" in query:
                return {"user_id": "u-past-due"}
            return None

    tx = _PastDueTx()
    svc = _svc(db=_FakeDB(tx=tx))

    out = await svc._mark_subscription_past_due(
        tx=tx,
        invoice={"subscription": "sub_user_2"},
    )

    assert out["status"] == "past_due"
    assert out["scope"] == "user"
    assert out["user_id"] == "u-past-due"
