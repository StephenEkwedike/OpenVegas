"""Verified TV dispatch before wallet funding, including read-only reversal lookup."""

from __future__ import annotations

import asyncio
from uuid import UUID

from openvegas.payments.adjustments import DISPUTE_OPEN, EVENTS, parse_event
from server.services.tv_billing import TVError
from server.services.tv_payments import (
    PURPOSE,
    _id,
    _provider_call,
    handle_verified_event,
    revoke_paid_invoice,
)


def _metadata(obj):
    details = (
        (obj.get("parent") or {}).get("subscription_details")
        or obj.get("subscription_details")
        or {}
    )
    return {**(obj.get("metadata") or {}), **(details.get("metadata") or {})}, details


def _mode(gateway):
    key = str(getattr(getattr(gateway, "stripe", None), "api_key", ""))
    if getattr(gateway, "mode", None) != "stripe":
        return None
    if key.startswith(("sk_test_", "rk_test_")):
        return False
    if key.startswith(("sk_live_", "rk_live_")):
        return True
    return None


def _verified(event, mode, signature_verified):
    if (
        signature_verified is not True
        or type(mode) is not bool
        or event.get("livemode") is not mode
        or event.get("account")
        or event.get("context")
    ):
        raise TVError(503, "TV payment verification or account mode is unavailable.")


def _reference(value, prefix):
    value = _id(value)
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or not len(prefix) < len(value) <= 128
        or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in value)
    ):
        raise TVError(503, "TV payment identity could not be resolved; retry later.")
    return value


def _object(value, object_type, object_id, mode):
    if (
        not isinstance(value, dict)
        or value.get("object") != object_type
        or value.get("id") != object_id
        or value.get("livemode") is not mode
        or value.get("on_behalf_of")
        or value.get("application")
    ):
        raise TVError(503, "TV provider identity or account mode does not match.")
    return value


async def _read(gateway, resource, operation, *args, **kwargs):
    method = getattr(getattr(gateway.stripe, resource, None), operation, None)
    if not callable(method):
        raise TVError(503, "TV provider lookup is unavailable; retry after reconciliation.")
    return await _provider_call(method, *args, **kwargs)


async def _remote_invoice(tx, fact, gateway, mode):
    """At most four provider reads, no pagination, mutations or inferred FIFO links."""
    charge = _object(
        await _read(gateway, "Charge", "retrieve", fact.charge_id), "charge", fact.charge_id, mode
    )
    if (
        _id(charge.get("payment_intent")) != fact.payment_intent
        or charge.get("paid") is not True
        or charge.get("currency") != "usd"
        or type(charge.get("amount")) is not int
        or charge["amount"] < fact.amount_minor
    ):
        raise TVError(503, "TV reversal charge does not match the verified event.")
    customer = _reference(charge.get("customer"), "cus_")
    invoice_id = _id(charge.get("invoice"))
    if not invoice_id:
        # Current Stripe invoices link through InvoicePayment, not Charge.invoice.
        payments = await _read(
            gateway,
            "InvoicePayment",
            "list",
            limit=2,
            payment={"type": "payment_intent", "payment_intent": fact.payment_intent},
        )
        entries = payments.get("data", [])
        if payments.get("has_more") is not False or len(entries) != 1:
            raise TVError(503, "TV invoice mapping is absent or ambiguous; retry later.")
        payment = entries[0]
        if (
            payment.get("object") != "invoice_payment"
            or payment.get("livemode") is not mode
            or payment.get("status") != "paid"
            or payment.get("currency") != "usd"
            or payment.get("amount_paid") != charge["amount"]
            or payment.get("payment", {}).get("type") != "payment_intent"
            or _id(payment["payment"].get("payment_intent")) != fact.payment_intent
        ):
            raise TVError(503, "TV invoice payment binding is unverified.")
        invoice_id = payment.get("invoice")
    invoice_id = _reference(invoice_id, "in_")
    invoice = _object(
        await _read(gateway, "Invoice", "retrieve", invoice_id), "invoice", invoice_id, mode
    )
    if (
        _id(invoice.get("customer")) != customer
        or invoice.get("currency") != "usd"
        or invoice.get("amount_paid") != charge["amount"]
    ):
        raise TVError(503, "TV invoice customer or payment amount does not match.")
    metadata, details = _metadata(invoice)
    subscription_id = _reference(invoice.get("subscription") or details.get("subscription"), "sub_")
    subscription = await tx.fetchrow(
        "SELECT * FROM tv_subscriptions WHERE stripe_subscription_id=$1", subscription_id
    )
    if not subscription and metadata.get("purpose") != PURPOSE:
        remote = _object(
            await _read(gateway, "Subscription", "retrieve", subscription_id),
            "subscription",
            subscription_id,
            mode,
        )
        if _id(remote.get("customer")) != customer:
            raise TVError(503, "TV subscription customer does not match.")
        metadata = remote.get("metadata") or {}
    if not subscription and metadata.get("purpose") != PURPOSE:
        return None
    try:
        checkout_id = (
            subscription["checkout_id"] if subscription else UUID(metadata["tv_checkout_id"])
        )
    except (KeyError, ValueError, TypeError):
        raise TVError(503, "TV checkout mapping is not ready; retry later.") from None
    checkout = await tx.fetchrow("SELECT * FROM tv_checkouts WHERE id=$1", checkout_id)
    if (
        not checkout
        or checkout["stripe_customer_id"] != customer
        or (metadata.get("user_id") and metadata["user_id"] != str(checkout["user_id"]))
    ):
        raise TVError(503, "TV checkout owner does not match the reversal.")
    lines = invoice.get("lines") or {}
    entries = lines.get("data", [])
    if (
        lines.get("has_more") is not False
        or len(entries) != 1
        or charge["amount"] != 2000
        or entries[0].get("amount") != 2000
        or entries[0].get("quantity") != 1
    ):
        raise TVError(503, "TV invoice allocation requires reconciliation.")
    price = _id(entries[0].get("price")) or (entries[0].get("pricing") or {}).get(
        "price_details", {}
    ).get("price")
    if price != checkout["stripe_price_id"]:
        raise TVError(503, "TV reversal price does not match the confirmed checkout.")
    return {
        "invoice_id": invoice_id,
        "amount_cents": 2000,
        "stripe_customer_id": customer,
        "charge_id": fact.charge_id,
        "payment_intent_id": fact.payment_intent,
    }


