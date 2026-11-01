"""TV-only Stripe checkout and verified-event consumer; never funds a wallet."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import BoundedSemaphore
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from openvegas.tv.access import MONTHLY_CENTS
from server.services.tv_billing import (
    PAID_PROGRAMS,
    Consent,
    TVError,
    active_pass,
    require_billing,
    user_lock,
)

PURPOSE = "tv_subscription"
_provider_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tv-payments")
_provider_slots = BoundedSemaphore(2)


async def _provider_call(function, *args, **kwargs):
    # A timed-out HTTP handler cannot release a still-running provider worker.
    slots = _provider_slots
    if not slots.acquire(blocking=False):
        raise TVError(503, "TV payment service is busy. Retry the same request later.")
    try:
        future = _provider_executor.submit(function, *args, **kwargs)
    except BaseException:
        slots.release()
        raise
    future.add_done_callback(lambda _: slots.release())
    return await asyncio.wrap_future(future)


def checkout_enabled():
    return os.getenv("OPENVEGAS_TV_CHECKOUT_ENABLED", "0") == "1"


def stripe_url(value, host):
    parsed = urlsplit(value or "")
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise TVError(503, "TV payment redirect is unavailable.")
    return value


class TVPayments:
    def __init__(self, billing, *, programs=None):
        self.billing = billing
        self.db = billing.db
        self.gateway = billing.stripe_gateway
        self.programs = PAID_PROGRAMS if programs is None else programs

    async def checkout(self, user_id, consent: Consent):
        require_billing()
        Consent.model_validate(consent.model_dump())
        if not checkout_enabled() or not self.programs:
            raise TVError(
                503, "TV sales are closed pending final programs and billing integration."
            )
        if self.billing.provider_mode != "stripe":
            raise TVError(
                503, "TV requires verified Stripe payments; simulated payments cannot grant access."
            )
        price_id = os.getenv("STRIPE_TV_MONTHLY_PRICE_ID", "")
        base = os.getenv("APP_BASE_URL", "").rstrip("/")
        parsed = urlsplit(base)
        if (
            not price_id.startswith("price_")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path
            or not parsed.hostname
            or (
                parsed.scheme != "https"
                and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})
            )
        ):
            raise TVError(503, "TV checkout is not configured.")
        user = UUID(str(user_id))
        async with self.db.transaction() as tx:
            await user_lock(tx, user)
            if await active_pass(tx, user, datetime.now(UTC)):
                raise TVError(
                    409, "A paid TV pass is already active. Manage that subscription instead."
                )
            ongoing = await tx.fetchrow(
                "SELECT stripe_subscription_id FROM tv_subscriptions WHERE user_id=$1 AND status <> 'canceled' LIMIT 1",
                user,
            )
            if ongoing:
                raise TVError(
                    409,
                    "An existing TV subscription needs review. Manage it instead of buying another.",
                )
            row = await tx.fetchrow(
                "SELECT * FROM tv_checkouts WHERE user_id=$1 AND idempotency_key=$2",
                user,
                consent.idempotency_key,
            )
            if row and row["state"] != "pending":
                raise TVError(
                    409, "This checkout has ended. Refresh TV status before another purchase."
                )
            if not row:
                pending = await tx.fetchrow(
                    "SELECT id FROM tv_checkouts WHERE user_id=$1 AND state='pending'", user
                )
                if pending:
                    raise TVError(
                        409,
                        "A TV checkout is pending. Retry its original request; do not pay twice.",
                    )
                # This reuses existing customer ownership, not a client-supplied card ID.
                customer = await self.billing._find_user_customer(str(user))
                if not customer:
                    raise TVError(
                        409,
                        "No saved billing customer. Configure billing on the existing wallet page first.",
                    )
                row = await tx.fetchrow(
                    "INSERT INTO tv_checkouts(id,user_id,idempotency_key,stripe_customer_id,stripe_price_id) VALUES ($1,$2,$3,$4,$5) RETURNING *",
                    uuid4(),
                    user,
                    consent.idempotency_key,
                    customer,
                    price_id,
                )
        if row["checkout_url"]:
            return {
                "url": stripe_url(row["checkout_url"], "checkout.stripe.com"),
                "access_granted": False,
            }
        # Never recreate an unknown-outcome session after Stripe's idempotency window.
        if (datetime.now(UTC) - row["created_at"]).total_seconds() >= 23 * 3600:
            raise TVError(409, "TV checkout needs reconciliation. Do not submit another payment.")
        # No inline price/product creation, off-session confirmation or trial grant.
        price = await _provider_call(self.gateway.stripe.Price.retrieve, row["stripe_price_id"])
        if (
            price.get("active") is not True
            or price.get("currency") != "usd"
            or price.get("unit_amount") != MONTHLY_CENTS
            or price.get("recurring", {}).get("interval") != "month"
            or price.get("recurring", {}).get("interval_count") != 1
        ):
            raise TVError(503, "TV price must be the approved US$20 monthly price.")
        metadata = {"purpose": PURPOSE, "user_id": str(user), "tv_checkout_id": str(row["id"])}
        session = await _provider_call(
            self.gateway._session_create,
            idempotency_key="tv-checkout:" + str(row["id"]),
            mode="subscription",
            customer=row["stripe_customer_id"],
            client_reference_id=str(user),
            line_items=[{"price": row["stripe_price_id"], "quantity": 1}],
            metadata=metadata,
            subscription_data={"metadata": metadata},
            success_url=base + "/ui/tv?checkout=returned",
            cancel_url=base + "/ui/tv?checkout=canceled",
        )
        url = stripe_url(session.get("url"), "checkout.stripe.com")
        if not str(session.get("id", "")).startswith("cs_"):
            raise TVError(503, "TV checkout could not be verified.")
        await self.db.execute(
            "UPDATE tv_checkouts SET stripe_session_id=$2,checkout_url=$3 WHERE id=$1",
            row["id"],
            session["id"],
            url,
        )
        return {"url": url, "access_granted": False}

    async def cancel_portal(self, user_id, consent: Consent):
        Consent.model_validate(consent.model_dump())
        if self.billing.provider_mode != "stripe":
            raise TVError(503, "TV subscription management is unavailable.")
        row = await self.db.fetchrow(
            "SELECT stripe_customer_id,stripe_subscription_id FROM tv_subscriptions WHERE user_id=$1 "
            "AND status <> 'canceled' ORDER BY last_event_created DESC LIMIT 1",
            UUID(str(user_id)),
        )
        if not row:
            raise TVError(404, "No TV subscription is available to manage.")
        url = await _provider_call(
            self.gateway.create_billing_portal,
            customer_id=row["stripe_customer_id"],
            subscription_id=row["stripe_subscription_id"],
            flow_type="subscription_cancel",
        )
        return {"url": stripe_url(url, "billing.stripe.com")}


def _id(value):
    return value.get("id") if isinstance(value, dict) else value


async def _invoice_lock(tx, invoice_id):
    if not isinstance(invoice_id, str) or not invoice_id.startswith("in_") or len(invoice_id) > 128:
        raise TVError(422, "Invalid verified TV invoice identity.")
    await tx.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "tv-invoice:" + invoice_id
    )


async def handle_verified_event(tx, event, *, expected_livemode: bool):
    """Parent calls AFTER signature verification and event dedupe, in the SAME tx.

    Return None only for non-TV events. TV events must never reach wallet funding.
    No callback from a browser or checkout success URL is payment verification.
    """
    kind = event["type"]
    obj = event["data"]["object"]
    details = (
        (obj.get("parent") or {}).get("subscription_details")
        or obj.get("subscription_details")
        or {}
    )
    metadata = {**(obj.get("metadata") or {}), **(details.get("metadata") or {})}
    # Adding the shared hook must not require migrating an otherwise closed TV product.
    if not await tx.fetchval("SELECT to_regclass('public.tv_subscriptions')"):
        if metadata.get("purpose") == PURPOSE:
            raise TVError(503, "TV payment storage is not ready; retry the verified event later.")
        return None
    subscription_id = _id(obj.get("subscription") or details.get("subscription"))
    if kind.startswith("customer.subscription."):
        subscription_id = obj["id"]
    subscription = (
        await tx.fetchrow(
            "SELECT * FROM tv_subscriptions WHERE stripe_subscription_id=$1", subscription_id
        )
        if subscription_id
        else None
    )
    if metadata.get("purpose") != PURPOSE and not subscription:
        return None
    if (
        type(expected_livemode) is not bool
        or event.get("livemode") is not expected_livemode
        or obj.get("livemode") is not expected_livemode
    ):
        raise TVError(409, "TV Stripe mode mismatch.")
    checkout = None
    if subscription:
        checkout = await tx.fetchrow(
            "SELECT * FROM tv_checkouts WHERE id=$1", subscription["checkout_id"]
        )
    elif metadata.get("tv_checkout_id"):
        checkout = await tx.fetchrow(
            "SELECT * FROM tv_checkouts WHERE id=$1", UUID(metadata["tv_checkout_id"])
        )
    if not checkout or _id(obj.get("customer")) != checkout["stripe_customer_id"]:
        raise TVError(409, "TV payment does not match a confirmed checkout.")
    if metadata.get("user_id") and metadata["user_id"] != str(checkout["user_id"]):
        raise TVError(409, "TV payment owner mismatch.")
    # All invoice consumers lock invoice before owner, including revocations.
    if kind == "invoice.paid":
        await _invoice_lock(tx, obj["id"])
    await user_lock(tx, checkout["user_id"])
    if kind.startswith("checkout.session.") and obj.get("id") != checkout["stripe_session_id"]:
        raise TVError(409, "TV checkout session identity mismatch.")
    if kind == "checkout.session.expired":
        await tx.execute(
            "UPDATE tv_checkouts SET state='expired' WHERE id=$1 AND state='pending'",
            checkout["id"],
        )
        return {"status": "tv-checkout-expired"}
    if not subscription_id or not str(subscription_id).startswith("sub_"):
        raise TVError(409, "TV subscription identity is missing.")
    await tx.execute(
        "INSERT INTO tv_subscriptions(stripe_subscription_id,user_id,checkout_id,stripe_customer_id,stripe_price_id) "
        "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (stripe_subscription_id) DO NOTHING",
        subscription_id,
        checkout["user_id"],
        checkout["id"],
        checkout["stripe_customer_id"],
        checkout["stripe_price_id"],
    )
    subscription = await tx.fetchrow(
        "SELECT * FROM tv_subscriptions WHERE stripe_subscription_id=$1 FOR UPDATE", subscription_id
    )
    if not subscription or subscription["checkout_id"] != checkout["id"]:
        raise TVError(409, "TV subscription binding mismatch.")
    if kind in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        if obj.get("mode") != "subscription":
            raise TVError(409, "TV checkout mode mismatch.")
        await tx.execute("UPDATE tv_checkouts SET state='completed' WHERE id=$1", checkout["id"])
        return {"status": "tv-checkout-recorded-no-access"}
    created = event["created"]
    if type(created) is not int or created < 0:
        raise TVError(409, "Invalid TV event timestamp.")
    if kind == "invoice.paid":
        lines = obj.get("lines", {})
        entries = lines.get("data", [])
        if (
            obj.get("status") != "paid"
            or obj.get("paid") is not True
            or obj.get("paid_out_of_band") is True
            or obj.get("currency") != "usd"
            or obj.get("amount_paid") != MONTHLY_CENTS
            or obj.get("amount_due") != MONTHLY_CENTS
            or lines.get("has_more") is not False
            or len(entries) != 1
        ):
            raise TVError(409, "TV access requires a fully paid US$20 invoice.")
        line = entries[0]
        price_id = _id(line.get("price")) or line.get("pricing", {}).get("price_details", {}).get(
            "price"
        )
        if (
            price_id != checkout["stripe_price_id"]
            or line.get("quantity") != 1
            or line.get("amount") != MONTHLY_CENTS
        ):
            raise TVError(409, "TV invoice price or quantity mismatch.")
        period = line.get("period", {})
        start, end = period.get("start"), period.get("end")
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 < end - start <= 32 * 86400
            or line.get("proration") is True
            or line.get("parent", {}).get("subscription_item_details", {}).get("proration") is True
        ):
            raise TVError(409, "TV invoice term is invalid.")
        begin, finish = datetime.fromtimestamp(start, UTC), datetime.fromtimestamp(end, UTC)
        revoked = await tx.fetchval(
            "SELECT EXISTS(SELECT 1 FROM tv_invoice_revocations WHERE invoice_id=$1)", obj["id"]
        )
        if revoked:
            await tx.execute(
                "UPDATE tv_subscriptions SET status='blocked' WHERE stripe_subscription_id=$1",
                subscription_id,
            )
            return {"status": "tv-revoked-no-access"}
        old = await tx.fetchrow("SELECT * FROM tv_paid_terms WHERE invoice_id=$1", obj["id"])
        if old:
            if (
                old["stripe_subscription_id"] != subscription_id
                or old["period_start"] != begin
                or old["period_end"] != finish
            ):
                raise TVError(409, "TV invoice replay conflicts with its paid term.")
            return {"status": "tv-invoice-replayed"}
        if subscription["status"] in {"canceled", "blocked"}:
            return {"status": "tv-revoked-no-access"}
        await tx.execute(
            "INSERT INTO tv_paid_terms(invoice_id,stripe_subscription_id,period_start,period_end,amount_cents,charge_id,payment_intent_id) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            obj["id"],
            subscription_id,
            begin,
            finish,
            MONTHLY_CENTS,
            _id(obj.get("charge")),
            _id(obj.get("payment_intent")),
        )
        # Record even stale paid proof for replay protection, but only a strictly
        # newer payment can lift a transient restriction. Restriction wins ties.
        if (
            subscription["status"] not in {"unpaid", "paused", "past_due"}
            or created > subscription["last_event_created"]
        ):
            await tx.execute(
                "UPDATE tv_subscriptions SET status='active',last_event_created=GREATEST(last_event_created,$2) WHERE stripe_subscription_id=$1",
                subscription_id,
                created,
            )
        await tx.execute("UPDATE tv_checkouts SET state='completed' WHERE id=$1", checkout["id"])
        return {"status": "tv-paid-term-recorded", "wallet_credit_v": "0"}
    if kind in {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_failed",
    }:
        status = (
            "canceled"
            if kind.endswith(".deleted") or obj.get("status") == "incomplete_expired"
            else "past_due"
            if kind == "invoice.payment_failed"
            else obj.get("status")
        )
        # Metadata and financial status have independent clocks. An active
        # snapshot must never hide an earlier-created payment restriction.
        if (
            kind.startswith("customer.subscription.")
            and type(obj.get("cancel_at_period_end")) is bool
            and created > subscription["last_metadata_event_created"]
        ):
            await tx.execute(
                "UPDATE tv_subscriptions SET cancel_at_period_end=$2,last_metadata_event_created=$3 WHERE stripe_subscription_id=$1",
                subscription_id,
                obj["cancel_at_period_end"],
                created,
            )
        if status not in {
            "canceled",
            "past_due",
            "unpaid",
            "paused",
        }:
            return {"status": "tv-subscription-recorded-no-new-access"}
        # past_due still allows already-paid terms; a failure cannot undo a
        # harder unpaid/paused restriction without a new verified payment.
        if subscription["status"] in {"unpaid", "paused"} and status == "past_due":
            status = subscription["status"]
        # Terminal cancellation wins in every delivery order. Other restrictions
        # win same-second payment ties; no late event may move the clock backward.
        if subscription["status"] not in {"blocked", "canceled"} and (
            status == "canceled" or created >= subscription["last_event_created"]
        ):
            await tx.execute(
                "UPDATE tv_subscriptions SET status=$2,last_event_created=GREATEST(last_event_created,$3) WHERE stripe_subscription_id=$1",
                subscription_id,
                status,
                created,
            )
        return {"status": "tv-subscription-recorded-no-new-access"}
    return {"status": "tv-event-ignored"}


async def revoke_paid_invoice(tx, invoice_id, *, reason):
    """Only for a verified TV invoice; parent must consume even before its paid event.

    Persist a tombstone first so an out-of-order invoice.paid cannot restore access.
    Never use client-supplied invoice IDs or call this for wallet-funding invoices.
    """
    if reason not in {"refund", "dispute"}:
        raise TVError(422, "Invalid TV revocation reason.")
    await _invoice_lock(tx, invoice_id)
    await tx.execute(
        "INSERT INTO tv_invoice_revocations(invoice_id,reason) VALUES ($1,$2) ON CONFLICT (invoice_id) DO NOTHING",
        invoice_id,
        reason,
    )
    term = await tx.fetchrow("SELECT * FROM tv_paid_terms WHERE invoice_id=$1", invoice_id)
    if not term:
        return True
    owner = await tx.fetchrow(
        "SELECT * FROM tv_subscriptions WHERE stripe_subscription_id=$1",
        term["stripe_subscription_id"],
    )
    await user_lock(tx, owner["user_id"])
    await tx.execute(
        "UPDATE tv_paid_terms SET revoked=true,revoke_reason=$2 WHERE invoice_id=$1",
        invoice_id,
        reason,
    )
    await tx.execute(
        "UPDATE tv_subscriptions SET status='blocked' WHERE stripe_subscription_id=$1",
        term["stripe_subscription_id"],
    )
    return True
