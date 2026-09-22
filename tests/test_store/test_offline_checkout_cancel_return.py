"""Browser cancel/abandonment is not a Stripe payment-expiration event.

Real ASGI routes, gateway parameter construction and billing/ledger code; only
local Checkout responses, signed TEST payloads and in-memory SQL fixtures.
No browser JS, provider, PostgreSQL/RLS or production certification is claimed.
The store fixture's adjustment-policy boundary remains synthetic.
"""

import os
import socket
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from openvegas.payments.service import BillingService
from openvegas.store.service import StoreService
from openvegas.wallet.ledger import InsufficientBalance, WalletService
from tests.integration.test_restoration_db import _checkout_event, _signed
from tests.test_store import test_offline_stripe_flow as stripe_flow
from tests.test_store.fakes import normalized

offline = stripe_flow.offline


class CancelReturnDB(stripe_flow.OfflineDB):
    def transaction(self):
        return CancelReturnTx(self)

    async def fetchrow(self, query, *args):
        return await CancelReturnTx(self).fetchrow(query, *args)


class CancelReturnTx(stripe_flow.OfflineTx):
    """Only the SQL needed for checkout creation, retry and local TTL polling."""

    async def execute(self, query, *args):
        q = normalized(query)
        if q.startswith("INSERT INTO fiat_topups"):
            self.record(q, args)
            self.write()
            topup_id, user, amount, credit, key, payload_hash, mode, expiry = args
            assert not any(
                (r["user_id"], r["idempotency_key"]) == (user, key)
                for r in self.db.state["topups"].values()
            )
            self.db.state["topups"][topup_id] = {
                "id": topup_id,
                "user_id": user,
                "amount_usd": amount,
                "v_credit": credit,
                "idempotency_key": key,
                "idempotency_payload_hash": payload_hash,
                "mode": mode,
                "status": "created",
                "expires_at": expiry,
            }
            return "INSERT 0 1"
        return await super().execute(query, *args)

    async def fetchrow(self, query, *args):
        q = normalized(query)
        rows = self.db.state["topups"]
        if q.startswith("SELECT stripe_customer_id FROM fiat_topups"):
            assert args == ("alice",)
            return {"stripe_customer_id": "cus_local_store"}
        if q.startswith("SELECT * FROM fiat_topups WHERE user_id = $1 AND idempotency_key"):
            row = next(
                (r for r in rows.values() if (r["user_id"], r["idempotency_key"]) == args),
                None,
            )
            if row:
                await self.lock(("topup", row["id"]))
            return deepcopy(row)
        if q.startswith("SELECT * FROM fiat_topups WHERE id = $1"):
            row = rows.get(args[0])
            if row and len(args) == 2 and row["user_id"] != args[1]:
                return None
            if row and "FOR UPDATE" in q:
                await self.lock(("topup", row["id"]))
            return deepcopy(row)
        if q.startswith("UPDATE fiat_topups SET status = 'checkout_created'"):
            self.record(q, args)
            row = rows[args[0]]
            if row["status"] not in {"created", "failed", "checkout_created"}:
                return None
            self.write()
            row.update(
                status="checkout_created",
                stripe_customer_id=args[1],
                stripe_checkout_session_id=args[2],
                stripe_checkout_url=args[3],
                mode=args[4],
                expires_at=args[5],
            )
            return deepcopy(row)
        if q.startswith("UPDATE fiat_topups SET status = 'expired'"):
            self.record(q, args)
            row = rows[args[0]]
            if row["status"] not in {"created", "checkout_created"}:
                return None
            self.write()
            row["status"] = "expired"
            return deepcopy(row)
        return await super().fetchrow(query, *args)


