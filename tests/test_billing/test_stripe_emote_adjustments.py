"""Pure event/state policy and real local signature verification; no network/DB."""

from dataclasses import asdict, replace

import httpx
import pytest
from fastapi import FastAPI

from openvegas.payments.adjustments import AdjustmentError, Fact, parse_event, policy, transition
from openvegas.payments.service import BillingService, WebhookVerificationError
from server.routes import payments
from tests.integration.test_restoration_db import _signed

FACT = Fact("dispute", "du_test", "pi_test", "ch_test", 1000, "usd", "needs_response", 100, False)


def row(fact=FACT, **changes):
    return {**asdict(fact), "needs_review": False, **changes}


def test_terminal_won_beats_late_open_and_conflicting_terminal_is_reviewed():
    assert transition(row(state="won"), replace(FACT, event_created=101)) == ("stale", False)
    assert transition(row(state="won"), replace(FACT, state="lost")) == ("conflict", True)
    assert transition(row(event_created=200), replace(FACT, state="won")) == ("apply", False)


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "canceled"])
def test_refund_terminal_not_erased_by_newer_pending(terminal):
    fact = replace(FACT, kind="refund", state="pending", object_id="re_test")
    assert transition(row(fact, state=terminal), replace(fact, event_created=1000)) == (
        "stale",
        False,
    )


@pytest.mark.parametrize(
    "field,value", [("charge_id", "ch_other"), ("currency", "eur"), ("livemode", True)]
)
def test_identity_is_immutable(field, value):
    with pytest.raises(AdjustmentError, match="IDENTITY"):
        transition(row(), replace(FACT, **{field: value}))


def test_overlapping_refund_evidence_is_not_counted_twice():
    refund = row(kind="refund", state="succeeded", amount_minor=300)
    aggregate = row(kind="charge_refund", amount_minor=300)
    assert policy([refund, aggregate], total_minor=1000, exclusive=False) == (
        "review",
        "partial_refund_allocation_unknown",
    )
    assert policy([refund, aggregate], total_minor=1000, exclusive=True)[0] == "revoked"
    assert (
        policy([row(kind="charge_refund", amount_minor=1000)], total_minor=1000, exclusive=False)[0]
        == "revoked"
    )


def test_disputes_combine_without_summing_overlapping_losses():
    assert policy([row(state="won"), row()], total_minor=1000, exclusive=True)[0] == "suspended"
    assert (
        policy([row(state="won"), row(state="won")], total_minor=1000, exclusive=True)[0]
        == "active"
    )
    assert (
        policy([row(state="lost", amount_minor=500)] * 2, total_minor=1000, exclusive=False)[0]
        == "suspended"
    )
    assert (
        policy([row(state="won"), row(state="lost")], total_minor=1000, exclusive=False)[0]
        == "revoked"
    )


def event():
    return {
        "id": "evt_fixture",
        "object": "event",
        "type": "charge.dispute.created",
        "created": 100,
        "livemode": False,
        "data": {
            "object": {
                "object": "dispute",
                "id": "du_test",
                "payment_intent": "pi_test",
                "charge": "ch_test",
                "amount": 1000,
                "currency": "usd",
                "status": "needs_response",
                "livemode": False,
                "reason": "product_not_received",
                "evidence": {},
                "metadata": {},
            }
        },
    }


def test_parse_exact_provider_facts():
    assert parse_event(event(), expected_livemode=False) == FACT


@pytest.mark.parametrize(
    "event_type,state",
    [
        ("charge.dispute.created", "needs_response"),
        ("charge.dispute.updated", "under_review"),
        ("charge.dispute.closed", "won"),
        ("charge.dispute.closed", "lost"),
    ],
)
def test_documented_du_dispute_shape_parses_all_lifecycle_events(event_type, state):
    body = event()
    body["type"] = event_type
    body["data"]["object"].update(id="du_1MtJUT2eZvKYlo2CNaw2HvEv", status=state)
    assert parse_event(body, expected_livemode=False) == replace(
        FACT, object_id="du_1MtJUT2eZvKYlo2CNaw2HvEv", state=state
    )


@pytest.mark.parametrize(
    "object_id",
    [
        "dp_test", "dp_1MtJUT2eZvKYlo2CNaw2HvEv", "re_test", "ch_test", "evt_test",
        "DU_test", "du_", "du_bad\n", "du_bad/path", "du_" + "a" * 253,
        "du_\u00e9", "", None, True,
    ],
)
def test_dispute_rejects_bogus_prefix_and_malformed_id(object_id):
    body = event()
    body["data"]["object"]["id"] = object_id
    with pytest.raises(AdjustmentError, match="^ADJUSTMENT_INVALID_REFERENCE$"):
        parse_event(body, expected_livemode=False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", True),
        ("amount", 0),
        ("amount", -1),
        ("amount", 1.5),
        ("amount", "1000"),
        ("currency", "eur"),
        ("status", "unknown"),
        ("charge", None),
        ("payment_intent", {"id": "pi_test"}),
        ("id", "du_bad\n"),
        ("livemode", 0),
        ("object", "refund"),
    ],
)
def test_malformed_provider_objects_fail_closed(field, value):
    body = event()
    body["data"]["object"][field] = value
    with pytest.raises(AdjustmentError):
        parse_event(body, expected_livemode=False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("livemode", True),
        ("livemode", None),
        ("created", True),
        ("created", 0),
        ("account", "acct_another"),
        ("context", "acct_another"),
    ],
)
def test_mode_and_account_preflight(field, value):
    body = event()
    body[field] = value
    with pytest.raises(AdjustmentError):
        parse_event(body, expected_livemode=False)


@pytest.mark.asyncio
async def test_signed_route_invalid_signature_never_reaches_database(monkeypatch):
    from openvegas.payments.stripe_gateway import StripeGateway

    class NoDB:
        def transaction(self):
            pytest.fail("Invalid signature reached database")

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_signature_only")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_signature_only")
    service = BillingService(NoDB(), None, StripeGateway())
    payload = _signed(event())
    with pytest.raises(WebhookVerificationError):
        await service.handle_webhook(
            raw_body=payload["raw_body"] + b" ", signature=payload["signature"]
        )
    app = FastAPI()
    app.include_router(payments.router)
    monkeypatch.setattr(payments, "get_billing_service", lambda: service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/billing/webhook/stripe",
            content=payload["raw_body"],
            headers={"stripe-signature": "bad"},
        )
    assert result.status_code == 400
    assert result.json() == {"detail": "Unable to verify Stripe webhook"}
