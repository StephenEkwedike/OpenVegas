"""Billing service for Stripe-backed topups and org sponsorship subscriptions."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from openvegas.wallet.ledger import WalletService
from .stripe_gateway import StripeGateway

V_SCALE = Decimal("0.000001")


class BillingError(Exception):
    pass


class IdempotencyConflict(BillingError):
    pass


class NotFoundError(BillingError):
    pass


class BillingService:
    def __init__(self, db: Any, wallet: WalletService, stripe_gateway: StripeGateway):
        self.db = db
        self.wallet = wallet
        self.stripe_gateway = stripe_gateway

    @staticmethod
    def canonical_payload_hash(payload: dict) -> str:
        def norm(v):
            if isinstance(v, Decimal):
                return format(v.normalize(), "f")
            if isinstance(v, dict):
                return {k: norm(v[k]) for k in sorted(v.keys())}
            if isinstance(v, list):
                return [norm(x) for x in v]
            return v

        canonical = json.dumps(norm(payload), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _money(value: Decimal | str | float) -> Decimal:
        return Decimal(str(value)).quantize(V_SCALE)

    @staticmethod
    def compute_has_active_subscription(subscription: dict) -> bool:
        status = str(subscription.get("status", "inactive"))
        status_ok = status in {"active", "trialing"}
        period_end = subscription.get("current_period_end")
        if not period_end:
            return status_ok
        return status_ok and datetime.fromtimestamp(int(period_end), tz=timezone.utc) > datetime.now(timezone.utc)

    async def resolve_org_id_from_subscription(self, subscription: dict, tx: Any) -> str:
        metadata = subscription.get("metadata") or {}
        org_id = metadata.get("org_id")
        if org_id:
            return str(org_id)

        # Fallback for older sessions lacking subscription_data.metadata.
        sub_id = subscription.get("id")
        if sub_id:
            row = await tx.fetchrow(
                "SELECT org_id FROM org_sponsorships WHERE stripe_subscription_id = $1",
                sub_id,
            )
            if row:
                return str(row["org_id"])

        raise BillingError("Missing org_id in subscription metadata")

    async def _ensure_user_customer(self, user_id: str) -> str:
        row = await self.db.fetchrow(
            """
            SELECT stripe_customer_id
            FROM fiat_topups
            WHERE user_id = $1
              AND stripe_customer_id IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
            """,
            user_id,
        )
        if row and row["stripe_customer_id"]:
            return str(row["stripe_customer_id"])

        user = await self.db.fetchrow(
            "SELECT email FROM auth.users WHERE id = $1",
            user_id,
        )
        email = str(user["email"]) if user and user.get("email") else None
        customer = self.stripe_gateway.create_customer(
            email=email,
            name=None,
            metadata={"user_id": str(user_id)},
        )
        return str(customer["id"])

    async def create_topup_checkout(self, *, user_id: str, amount_usd: Decimal, idempotency_key: str) -> dict:
        try:
            amount_usd = Decimal(str(amount_usd))
        except InvalidOperation as e:
            raise BillingError("Invalid amount") from e

        min_usd = Decimal(os.getenv("TOPUP_MIN_USD", "1"))
        max_usd = Decimal(os.getenv("TOPUP_MAX_USD", "500"))
        if amount_usd < min_usd or amount_usd > max_usd:
            raise BillingError(f"Amount must be between {min_usd} and {max_usd} USD")

        v_per_usd = Decimal(os.getenv("V_PER_USD", "100"))
        v_credit = (amount_usd * v_per_usd).quantize(V_SCALE)
        payload_hash = self.canonical_payload_hash(
            {"amount_usd": amount_usd, "currency": "usd"}
        )

        resume_existing = False
        async with self.db.transaction() as tx:
            existing = await tx.fetchrow(
                """
                SELECT *
                FROM fiat_topups
                WHERE user_id = $1 AND idempotency_key = $2
                FOR UPDATE
                """,
                user_id,
                idempotency_key,
            )
            if existing:
                if existing["idempotency_payload_hash"] != payload_hash:
                    raise IdempotencyConflict("IDEMPOTENCY_PAYLOAD_CONFLICT")
                status = str(existing["status"])
                if status in {"checkout_created", "paid"} and existing.get("stripe_checkout_session_id"):
                    return self._format_topup(existing)
                topup_id = str(existing["id"])
                resume_existing = True
            else:
                topup_id = str(uuid.uuid4())
                await tx.execute(
                    """
                    INSERT INTO fiat_topups
                      (id, user_id, amount_usd, v_credit, status, idempotency_key, idempotency_payload_hash)
                    VALUES ($1, $2, $3, $4, 'created', $5, $6)
                    """,
                    topup_id,
                    user_id,
                    amount_usd,
                    v_credit,
                    idempotency_key,
                    payload_hash,
                )

        customer_id = await self._ensure_user_customer(user_id)

        try:
            session = self.stripe_gateway.create_topup_checkout(
                customer_id=customer_id,
                amount_usd=amount_usd,
                topup_id=topup_id,
            )
        except Exception as e:
            await self.db.execute(
                """
                UPDATE fiat_topups
                SET status = 'failed', failure_reason = $2, updated_at = now()
                WHERE id = $1 AND status = 'created'
                """,
                topup_id,
                str(e)[:500],
            )
            raise BillingError("Unable to create Stripe Checkout session") from e

        row = await self.db.fetchrow(
            """
            UPDATE fiat_topups
            SET status = 'checkout_created',
                stripe_customer_id = $2,
                stripe_checkout_session_id = $3,
                stripe_checkout_url = $4,
                updated_at = now()
            WHERE id = $1
              AND status IN ('created', 'failed', 'checkout_created')
            RETURNING *
            """,
            topup_id,
            customer_id,
            session["id"],
            session["url"],
        )
        if not row:
            if resume_existing:
                latest = await self.db.fetchrow("SELECT * FROM fiat_topups WHERE id = $1", topup_id)
                if latest:
                    return self._format_topup(latest)
            raise BillingError("Unable to persist checkout session")

        return self._format_topup(row)

    async def get_topup_status(self, *, user_id: str, topup_id: str) -> dict:
        row = await self.db.fetchrow(
            """
            SELECT *
            FROM fiat_topups
            WHERE id = $1 AND user_id = $2
            """,
            topup_id,
            user_id,
        )
        if not row:
            raise NotFoundError("Top-up not found")
        return self._format_topup(row)

    async def create_org_subscription_checkout(self, *, org_id: str) -> dict:
        price_id = os.getenv("STRIPE_ORG_PRICE_ID", "").strip()
        if not price_id:
            raise BillingError("STRIPE_ORG_PRICE_ID is not configured")

        row = await self.db.fetchrow(
            """
            SELECT o.name, os.stripe_customer_id
            FROM org_sponsorships os
            JOIN organizations o ON o.id = os.org_id
            WHERE os.org_id = $1
            """,
            org_id,
        )
        if not row:
            raise NotFoundError("Org sponsorship not found")

        customer_id = row["stripe_customer_id"]
        if not customer_id:
            customer = self.stripe_gateway.create_customer(
                email=None,
                name=str(row["name"]),
                metadata={"org_id": str(org_id)},
            )
            customer_id = str(customer["id"])
            await self.db.execute(
                """
                UPDATE org_sponsorships
                SET stripe_customer_id = $2, stripe_price_id = $3, updated_at = now()
                WHERE org_id = $1
                """,
                org_id,
                customer_id,
                price_id,
            )

        checkout_attempt_id = str(uuid.uuid4())
        session = self.stripe_gateway.create_org_subscription_checkout(
            customer_id=str(customer_id),
            price_id=price_id,
            org_id=str(org_id),
            checkout_attempt_id=checkout_attempt_id,
        )
        await self.db.execute(
            """
            UPDATE org_sponsorships
            SET stripe_customer_id = $2,
                stripe_price_id = $3,
                updated_at = now()
            WHERE org_id = $1
            """,
            org_id,
            customer_id,
            price_id,
        )
        return {
            "org_id": str(org_id),
            "checkout_attempt_id": checkout_attempt_id,
            "checkout_session_id": str(session["id"]),
            "checkout_url": str(session["url"]),
        }

    async def get_org_subscription_status(self, *, org_id: str) -> dict:
        row = await self.db.fetchrow(
            """
            SELECT org_id, stripe_customer_id, stripe_subscription_id, stripe_price_id,
                   stripe_subscription_status, has_active_subscription, cancel_at_period_end,
                   current_period_end, updated_at
            FROM org_sponsorships
            WHERE org_id = $1
            """,
            org_id,
        )
        if not row:
            raise NotFoundError("Org sponsorship not found")
        return {
            "org_id": str(row["org_id"]),
            "stripe_customer_id": row["stripe_customer_id"],
            "stripe_subscription_id": row["stripe_subscription_id"],
            "stripe_price_id": row["stripe_price_id"],
            "stripe_subscription_status": row["stripe_subscription_status"] or "inactive",
            "has_active_subscription": bool(row["has_active_subscription"]),
            "cancel_at_period_end": bool(row["cancel_at_period_end"]),
            "current_period_end": row["current_period_end"].isoformat() if row["current_period_end"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    async def create_org_billing_portal(self, *, org_id: str, flow_type: str | None = None) -> dict:
        row = await self.db.fetchrow(
            """
            SELECT stripe_customer_id, stripe_subscription_id
            FROM org_sponsorships
            WHERE org_id = $1
            """,
            org_id,
        )
        if not row or not row["stripe_customer_id"]:
            raise BillingError("Org has no Stripe customer configured")

        url = self.stripe_gateway.create_billing_portal(
            customer_id=str(row["stripe_customer_id"]),
            flow_type=flow_type,
            subscription_id=str(row["stripe_subscription_id"]) if row["stripe_subscription_id"] else None,
        )
        return {"url": url}

    async def handle_webhook(self, *, raw_body: bytes, signature: str) -> dict:
        event = self.stripe_gateway.construct_event(raw_body, signature)
        return await self.handle_event(event)

    async def handle_event(self, event: dict) -> dict:
        event_id = str(event["id"])
        event_type = str(event["type"])
        payload_hash = hashlib.sha256(
            json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        obj = event["data"]["object"]

        async with self.db.transaction() as tx:
            existing = await tx.fetchrow(
                "SELECT payload_hash FROM stripe_webhook_events WHERE event_id = $1",
                event_id,
            )
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise BillingError(f"Webhook payload hash mismatch for event {event_id}")
                return {"status": "duplicate"}

            await tx.execute(
                """
                INSERT INTO stripe_webhook_events(event_id, event_type, payload_hash)
                VALUES ($1, $2, $3)
                """,
                event_id,
                event_type,
                payload_hash,
            )

            if event_type == "checkout.session.completed":
                return await self._handle_checkout_completed(tx=tx, session=obj)
            if event_type in {
                "customer.subscription.created",
                "customer.subscription.updated",
                "customer.subscription.deleted",
            }:
                return await self._handle_subscription_upsert(tx=tx, subscription=obj)
            if event_type == "invoice.paid":
                return await self._apply_org_budget_credit_once(tx=tx, invoice=obj)
            if event_type == "invoice.payment_failed":
                return await self._mark_org_subscription_past_due(tx=tx, invoice=obj)
            return {"status": "ignored"}

    async def _handle_checkout_completed(self, *, tx: Any, session: dict) -> dict:
        if session.get("mode") == "payment":
            return await self._settle_topup_from_checkout(tx=tx, session=session)
        # Subscription-mode checkout doesn't settle wallet value in this phase.
        return {"status": "ignored"}

    async def _settle_topup_from_checkout(self, *, tx: Any, session: dict) -> dict:
        if session.get("payment_status") != "paid":
            return {"status": "not-paid"}

        session_id = str(session["id"])
        row = await tx.fetchrow(
            """
            UPDATE fiat_topups
            SET status = 'paid',
                stripe_payment_intent_id = $2,
                updated_at = now()
            WHERE stripe_checkout_session_id = $1
              AND status IN ('created', 'checkout_created')
            RETURNING id, user_id, v_credit
            """,
            session_id,
            session.get("payment_intent"),
        )
        if not row:
            return {"status": "already-settled-or-missing"}

        await self.wallet.fund_from_card(
            account_id=f"user:{row['user_id']}",
            amount_v=Decimal(str(row["v_credit"])),
            reference_id=f"fiat_topup:{row['id']}",
            tx=tx,
        )
        return {"status": "paid", "topup_id": str(row["id"])}

    async def _handle_subscription_upsert(self, *, tx: Any, subscription: dict) -> dict:
        org_id = await self.resolve_org_id_from_subscription(subscription, tx=tx)
        await self.sync_org_sponsorship_from_subscription(
            tx=tx,
            org_id=org_id,
            subscription=subscription,
        )
        return {"status": "synced", "org_id": org_id}

    async def sync_org_sponsorship_from_subscription(
        self,
        *,
        tx: Any,
        org_id: str,
        subscription: dict,
    ) -> None:
        items = (subscription.get("items") or {}).get("data") or []
        item0 = items[0] if items else {}
        price = item0.get("price") if isinstance(item0, dict) else {}
        price_id = price.get("id") if isinstance(price, dict) else None
        period_end = subscription.get("current_period_end")

        row = await tx.fetchrow(
            """
            UPDATE org_sponsorships
            SET stripe_subscription_id = $2,
                stripe_customer_id = COALESCE($3, stripe_customer_id),
                stripe_subscription_status = $4,
                has_active_subscription = $5,
                stripe_price_id = COALESCE($6, stripe_price_id),
                cancel_at_period_end = $7,
                current_period_end = CASE WHEN $8::bigint IS NULL THEN NULL ELSE to_timestamp($8::bigint) END,
                updated_at = now()
            WHERE org_id = $1
            RETURNING org_id
            """,
            org_id,
            subscription.get("id"),
            subscription.get("customer"),
            subscription.get("status", "inactive"),
            self.compute_has_active_subscription(subscription),
            price_id,
            bool(subscription.get("cancel_at_period_end", False)),
            int(period_end) if period_end else None,
        )
        if not row:
            raise NotFoundError("Org sponsorship not found for subscription sync")

    async def _apply_org_budget_credit_once(self, *, tx: Any, invoice: dict) -> dict:
        subscription_id = invoice.get("subscription")
        invoice_id = invoice.get("id")
        if not subscription_id or not invoice_id:
            return {"status": "ignored"}

        org = await tx.fetchrow(
            "SELECT org_id FROM org_sponsorships WHERE stripe_subscription_id = $1",
            subscription_id,
        )
        if not org:
            return {"status": "org-not-found"}

        amount_paid = (Decimal(str(invoice.get("amount_paid", 0))) / Decimal("100")).quantize(
            Decimal("0.0001")
        )
        await tx.execute(
            """
            INSERT INTO org_budget_ledger (org_id, source, delta_usd, reference_id)
            VALUES ($1, 'stripe_subscription', $2, $3)
            ON CONFLICT DO NOTHING
            """,
            org["org_id"],
            amount_paid,
            f"stripe_invoice:{invoice_id}",
        )
        return {"status": "credited"}

    async def _mark_org_subscription_past_due(self, *, tx: Any, invoice: dict) -> dict:
        subscription_id = invoice.get("subscription")
        if not subscription_id:
            return {"status": "ignored"}
        await tx.execute(
            """
            UPDATE org_sponsorships
            SET stripe_subscription_status = 'past_due',
                has_active_subscription = FALSE,
                updated_at = now()
            WHERE stripe_subscription_id = $1
            """,
            subscription_id,
        )
        return {"status": "past_due"}

    @staticmethod
    def _format_topup(row: Any) -> dict:
        return {
            "topup_id": str(row["id"]),
            "status": str(row["status"]),
            "amount_usd": str(row["amount_usd"]),
            "v_credit": str(row["v_credit"]),
            "checkout_session_id": row["stripe_checkout_session_id"],
            "checkout_url": row.get("stripe_checkout_url"),
            "payment_intent_id": row["stripe_payment_intent_id"],
            "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        }
