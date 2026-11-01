"""Owner-authorized agent funding. Provider webhooks, never a browser return, credit wallets."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from openvegas.payments.service import BillingError, BillingService, IdempotencyConflict
from openvegas.wallet.ledger import LedgerEntry, WalletService

CONSENT_VERSION = "agent-auto-v1"


async def provider_read(call, **kwargs):
    return await asyncio.wait_for(asyncio.to_thread(call, **kwargs), timeout=20)


def decimal_amount(value, *, minimum=Decimal(10), maximum=Decimal(500), places=2):
    try:
        number = Decimal(str(value))
        quantum = Decimal(10) ** -places
        if (
            not number.is_finite()
            or not minimum <= number <= maximum
            or number != number.quantize(quantum)
        ):
            raise ValueError
        return number.quantize(quantum)
    except (InvalidOperation, ValueError):
        raise BillingError(
            f"Enter an amount between {minimum} and {maximum}, with at most {places} decimal places."
        ) from None


def live_mode(gateway):
    key = str(getattr(getattr(gateway, "stripe", None), "api_key", ""))
    if not key.startswith(("sk_test_", "rk_test_", "rkcs_test_", "sk_live_", "rk_live_")):
        raise BillingError("Stripe test or live mode must be configured")
    return key.startswith(("sk_live_", "rk_live_"))


def require_enabled(gateway):
    if os.getenv("OPENVEGAS_AGENT_FUNDING_ENABLED", "0") != "1":
        raise BillingError("Agent funding is not enabled on this server")
    mode = live_mode(gateway)
    if mode and (
        os.getenv("OPENVEGAS_AGENT_FUNDING_LIVE_APPROVED", "0") != "1"
        or not os.getenv("OPENVEGAS_AGENT_FUNDING_APPROVAL_REFERENCE", "").strip()
    ):
        raise BillingError(
            "Live agent funding is awaiting payment-provider eligibility and release approval"
        )
    return mode


def public_order(row):
    return {
        key: str(row[key]) if isinstance(row[key], (Decimal, UUID, datetime)) else row[key]
        for key in (
            "id",
            "agent_id",
            "amount_usd",
            "credit_v",
            "status",
            "source",
            "reversed_v",
            "debt_v",
            "livemode",
            "checkout_url",
            "created_at",
        )
    }


class AgentFunding:
    def __init__(self, db, wallet, gateway):
        self.db, self.wallet, self.gateway = db, wallet, gateway

    async def daily_limit(self, tx, agent_id, mode, amount):
        cap = decimal_amount(
            os.getenv("OPENVEGAS_AGENT_FUNDING_DAILY_USD", "500"),
            minimum=Decimal(10), maximum=Decimal(5000),
        )
        try:
            attempts = int(os.getenv("OPENVEGAS_AGENT_FUNDING_DAILY_ATTEMPTS", "10"))
            if not 1 <= attempts <= 100:
                raise ValueError
        except ValueError:
            raise BillingError("Agent daily funding limit is not configured safely") from None
        used = await tx.fetchrow(
            """SELECT COALESCE(sum(amount_usd),0) AS amount, count(*) AS attempts
            FROM agent_funding_orders WHERE agent_id=$1 AND livemode=$2
            AND created_at >= date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""",
            agent_id, mode,
        )
        # Failed, refunded and ambiguous attempts still consume the daily limit.
        if used["amount"] + amount > cap:
            return "daily_cap"
        if used["attempts"] >= attempts:
            return "daily_attempts"
        return None

    async def lock_funding(self, tx, agent_id):
        # Manual and automatic funding serialize on the same durable transaction lock.
        await tx.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            "agent-funding:" + str(agent_id),
        )

    async def authorize(self, conn, user_id, org_id, agent_id, *, active=True):
        row = await conn.fetchrow(
            """SELECT a.status,o.is_demo FROM agent_accounts a
            JOIN organizations o ON o.id=a.org_id JOIN org_members m ON m.org_id=o.id
            WHERE a.id=$1 AND a.org_id=$2 AND m.user_id=$3 AND m.status='active'
              AND m.role IN ('owner','admin') AND o.status='active'""",
            agent_id,
            org_id,
            user_id,
        )
        if not row:
            raise BillingError("Org owner/admin access required for this agent")
        if active and (row["status"] != "active" or row["is_demo"]):
            raise BillingError("Demo or suspended agents cannot receive card funding")
        return row

    async def checkout(
        self, *, user_id, org_id, agent_id, amount_usd, idempotency_key, save_card=False
    ):
        mode = require_enabled(self.gateway)
        amount = decimal_amount(amount_usd)
        await self.authorize(self.db, user_id, org_id, agent_id)
        if not 1 <= len(idempotency_key) <= 120:
            raise BillingError("An idempotency key of 1 to 120 characters is required")
        payload_hash = BillingService.canonical_payload_hash(
            {
                "org_id": org_id,
                "agent_id": agent_id,
                "amount": amount,
                "livemode": mode,
                "source": "checkout",
                "save_card": save_card,
            }
        )
        existing = await self.db.fetchrow(
            "SELECT * FROM agent_funding_orders WHERE user_id=$1 AND idempotency_key=$2",
            user_id,
            idempotency_key,
        )
        if existing:
            if existing["payload_hash"] != payload_hash:
                raise IdempotencyConflict("Idempotency key already used for a different purchase")
            if existing["status"] != "created":
                return public_order(existing)
            row = existing
        else:
            if await self.db.fetchval(
                "SELECT EXISTS(SELECT 1 FROM agent_funding_orders WHERE agent_id=$1 AND status IN ('unknown','pending','requires_action','review'))",
                agent_id,
            ):
                raise BillingError(
                    "An existing payment needs review before another purchase. Check funding history; no replacement charge was made."
                )
            customer = await self.customer(user_id, mode)
            credit = amount * decimal_amount(
                os.getenv("V_PER_USD", "100"),
                minimum=Decimal(".000001"),
                maximum=Decimal(1000000),
                places=6,
            )
            async with self.db.transaction() as tx:
                await self.lock_funding(tx, agent_id)
                await self.authorize(tx, user_id, org_id, agent_id)
                existing = await tx.fetchrow(
                    "SELECT * FROM agent_funding_orders WHERE user_id=$1 AND idempotency_key=$2",
                    user_id, idempotency_key,
                )
                if not existing:
                    if await self.daily_limit(tx, agent_id, mode, amount):
                        raise BillingError("Daily agent funding limit reached. Try again tomorrow (UTC).")
                    if await tx.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM agent_funding_orders WHERE agent_id=$1 AND status IN ('unknown','pending','requires_action','review'))",
                        agent_id,
                    ):
                        raise BillingError("An existing payment needs review before another purchase.")
                await tx.execute(
                    """INSERT INTO agent_funding_orders
                    (id,org_id,agent_id,user_id,amount_usd,credit_v,livemode,customer_id,source,idempotency_key,payload_hash)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,'checkout',$9,$10)
                    ON CONFLICT(user_id,idempotency_key) DO NOTHING""",
                    str(uuid4()),
                    org_id,
                    agent_id,
                    user_id,
                    amount,
                    credit,
                    mode,
                    customer,
                    idempotency_key,
                    payload_hash,
                )
                row = await tx.fetchrow(
                    "SELECT * FROM agent_funding_orders WHERE user_id=$1 AND idempotency_key=$2",
                    user_id,
                    idempotency_key,
                )
                if row["payload_hash"] != payload_hash:
                    raise IdempotencyConflict(
                        "Idempotency key already used for a different purchase"
                    )
        if (datetime.now(UTC) - row["created_at"]).total_seconds() > 72000:
            raise BillingError("Unfinished checkout needs reconciliation; it will not be recreated")
        origin = os.getenv("APP_BASE_URL", "").rstrip("/")
        parsed = urlsplit(origin)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"} and not mode
        if (
            (parsed.scheme != "https" and not (local and parsed.scheme == "http"))
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise BillingError("Checkout return origin is not configured safely")
        try:
            result = await provider_read(
                self.gateway.create_agent_checkout,
                customer_id=row["customer_id"],
                amount_usd=amount,
                order_id=str(row["id"]),
                return_url=origin + "/ui/agents",
                save_card=save_card,
            )
        except Exception:  # noqa: BLE001 - provider failures are ambiguous; never retry a charge.
            await self.db.execute(
                "UPDATE agent_funding_orders SET status='unknown',updated_at=now() WHERE id=$1 AND status='created'",
                row["id"],
            )
            raise BillingError(
                "Checkout status is uncertain. No automatic retry; check funding history."
            ) from None
        url = str(result.get("url", ""))
        target = urlsplit(url)
        if (
            target.scheme != "https"
            or target.hostname != "checkout.stripe.com"
            or target.username
            or target.password
        ):
            raise BillingError("Stripe returned an unexpected checkout URL")
        await self.db.execute(
            """UPDATE agent_funding_orders SET checkout_id=$2,checkout_url=$3,
            status=CASE WHEN status IN ('created','unknown') THEN 'checkout' ELSE status END,updated_at=now()
            WHERE id=$1 AND (checkout_id IS NULL OR checkout_id=$2)""",
            row["id"],
            result["id"],
            url,
        )
        return public_order(
            await self.db.fetchrow("SELECT * FROM agent_funding_orders WHERE id=$1", row["id"])
        )

    async def customer(self, user_id, mode):
        customer = await self.db.fetchval(
            "SELECT customer_id FROM agent_funding_orders WHERE user_id=$1 AND livemode=$2 ORDER BY created_at DESC LIMIT 1",
            user_id,
            mode,
        )
        if customer:
            return customer
        existing = await BillingService(self.db, self.wallet, self.gateway)._find_user_customer(
            user_id
        )
        if existing:
            try:
                record = await provider_read(self.gateway.retrieve_customer, customer_id=existing)
            except Exception:  # noqa: BLE001 - redact provider errors and preserve the payment profile.
                raise BillingError(
                    "Saved payment profile needs verification before funding"
                ) from None
            if (
                record.get("id") == existing
                and record.get("livemode") is mode
                and not record.get("deleted")
            ):
                return existing
        email = await self.db.fetchval("SELECT email FROM auth.users WHERE id=$1", user_id)
        record = await provider_read(
            self.gateway.create_agent_customer, user_id=user_id, email=email, livemode=mode
        )
        if not isinstance(record.get("id"), str) or not record["id"].startswith("cus_"):
            raise BillingError("Payment profile unavailable")
        return record["id"]

    async def configure_auto(
        self,
        *,
        user_id,
        org_id,
        agent_id,
        enabled,
        threshold_v="100",
        amount_usd="10",
        monthly_cap_usd="50",
        consent=False,
    ):
        await self.authorize(self.db, user_id, org_id, agent_id, active=enabled)
        if not enabled:
            await self.db.execute(
                "UPDATE agent_auto_topups SET enabled=FALSE,pause_reason='owner_disabled',updated_at=now() WHERE agent_id=$1",
                agent_id,
            )
            return {"enabled": False}
        mode = require_enabled(self.gateway)
        if consent is not True:
            raise BillingError("Explicit automatic-charge consent is required")
        amount = decimal_amount(amount_usd)
        cap = decimal_amount(monthly_cap_usd, minimum=amount, maximum=Decimal(2000))
        threshold = decimal_amount(
            threshold_v, minimum=Decimal(".000001"), maximum=Decimal(1000000), places=6
        )
        # Use only this owner's customer and re-check the actual payment-method attachment.
        customer = await self.db.fetchval(
            "SELECT customer_id FROM agent_funding_orders WHERE user_id=$1 AND livemode=$2 AND status='paid' ORDER BY created_at DESC LIMIT 1",
            user_id,
            mode,
        )
        if not customer:
            raise BillingError(
                "Complete an agent checkout with Save card before enabling automatic top-ups"
            )
        methods = await provider_read(
            self.gateway.list_customer_card_payment_methods, customer_id=customer, limit=5
        )
        candidates = [
            p
            for p in methods.get("data", [])
            if p.get("customer") == customer and p.get("type") == "card"
        ]
        if not candidates:
            raise BillingError(
                "No saved card for this owner. Complete checkout with Save card first."
            )
        pm = candidates[0]["id"]
        async with self.db.transaction() as tx:
            await self.authorize(tx, user_id, org_id, agent_id)
            await tx.execute(
                """INSERT INTO agent_auto_topups(agent_id,org_id,user_id,enabled,threshold_v,amount_usd,
                monthly_cap_usd,customer_id,payment_method_id,livemode,consent_version,consent_at)
                VALUES($1,$2,$3,TRUE,$4,$5,$6,$7,$8,$9,$10,now())
                ON CONFLICT(agent_id) DO UPDATE SET user_id=$3,enabled=TRUE,threshold_v=$4,amount_usd=$5,
                monthly_cap_usd=$6,customer_id=$7,payment_method_id=$8,livemode=$9,consent_version=$10,
                consent_at=now(),pause_reason=NULL,updated_at=now()""",
                agent_id,
                org_id,
                user_id,
                threshold,
                amount,
                cap,
                customer,
                pm,
                mode,
                CONSENT_VERSION,
            )
        return {"enabled": True, "consent_version": CONSENT_VERSION}

    async def review_payment(self, *, user_id, org_id, agent_id, order_id, cancel=False):
        await self.authorize(self.db, user_id, org_id, agent_id, active=False)
        row = await self.db.fetchrow(
            "SELECT * FROM agent_funding_orders WHERE id=$1 AND org_id=$2 AND agent_id=$3",
            order_id,
            org_id,
            agent_id,
        )
        if not row or str(row["user_id"]) != str(user_id):
            raise BillingError("Only the payment owner may review or cancel this payment")
        if row["livemode"] is not live_mode(self.gateway):
            raise BillingError("Payment environment mismatch")
        pi = row["payment_intent_id"]
        if not pi and row["checkout_id"]:
            session = await provider_read(
                self.gateway.retrieve_agent_checkout, checkout_id=row["checkout_id"]
            )
            if (
                session.get("id") != row["checkout_id"]
                or session.get("customer") != row["customer_id"]
                or session.get("livemode") is not row["livemode"]
                or (session.get("metadata") or {}).get("agent_funding_id") != str(row["id"])
            ):
                raise BillingError("Checkout identity mismatch")
            pi = session.get("payment_intent")
        if not pi:
            return {
                "status": row["status"],
                "message": "No settled payment is confirmed. An uncertain request needs support review; no replacement charge will run automatically.",
            }
        intent = await provider_read(self.gateway.retrieve_agent_intent, payment_intent_id=pi)
        if (
            intent.get("id") != pi
            or intent.get("customer") != row["customer_id"]
            or intent.get("livemode") is not row["livemode"]
            or intent.get("currency") != "usd"
            or intent.get("amount") != int(row["amount_usd"] * 100)
            or (intent.get("metadata") or {}).get("agent_funding_id") != str(row["id"])
        ):
            raise BillingError("Payment identity mismatch")
        if cancel:
            if intent.get("status") not in {
                "requires_action",
                "requires_payment_method",
                "requires_confirmation",
            }:
                raise BillingError(
                    "This payment cannot be cancelled here. Review its current status; no new charge was made."
                )
            result = await provider_read(
                self.gateway.cancel_agent_intent, payment_intent_id=pi, order_id=str(row["id"])
            )
            if result.get("id") != pi or result.get("status") != "canceled":
                raise BillingError(
                    "Cancellation is not confirmed. Keep automatic top-ups off and check again."
                )
            async with self.db.transaction() as tx:
                await tx.execute(
                    "UPDATE agent_funding_orders SET status='failed',payment_intent_id=$2,updated_at=now() WHERE id=$1 AND credited_at IS NULL AND status!='review'",
                    row["id"],
                    pi,
                )
                await tx.execute(
                    "UPDATE agent_auto_topups SET enabled=FALSE,pause_reason='owner_cancelled_payment' WHERE agent_id=$1",
                    agent_id,
                )
            return {
                "status": "canceled",
                "message": "Pending payment cancelled. Use a new hosted checkout to confirm your card, then explicitly re-enable auto top-ups if wanted.",
            }
        return {
            "status": intent.get("status"),
            "message": "Stripe status checked without a new charge. Credits still require the verified webhook. If card confirmation is required, cancel this pending payment, then complete a new hosted checkout. Automatic top-ups stay paused.",
        }

    async def auto_tick(self, agent_id):
        mode = require_enabled(self.gateway)
        async with self.db.transaction() as tx:
            p = await tx.fetchrow(
                "SELECT * FROM agent_auto_topups WHERE agent_id=$1 FOR UPDATE", agent_id
            )
            if not p or not p["enabled"] or p["livemode"] != mode:
                return "disabled"
            await self.lock_funding(tx, agent_id)
            try:
                await self.authorize(tx, str(p["user_id"]), str(p["org_id"]), str(agent_id))
            except BillingError:
                await tx.execute(
                    "UPDATE agent_auto_topups SET enabled=FALSE,pause_reason='access_changed' WHERE agent_id=$1",
                    agent_id,
                )
                return "access_changed"
            if (
                p["last_attempt_at"]
                and (datetime.now(UTC) - p["last_attempt_at"]).total_seconds() < 3600
            ):
                return "cooldown"
            balance = await self.wallet.get_balance("agent:" + str(agent_id), tx=tx)
            if balance >= p["threshold_v"]:
                return "above_threshold"
            if await tx.fetchval(
                "SELECT EXISTS(SELECT 1 FROM agent_funding_orders WHERE agent_id=$1 AND source='auto' AND status IN ('created','pending','unknown','requires_action','review'))",
                agent_id,
            ):
                return "pending"
            # Count all attempts (including ambiguous/failed), across consent changes.
            used = await tx.fetchval(
                """SELECT COALESCE(sum(amount_usd),0) FROM agent_funding_orders
                WHERE agent_id=$1 AND source='auto' AND created_at>=date_trunc('month',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""",
                agent_id,
            )
            if used + p["amount_usd"] > p["monthly_cap_usd"]:
                return "monthly_cap"
            daily_limit = await self.daily_limit(tx, agent_id, mode, p["amount_usd"])
            if daily_limit:
                return daily_limit
            oid = str(uuid4())
            rate = decimal_amount(
                os.getenv("V_PER_USD", "100"),
                minimum=Decimal(".000001"),
                maximum=Decimal(1000000),
                places=6,
            )
            await tx.execute(
                """INSERT INTO agent_funding_orders(id,org_id,agent_id,user_id,amount_usd,credit_v,
                livemode,customer_id,payment_method_id,source,status,idempotency_key,payload_hash)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'auto','pending',$10,$10)""",
                oid,
                p["org_id"],
                agent_id,
                p["user_id"],
                p["amount_usd"],
                p["amount_usd"] * rate,
                mode,
                p["customer_id"],
                p["payment_method_id"],
                "auto:" + oid,
            )
            await tx.execute(
                "UPDATE agent_auto_topups SET last_attempt_at=now() WHERE agent_id=$1", agent_id
            )
        # The durable pending row is the authorization point. A later opt-out stops future attempts.
        try:
            result = await provider_read(
                self.gateway.charge_agent,
                customer_id=p["customer_id"],
                payment_method_id=p["payment_method_id"],
                amount_usd=p["amount_usd"],
                order_id=oid,
            )
            if (
                not str(result.get("id", "")).startswith("pi_")
                or result.get("customer") != p["customer_id"]
                or result.get("livemode") is not mode
                or result.get("currency") != "usd"
                or result.get("amount") != int(p["amount_usd"] * 100)
                or (result.get("metadata") or {}).get("agent_funding_id") != oid
            ):
                raise BillingError("Automatic payment identity mismatch")
            status = {
                "requires_action": "requires_action",
                "requires_payment_method": "failed",
                "canceled": "failed",
            }.get(result.get("status"), "pending")
            await self.db.execute(
                """UPDATE agent_funding_orders SET payment_intent_id=$2,status=$3,updated_at=now()
                WHERE id=$1 AND status='pending' AND payment_intent_id IS NULL""",
                oid,
                result["id"],
                status,
            )
        except Exception:  # noqa: BLE001 - retain the durable intent after any provider failure.
            # Even a network timeout may have charged. Never retry or create a replacement automatically.
            status = "unknown"
            await self.db.execute(
                "UPDATE agent_funding_orders SET status='unknown',updated_at=now() WHERE id=$1 AND status='pending'",
                oid,
            )
        if status in {"unknown", "requires_action", "failed"}:
            await self.db.execute(
                "UPDATE agent_auto_topups SET enabled=FALSE,pause_reason=$2 WHERE agent_id=$1",
                agent_id,
                status,
            )
        return status


async def dispatch_agent_event(tx, event, *, gateway, signature_verified):
    """Called inside the existing signed webhook journal transaction, even when sales are off."""
    if not await tx.fetchval("SELECT to_regclass('public.agent_funding_orders')"):
        return None
    obj = event.get("data", {}).get("object", {})
    metadata = obj.get("metadata") or {}
    oid = metadata.get("agent_funding_id")
    pi = obj.get("payment_intent") if obj.get("object") != "payment_intent" else obj.get("id")
    from openvegas.payments.adjustments import EVENTS

    if oid:
        try:
            oid = str(UUID(oid))
        except (ValueError, TypeError):
            raise BillingError("Invalid agent funding reference") from None
        row = await tx.fetchrow("SELECT * FROM agent_funding_orders WHERE id=$1 FOR UPDATE", oid)
    else:
        row = (
            await tx.fetchrow(
                "SELECT * FROM agent_funding_orders WHERE payment_intent_id=$1 FOR UPDATE", pi
            )
            if isinstance(pi, str)
            else None
        )
        if (
            not row
            and event.get("type") in EVENTS
            and isinstance(pi, str)
            and await tx.fetchval(
                "SELECT EXISTS(SELECT 1 FROM agent_funding_orders WHERE credited_at IS NULL)"
            )
        ):
            # Refunds may arrive before Checkout/PaymentIntent success. Read Stripe's
            # original immutable target, never guess a wallet from a customer alone.
            mode = live_mode(gateway)
            if (
                not signature_verified
                or event.get("livemode") is not mode
                or event.get("account")
                or event.get("context")
            ):
                raise BillingError("Agent payment signature, account or mode mismatch")
            intent = await provider_read(gateway.retrieve_agent_intent, payment_intent_id=pi)
            oid = (intent.get("metadata") or {}).get("agent_funding_id")
            if oid:
                try:
                    oid = str(UUID(oid))
                except (ValueError, TypeError):
                    raise BillingError("Invalid agent funding reference") from None
                row = await tx.fetchrow(
                    "SELECT * FROM agent_funding_orders WHERE id=$1 FOR UPDATE", oid
                )
                if row and (
                    intent.get("id") != pi
                    or intent.get("object") != "payment_intent"
                    or intent.get("customer") != row["customer_id"]
                    or intent.get("currency") != "usd"
                    or type(intent.get("amount")) is not int
                    or intent["amount"] != int(row["amount_usd"] * 100)
                    or intent.get("livemode") is not mode
                    or row["payment_intent_id"] not in {None, pi}
                ):
                    raise BillingError("Original agent payment identity mismatch")
                if row:
                    await tx.execute(
                        "UPDATE agent_funding_orders SET payment_intent_id=$2 WHERE id=$1",
                        row["id"],
                        pi,
                    )
    if not row:
        if oid:
            raise BillingError("Agent payment order not found")
        return None
    mode = live_mode(gateway)
    if (
        not signature_verified
        or type(event.get("livemode")) is not bool
        or event["livemode"] != mode
        or row["livemode"] != mode
        or event.get("account")
        or event.get("context")
    ):
        raise BillingError("Agent payment signature, account or mode mismatch")
    kind = event.get("type")
    if kind in EVENTS:
        return await adjust_agent_payment(tx, row, event, mode)
    if obj.get("customer") != row["customer_id"] or obj.get("currency") != "usd":
        raise BillingError("Agent payment customer or currency mismatch")
    session = obj.get("object") == "checkout.session"
    expected = int(row["amount_usd"] * 100)
    actual = obj.get("amount_total") if session else obj.get("amount")
    if type(actual) is not int or actual != expected:
        raise BillingError("Agent payment amount mismatch")
    if session and row["checkout_id"] and row["checkout_id"] != obj.get("id"):
        raise BillingError("Agent checkout identity mismatch")
    if row["payment_intent_id"] and pi and pi != row["payment_intent_id"]:
        raise BillingError("Agent payment intent identity mismatch")
    success = (
        session
        and kind in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}
        and obj.get("payment_status") == "paid"
    ) or (
        kind == "payment_intent.succeeded"
        and obj.get("status") == "succeeded"
        and obj.get("amount_received") == expected
    )
    if not success:
        status = {
            "payment_intent.payment_failed": "failed",
            "payment_intent.canceled": "failed",
            "payment_intent.requires_action": "requires_action",
            "checkout.session.expired": "expired",
        }.get(kind)
        if status and row["status"] not in {"paid", "review"}:
            await tx.execute(
                "UPDATE agent_funding_orders SET status=$2,updated_at=now() WHERE id=$1",
                row["id"],
                status,
            )
            await tx.execute(
                "UPDATE agent_auto_topups SET enabled=FALSE,pause_reason=$2 WHERE agent_id=$1",
                row["agent_id"],
                status,
            )
        return {"status": status or "pending", "agent_funding_id": str(row["id"])}
    if not isinstance(pi, str) or not pi.startswith("pi_"):
        raise BillingError("A settled agent payment must have a payment intent")
    if row["credited_at"] is not None:
        return {"status": row["status"], "idempotent": True}
    await WalletService(tx).fund_from_card(
        "agent:" + str(row["agent_id"]),
        row["credit_v"],
        "agent-funding:" + str(row["id"]),
        entry_type="agent_fiat_topup",
        tx=tx,
    )
    row = await tx.fetchrow(
        "UPDATE agent_funding_orders SET status=CASE WHEN status='review' THEN status ELSE 'paid' END,payment_intent_id=$2,credited_at=now(),updated_at=now() WHERE id=$1 RETURNING *",
        row["id"],
        pi,
    )
    adjustment = await apply_agent_adjustments(tx, row)
    return {
        "status": "review" if adjustment["status"] == "review" else "paid",
        "agent_funding_id": str(row["id"]),
    }


async def adjust_agent_payment(tx, row, event, mode):
    from openvegas.payments.adjustments import parse_event, transition

    fact = parse_event(event, expected_livemode=mode)
    old = await tx.fetchrow(
        "SELECT * FROM agent_payment_adjustments WHERE object_id=$1", fact.object_id
    )
    action, review = transition(dict(old) if old else None, fact)
    if action == "stale":
        return {"status": "duplicate_adjustment"}
    if old and old["order_id"] != row["id"]:
        raise BillingError("Adjustment order mismatch")
    await tx.execute(
        """INSERT INTO agent_payment_adjustments(object_id,order_id,kind,state,amount_minor,
        event_created,charge_id,currency,livemode,needs_review) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT(object_id) DO UPDATE SET state=$4,event_created=$6,needs_review=$10,
        amount_minor=greatest(agent_payment_adjustments.amount_minor,$5)""",
        fact.object_id,
        row["id"],
        fact.kind,
        fact.state,
        fact.amount_minor,
        fact.event_created,
        fact.charge_id,
        fact.currency,
        mode,
        review,
    )
    return await apply_agent_adjustments(tx, row)


async def apply_agent_adjustments(tx, row):
    facts = await tx.fetch("SELECT * FROM agent_payment_adjustments WHERE order_id=$1", row["id"])
    refunded = max(
        sum(
            f["amount_minor"] for f in facts if f["kind"] == "refund" and f["state"] == "succeeded"
        ),
        max((f["amount_minor"] for f in facts if f["kind"] == "charge_refund"), default=0),
        max(
            (f["amount_minor"] for f in facts if f["kind"] == "dispute" and f["state"] == "lost"),
            default=0,
        ),
    )
    total = int(row["amount_usd"] * 100)
    if refunded > total:
        raise BillingError("Cash reversal exceeds original agent payment")
    target = (row["credit_v"] * Decimal(refunded) / Decimal(total)).quantize(Decimal(".000001"))
    delta = max(Decimal(0), target - row["reversed_v"])
    restrict = (
        refunded > 0
        or row["status"] == "review"
        or any(
            f["needs_review"]
            or (f["kind"] == "dispute" and f["state"] not in {"won", "warning_closed"})
            for f in facts
        )
    )
    if restrict:
        # A reversal never becomes a fresh source of agent spend. Operator review restores access.
        await tx.execute(
            "UPDATE agent_accounts SET status='suspended' WHERE id=$1 AND status='active'",
            row["agent_id"],
        )
        await tx.execute(
            "UPDATE agent_auto_topups SET enabled=FALSE,pause_reason='payment_review' WHERE agent_id=$1",
            row["agent_id"],
        )
        if row["credited_at"] is None:
            await tx.execute(
                "UPDATE agent_funding_orders SET status='review',updated_at=now() WHERE id=$1",
                row["id"],
            )
            return {"status": "review", "reversed_v": str(row["reversed_v"])}
        account = "agent:" + str(row["agent_id"])
        available = await tx.fetchval(
            "SELECT balance FROM wallet_accounts WHERE account_id=$1 FOR NO KEY UPDATE", account
        )
        recover = min(delta, max(Decimal(0), available or Decimal(0)))
        if recover:
            await WalletService(tx)._execute(
                LedgerEntry(
                    debit_account=account,
                    credit_account="fiat_reserve",
                    amount=recover,
                    entry_type="agent_cash_reversal",
                    reference_id="agent-reversal:" + str(row["id"]) + ":" + str(target),
                ),
                tx=tx,
            )
        await tx.execute(
            "UPDATE agent_funding_orders SET status='review',reversed_v=$2,debt_v=debt_v+$3,updated_at=now() WHERE id=$1",
            row["id"],
            target,
            delta - recover,
        )
    return {"status": "review" if restrict else "adjustment_recorded", "reversed_v": str(target)}