@pytest.fixture
def cancel_flow(offline, monkeypatch, tmp_path):
    # `offline` replaces the ambient environment and blocks socket/asyncpg access
    # before importing the application. Never load the checkout's real .env.
    from openvegas.payments import service as billing_module

    root = Path(billing_module.__file__).resolve().parents[2]
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="ascii")
    monkeypatch.setenv("OPENVEGAS_ENV_FILE", str(empty_env))
    monkeypatch.setenv("OPENVEGAS_ROOT", str(root))
    monkeypatch.setenv("OPENVEGAS_DOTENV_OVERRIDE", "0")
    monkeypatch.setenv("V_PER_USD", "100")
    monkeypatch.setenv("TOPUP_CHECKOUT_EXPIRY_SEC", "3600")
    monkeypatch.setenv("CHECKOUT_SUCCESS_URL", "http://example.test/ui?checkout=success")

    def blocked(*args, **kwargs):
        raise AssertionError("Cancel-return regression forbids external I/O and startup")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    from server import main
    from server.routes import payments

    monkeypatch.setattr(main, "init_runtime_deps", blocked)
    db = CancelReturnDB()
    wallet = WalletService(db)
    gateway = offline.billing.stripe_gateway
    billing = BillingService(db, wallet, gateway)
    calls = []

    def create_session(**params):
        calls.append(deepcopy(params))
        session_id = "cs_test_cancel_" + params["client_reference_id"]
        return {"id": session_id, "url": "https://checkout.example.test/" + session_id}

    # There is no real Stripe API client behind this fixture method.
    gateway.stripe.checkout = SimpleNamespace(Session=SimpleNamespace(create=create_session))
    monkeypatch.setattr(payments, "get_billing_service", lambda: billing)
    monkeypatch.setattr(
        main.app,
        "dependency_overrides",
        {
            **main.app.dependency_overrides,
            payments.get_current_user: lambda: {"user_id": "alice"},
        },
    )
    clock = [datetime.now(UTC)]
    # _is_expired is a classmethod; keep creation and polling on the same clock.
    monkeypatch.setattr(BillingService, "_utc_now", staticmethod(lambda: clock[0]))
    return SimpleNamespace(
        app=main.app,
        db=db,
        wallet=wallet,
        billing=billing,
        store=StoreService(db, wallet),
        calls=calls,
        clock=clock,
        root=root,
    )