async def _adjustment(tx, event, gateway, mode):
    fact = parse_event(event, expected_livemode=mode)
    rows = await tx.fetch(
        "SELECT t.*,s.stripe_customer_id FROM tv_paid_terms t JOIN tv_subscriptions s "
        "ON s.stripe_subscription_id=t.stripe_subscription_id "
        "WHERE t.charge_id=$1 OR t.payment_intent_id=$2 LIMIT 2",
        fact.charge_id,
        fact.payment_intent,
    )
    topup = await tx.fetchrow(
        "SELECT id FROM fiat_topups WHERE stripe_payment_intent_id=$1", fact.payment_intent
    )
    metadata, _ = _metadata(event["data"]["object"])
    if len(rows) > 1 or (topup and (rows or metadata.get("purpose") == PURPOSE)):
        raise TVError(503, "TV and wallet payment ownership conflicts; reconciliation required.")
    if topup:
        return None
    if rows:
        invoice = rows[0]
        if (invoice["charge_id"] and invoice["charge_id"] != fact.charge_id) or (
            invoice["payment_intent_id"] and invoice["payment_intent_id"] != fact.payment_intent
        ):
            raise TVError(503, "TV reversal binding conflicts with stored payment evidence.")
    else:
        invoice = await _remote_invoice(tx, fact, gateway, mode)
    if not invoice:
        # Generic wallet adjustment also fails closed if its mapping is not ready.
        return None
    if fact.amount_minor > invoice["amount_cents"]:
        raise TVError(503, "TV reversal exceeds the paid invoice.")
    obj = event["data"]["object"]
    if fact.kind == "charge_refund" and (
        obj.get("amount") != invoice["amount_cents"]
        or obj.get("paid") is not True
        or _id(obj.get("customer")) != invoice["stripe_customer_id"]
    ):
        raise TVError(503, "TV refunded charge owner or amount does not match.")
    revoke = (fact.kind != "dispute" and fact.state == "succeeded") or (
        fact.kind == "dispute" and fact.state in DISPUTE_OPEN | {"lost"}
    )
    if revoke:
        await revoke_paid_invoice(
            tx, invoice["invoice_id"], reason="dispute" if fact.kind == "dispute" else "refund"
        )
    return {"status": "tv-revocation-recorded" if revoke else "tv-adjustment-no-new-access"}


async def dispatch_tv_event(tx, event, *, gateway, signature_verified):
    """Called only inside BillingService's journal transaction, before non-TV dispatch."""
    kind, obj = event["type"], event["data"]["object"]
    metadata, details = _metadata(obj)
    relevant = kind in EVENTS or kind.startswith(
        ("invoice.", "customer.subscription.", "checkout.session.")
    )
    if not relevant and metadata.get("purpose") != PURPOSE:
        return None
    schema = await tx.fetchrow("SELECT to_regclass('public.tv_subscriptions') AS ready")
    if not schema or not schema["ready"]:
        if metadata.get("purpose") == PURPOSE:
            raise TVError(503, "TV payment storage is unavailable; retry later.")
        return None
    mode = _mode(gateway)
    if kind in EVENTS:
        _verified(event, mode, signature_verified)
        async with asyncio.timeout(8):
            return await _adjustment(tx, event, gateway, mode)
    subscription_id = (
        obj.get("id")
        if kind.startswith("customer.subscription.")
        else _id(obj.get("subscription") or details.get("subscription"))
    )
    known = (
        await tx.fetchrow(
            "SELECT stripe_subscription_id FROM tv_subscriptions WHERE stripe_subscription_id=$1",
            subscription_id,
        )
        if subscription_id
        else None
    )
    if metadata.get("purpose") != PURPOSE and not known:
        return None
    _verified(event, mode, signature_verified)
    return await handle_verified_event(tx, event, expected_livemode=mode)