def commerce_state(flow):
    return deepcopy(
        {key: value for key, value in flow.db.state.items() if key not in {"topups", "webhooks"}}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "return_path",
    ["/ui?checkout=cancel", "/ui/checkout-cancel", None],
    ids=["configured-query-return", "dedicated-cancel-page", "abandoned"],
)
async def test_cancel_return_or_abandonment_preserves_commerce_and_allows_safe_retry(
    cancel_flow, approved, monkeypatch, return_path
):
    flow = cancel_flow
    # Bind the default to documented application config, without reading .env.
    example = (flow.root / ".env.example").read_text(encoding="utf-8")
    assert "CHECKOUT_CANCEL_URL=http://127.0.0.1:8085/ui?checkout=cancel" in example
    cancel_path = return_path or "/ui?checkout=cancel"
    monkeypatch.setenv("CHECKOUT_CANCEL_URL", "http://example.test" + cancel_path)
    flow.db.state["balances"].update({"user:alice": Decimal(1), "user:bob": Decimal(7)})
    # Preserve nonempty, preexisting orders and ownership as well as balances.
    await flow.store.buy("bob", "test_other", "preexisting-bob-order")
    before = commerce_state(flow)
    body = {"amount_usd": "10.00", "idempotency_key": "cancel-retry"}

    # ASGITransport does not start lifespan or fetch linked scripts/fonts/assets.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=flow.app), base_url="http://example.test"
    ) as client:
        response = await client.post("/billing/topups/checkout", json=body)
        assert response.status_code == 200
        original = response.json()
        topup_id = original["topup_id"]
        status_url = "/billing/topups/" + topup_id
        assert original["status"] == "checkout_created"
        assert len(flow.calls) == 1
        assert flow.calls[0]["cancel_url"] == os.environ["CHECKOUT_CANCEL_URL"]
        assert flow.calls[0]["idempotency_key"] == "topup-checkout:" + topup_id
        pending = deepcopy(flow.db.state)

        for _ in range(2):
            if return_path is not None:
                returned = await client.get(flow.calls[0]["cancel_url"])
                assert returned.status_code == 200
                assert returned.headers["content-type"].startswith("text/html")
                page = (
                    "checkout-cancel.html"
                    if return_path.endswith("checkout-cancel")
                    else "index.html"
                )
                assert returned.content == (flow.root / "ui" / page).read_bytes()
                if page == "checkout-cancel.html":
                    assert 'href="/ui/topup-checkout"' in returned.text
                    assert (await client.get("/ui/topup-checkout")).status_code == 200
            status = await client.get(status_url)
            assert status.status_code == 200
            assert status.json()["status"] == "checkout_created"
            assert flow.db.state == pending
            assert commerce_state(flow) == before
            assert flow.db.state["webhooks"] == {}  # No invented cancellation event.

        for _ in range(2):
            replay = await client.post("/billing/topups/checkout", json=body)
            assert replay.status_code == 200
            assert replay.json() == original
            assert flow.db.state == pending
        assert len(flow.calls) == 1
        conflict = await client.post("/billing/topups/checkout", json={**body, "amount_usd": "20"})
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "IDEMPOTENCY_PAYLOAD_CONFLICT"
        assert flow.db.state == pending

        # Returning/canceling cannot fund a cosmetic purchase, including its replay.
        for _ in range(2):
            with pytest.raises(InsufficientBalance):
                await flow.store.buy("alice", "test_emote", "buy-after-return")
            assert flow.db.state == pending

        # The UI omits an idempotency key on a new click; the route creates a new
        # attempt. Neither the new session nor the old abandoned one is payment.
        fresh = await client.post("/billing/topups/checkout", json={"amount_usd": "10.00"})
        assert fresh.status_code == 200
        assert fresh.json()["topup_id"] != topup_id
        assert fresh.json()["checkout_session_id"] != original["checkout_session_id"]
        assert fresh.json()["status"] == "checkout_created"
        assert len(flow.calls) == len(flow.db.state["topups"]) == 2
        assert commerce_state(flow) == before
        assert flow.db.state["webhooks"] == {}
        assert flow.db.state["topups"][topup_id] == pending["topups"][topup_id]

        # Browser return is not irreversible payment cancellation: an authentic
        # (locally signed TEST fixture) paid completion can still settle once.
        event = _checkout_event(
            ("alice", topup_id, original["checkout_session_id"], "cus_local_store")
        )
        assert event["livemode"] is False
        assert (await flow.billing.handle_webhook(**_signed(event)))["status"] == "paid"
        paid_state = deepcopy(flow.db.state)
        assert (await flow.billing.handle_webhook(**_signed(event)))["status"] == "duplicate"
        assert flow.db.state == paid_state
        redelivery = deepcopy(event)
        redelivery["id"] += "_redelivery"
        assert (await flow.billing.handle_webhook(**_signed(redelivery)))["idempotent"] is True
        assert commerce_state(flow) == {
            k: v for k, v in paid_state.items() if k not in {"topups", "webhooks"}
        }
        assert await flow.wallet.get_balance("user:alice") == Decimal(1001)
        assert sum(r["entry_type"] == "fiat_topup" for r in flow.db.state["ledger"].values()) == 1
        assert flow.db.state["orders"] == before["orders"]
        assert flow.db.state["entitlements"] == before["entitlements"]
        assert flow.db.state["topups"][fresh.json()["topup_id"]]["status"] == "checkout_created"
        paid_retry = await client.post("/billing/topups/checkout", json=body)
        assert paid_retry.status_code == 200
        assert paid_retry.json()["status"] == "paid"
        assert paid_retry.json()["topup_id"] == topup_id
        assert len(flow.calls) == 2
        if return_path is not None:
            settled = deepcopy(flow.db.state)
            assert (await client.get(flow.calls[0]["cancel_url"])).status_code == 200
            assert flow.db.state == settled  # A stale cancel URL cannot reverse payment.

        bought = await flow.store.buy("alice", "test_emote", "buy-after-return")
        fulfilled = deepcopy(flow.db.state)
        replayed = await flow.store.buy("alice", "test_emote", "buy-after-return")
        assert replayed.order_id == bought.order_id
        assert flow.db.state == fulfilled
        assert await flow.wallet.get_balance("user:alice") == Decimal("998.5")
        assert len(flow.db.state["orders"]) == len(before["orders"]) + 1
        assert len(flow.db.state["entitlements"]) == len(before["entitlements"]) + 1


@pytest.mark.asyncio
async def test_abandoned_checkout_expires_only_when_billing_ttl_elapses(cancel_flow, monkeypatch):
    flow = cancel_flow
    monkeypatch.setenv("CHECKOUT_CANCEL_URL", "http://example.test/ui?checkout=cancel")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=flow.app), base_url="http://example.test"
    ) as client:
        response = await client.post(
            "/billing/topups/checkout",
            json={
                "amount_usd": "10",
                "idempotency_key": "abandoned-expiry",
            },
        )
        assert response.status_code == 200
        topup_id = response.json()["topup_id"]
        url = "/billing/topups/" + topup_id
        before = commerce_state(flow)
        flow.clock[0] += timedelta(seconds=3599)
        assert (await client.get(url)).json()["status"] == "checkout_created"
        flow.clock[0] += timedelta(seconds=1)
        for _ in range(2):
            polled = await client.get(url)
            assert polled.status_code == 200
            assert polled.json()["status"] == "expired"
            assert commerce_state(flow) == before
            assert flow.db.state["webhooks"] == {}
        assert len(flow.calls) == len(flow.db.state["topups"]) == 1
