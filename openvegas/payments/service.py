"""Billing service for Stripe-backed topups and subscriptions (user + org)."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from openvegas.qr_runtime import ensure_qrcode_available
from openvegas.telemetry import emit_metric
from openvegas.wallet.ledger import WalletService

V_SCALE = Decimal("0.000001")
USD_SCALE = Decimal("0.01")


class BillingError(Exception):
    pass


class IdempotencyConflict(BillingError):
    pass


class NotFoundError(BillingError):
    pass


class BillingService:
    def __init__(self, db: Any, wallet: WalletService, stripe_gateway: Any):
        self.db = db
        self.wallet = wallet
        self.stripe_gateway = stripe_gateway
        self.provider_mode = str(getattr(stripe_gateway, "mode", "stripe"))

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
    def _usd(value: Decimal | str | float) -> Decimal:
        return Decimal(str(value)).quantize(USD_SCALE)

    @staticmethod
    def _row_get(row: Any, key: str, default: Any = None) -> Any:
        if row is None:
            return default
        try:
            if key in row:
                return row[key]
        except Exception:
            pass
        if isinstance(row, dict):
            return row.get(key, default)
        return default

    @staticmethod
    def _fmt_usd(value: Decimal | str | float | None) -> str | None:
        if value is None:
            return None
        return format(Decimal(str(value)).quantize(USD_SCALE), "f")

    @staticmethod
    def _fmt_v(value: Decimal | str | float | None) -> str | None:
        if value is None:
            return None
        return format(Decimal(str(value)).quantize(V_SCALE), "f")

    @staticmethod
    def _fmt_v_2(value: Decimal | str | float | None) -> str | None:
        if value is None:
            return None
        return format(Decimal(str(value)).quantize(USD_SCALE), "f")

    @staticmethod
    def _payment_methods_display() -> list[str]:
        return ["Card", "PayPal", "Apple Pay", "Alipay"]

    @staticmethod
    def _default_topup_usd() -> Decimal:
        return Decimal(os.getenv("TOPUP_SUGGEST_DEFAULT_USD", "20.00")).quantize(USD_SCALE)

    @staticmethod
    def _low_balance_floor_usd() -> Decimal:
        return Decimal(os.getenv("TOPUP_LOW_BALANCE_FLOOR_USD", "5.00")).quantize(USD_SCALE)

    @staticmethod
    def _v_per_usd() -> Decimal:
        return Decimal(os.getenv("V_PER_USD", "100")).quantize(V_SCALE)

    @staticmethod
    def _checkout_ttl_sec() -> int:
        return max(60, int(os.getenv("TOPUP_CHECKOUT_EXPIRY_SEC", "3600")))

    @staticmethod
    def _late_settlement_window_sec() -> int:
        return max(60, int(os.getenv("TOPUP_STRIPE_LATE_SETTLEMENT_WINDOW_SEC", "259200")))

    @staticmethod
    def _continuation_max_v() -> Decimal:
        return Decimal(os.getenv("BNPL_CONTINUATION_MAX_V", "50")).quantize(V_SCALE)

    @staticmethod
    def _continuation_cooldown_hours() -> int:
        return max(1, int(os.getenv("BNPL_CONTINUATION_COOLDOWN_HOURS", "168")))

    @staticmethod
    def _continuation_risk_blocked_users() -> set[str]:
        raw = str(os.getenv("BNPL_CONTINUATION_RISK_BLOCKED_USER_IDS", "")).strip()
        if not raw:
            return set()
        return {part.strip() for part in raw.split(",") if part.strip()}

    async def _active_continuation_row(self, *, tx: Any, user_id: str) -> Any | None:
        return await tx.fetchrow(
            """
            SELECT id, principal_v, outstanding_v, status, cooldown_until, issued_at, repaid_at
            FROM user_continuation_credit
            WHERE user_id = $1
              AND status = 'active'
            ORDER BY issued_at DESC
            LIMIT 1
            FOR UPDATE
            """,
            user_id,
        )

    async def _latest_continuation_row(self, *, tx: Any, user_id: str) -> Any | None:
        return await tx.fetchrow(
            """
            SELECT id, principal_v, outstanding_v, status, cooldown_until, issued_at, repaid_at
            FROM user_continuation_credit
            WHERE user_id = $1
            ORDER BY issued_at DESC
            LIMIT 1
            FOR UPDATE
            """,
            user_id,
        )

    async def _has_paid_topup(self, *, tx: Any, user_id: str) -> bool:
        row = await tx.fetchrow(
            """
            SELECT 1
            FROM fiat_topups
            WHERE user_id = $1
              AND status = 'paid'
            LIMIT 1
            """,
            user_id,
        )
        return bool(row)

    @staticmethod
    def _preview_timestamp() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _emit_outstanding_gauge(self, outstanding_v: Decimal) -> None:
        emit_metric(
            "continuation_outstanding_v_gauge",
            {"outstanding_v": self._fmt_v(outstanding_v)},
        )

    def _resolve_mode(self) -> str:
        mode = str(self.provider_mode or "stripe").lower().strip()
        return mode if mode in {"stripe", "simulated"} else "stripe"

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _is_pending_status(cls, status: str) -> bool:
        return str(status) in {"created", "checkout_created"}

    @classmethod
    def _is_expired(cls, row: Any, now: datetime | None = None) -> bool:
        status = str(cls._row_get(row, "status", ""))
        if not cls._is_pending_status(status):
            return False
        expires_at = cls._row_get(row, "expires_at")
        if not expires_at:
            return False
        ts = expires_at
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        point = now or cls._utc_now()
        return ts <= point

    async def _mark_expired_if_needed(self, *, tx: Any, row: Any) -> Any:
        if not self._is_expired(row):
            return row
        topup_id = str(self._row_get(row, "id", ""))
        status_before = str(self._row_get(row, "status", ""))
        updated = await tx.fetchrow(
            """
            UPDATE fiat_topups
            SET status = 'expired',
                updated_at = now()
            WHERE id = $1
              AND status IN ('created', 'checkout_created')
            RETURNING *
            """,
            topup_id,
        )
        if updated:
            emit_metric(
                "topup_status_transition_total",
                {"from": status_before, "to": "expired", "mode": str(self._row_get(row, "mode", self._resolve_mode()))},
            )
            return updated
        latest = await tx.fetchrow("SELECT * FROM fiat_topups WHERE id = $1", topup_id)
        return latest or row

    async def _mark_manual_reconciliation_required(
        self,
        *,
        tx: Any,
        topup_id: str,
        mode: str,
        reason: str,
    ) -> None:
        row = await tx.fetchrow(
            """
            UPDATE fiat_topups
            SET status = 'manual_reconciliation_required',
                manual_reconciliation_required = TRUE,
                manual_reconciliation_reason = $2,
                manual_reconciliation_marked_at = now(),
                updated_at = now()
            WHERE id = $1
            RETURNING id
            """,
            topup_id,
            reason[:200],
        )
        if row:
            emit_metric(
                "topup_status_transition_total",
                {"from": "expired", "to": "manual_reconciliation_required", "mode": mode},
            )
            emit_metric("topup_late_settlement_manual_review_total", {"mode": mode})

    @staticmethod
    def _render_qr_svg(value: str) -> bytes:
        ok, runtime_reason = ensure_qrcode_available()
        if not ok:
            escaped = (
                str(value)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            escaped_reason = (
                str(runtime_reason or "unknown")
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )[:260]
            svg = (
                "<svg xmlns='http://www.w3.org/2000/svg' width='920' height='180'>"
                "<rect width='100%' height='100%' fill='white'/>"
                "<text x='12' y='24' font-family='monospace' font-size='14' fill='black'>"
                "QR unavailable in this runtime; use checkout URL:"
                "</text>"
                f"<text x='12' y='56' font-family='monospace' font-size='13' fill='black'>{escaped}</text>"
                f"<text x='12' y='88' font-family='monospace' font-size='12' fill='black'>reason: {escaped_reason}</text>"
                "</svg>"
            )
            return svg.encode("utf-8")

        try:
            import qrcode  # type: ignore
            import qrcode.image.svg  # type: ignore

            image = qrcode.make(value, image_factory=qrcode.image.svg.SvgPathImage)
            raw = image.to_string()
            if isinstance(raw, bytes):
                return raw
            return str(raw).encode("utf-8")
        except Exception as exc:
            reason = f"{exc.__class__.__name__}: {str(exc)}".strip()[:220]
            escaped = (
                str(value)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            escaped_reason = (
                reason.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                if reason
                else "unknown"
            )
            svg = (
                "<svg xmlns='http://www.w3.org/2000/svg' width='900' height='160'>"
                "<rect width='100%' height='100%' fill='white'/>"
                "<text x='12' y='24' font-family='monospace' font-size='14' fill='black'>"
                "QR unavailable in this runtime; use checkout URL:"
                "</text>"
                f"<text x='12' y='56' font-family='monospace' font-size='13' fill='black'>{escaped}</text>"
                f"<text x='12' y='88' font-family='monospace' font-size='12' fill='black'>reason: {escaped_reason}</text>"
                "</svg>"
            )
            return svg.encode("utf-8")

    @staticmethod
    def _provider_paid_at_from_event(event: dict, fallback: datetime) -> datetime:
        created = event.get("created")
        if created is None:
            return fallback
        try:
            return datetime.fromtimestamp(int(created), tz=timezone.utc)
        except Exception:
            return fallback

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

    async def resolve_user_id_from_subscription(self, subscription: dict, tx: Any) -> str:
        metadata = subscription.get("metadata") or {}
        user_id = metadata.get("user_id")
        if user_id:
            return str(user_id)

        sub_id = subscription.get("id")
        if sub_id:
            row = await tx.fetchrow(
                "SELECT user_id FROM user_subscriptions WHERE stripe_subscription_id = $1",
                sub_id,
            )
            if row:
                return str(row["user_id"])

        raise BillingError("Missing user_id in subscription metadata")

    async def _ensure_user_customer(self, user_id: str) -> str:
        existing = await self._find_user_customer(user_id)
        if existing:
            return existing
        return await self._create_user_customer(user_id)

    async def _find_user_customer(self, user_id: str) -> str | None:
        row = await self.db.fetchrow(
            """
            SELECT stripe_customer_id
            FROM fiat_topups
            WHERE user_id = $1
              AND stripe_customer_id IS NOT NULL
              AND mode = 'stripe'
              AND stripe_customer_id LIKE 'cus_%'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            user_id,
        )
        if row and row["stripe_customer_id"]:
            return str(row["stripe_customer_id"])

        row = await self.db.fetchrow(
            """
            SELECT stripe_customer_id
            FROM user_subscriptions
            WHERE user_id = $1
              AND stripe_customer_id IS NOT NULL
              AND stripe_customer_id LIKE 'cus_%'
            """,
            user_id,
        )
        if row and row["stripe_customer_id"]:
            return str(row["stripe_customer_id"])
        return None

    async def _create_user_customer(self, user_id: str) -> str:
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

    async def _ensure_user_subscription_customer(self, user_id: str) -> str:
        row = await self.db.fetchrow(
            """
            SELECT stripe_customer_id
            FROM user_subscriptions
            WHERE user_id = $1
              AND stripe_customer_id IS NOT NULL
              AND stripe_customer_id LIKE 'cus_%'
            """,
            user_id,
        )
        if row and row["stripe_customer_id"]:
            return str(row["stripe_customer_id"])
        return await self._create_user_customer(user_id)

    async def create_topup_checkout(self, *, user_id: str, amount_usd: Decimal, idempotency_key: str) -> dict:
        try:
            amount_usd = Decimal(str(amount_usd))
        except InvalidOperation as e:
            raise BillingError("Invalid amount") from e

        min_usd = Decimal(os.getenv("TOPUP_MIN_USD", "10"))
        max_usd = Decimal(os.getenv("TOPUP_MAX_USD", "500"))
        if amount_usd < min_usd or amount_usd > max_usd:
            raise BillingError(f"Amount must be between {min_usd} and {max_usd} USD")

        v_per_usd = Decimal(os.getenv("V_PER_USD", "100"))
        v_credit = (amount_usd * v_per_usd).quantize(V_SCALE)
        payload_hash = self.canonical_payload_hash(
            {"amount_usd": amount_usd, "currency": "usd"}
        )

        mode = self._resolve_mode()
        expires_at = self._utc_now() + timedelta(seconds=self._checkout_ttl_sec())
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
                existing = await self._mark_expired_if_needed(tx=tx, row=existing)
                status = str(existing["status"])
                if status in {"checkout_created", "paid"} and self._row_get(existing, "stripe_checkout_session_id"):
                    return self._format_topup(existing)
                topup_id = str(existing["id"])
                resume_existing = True
            else:
                topup_id = str(uuid.uuid4())
                await tx.execute(
                    """
                    INSERT INTO fiat_topups
                      (id, user_id, amount_usd, v_credit, status, idempotency_key, idempotency_payload_hash, mode, expires_at)
                    VALUES ($1, $2, $3, $4, 'created', $5, $6, $7, $8)
                    """,
                    topup_id,
                    user_id,
                    amount_usd,
                    v_credit,
                    idempotency_key,
                    payload_hash,
                    mode,
                    expires_at,
                )

        if mode == "simulated":
            customer_id = f"sim_{user_id}"
        else:
            customer_id = await self._ensure_user_customer(user_id)

        try:
            session = self.stripe_gateway.create_topup_checkout(
                customer_id=customer_id,
                amount_usd=amount_usd,
                topup_id=topup_id,
            )
        except Exception as e:
            msg = str(e)
            # Auto-recover stale/simulated customer ids from legacy rows.
            if mode != "simulated" and "no such customer" in msg.lower():
                try:
                    customer_id = await self._create_user_customer(user_id)
                    session = self.stripe_gateway.create_topup_checkout(
                        customer_id=customer_id,
                        amount_usd=amount_usd,
                        topup_id=topup_id,
                    )
                except Exception as retry_e:
                    await self.db.execute(
                        """
                        UPDATE fiat_topups
                        SET status = 'failed', failure_reason = $2, updated_at = now()
                        WHERE id = $1 AND status = 'created'
                        """,
                        topup_id,
                        str(retry_e)[:500],
                    )
                    emit_metric("topup_checkout_session_total", {"mode": mode, "status": "failure", "reason": "customer_retry_failed"})
                    raise BillingError("Unable to create Stripe Checkout session") from retry_e
            else:
                await self.db.execute(
                    """
                    UPDATE fiat_topups
                    SET status = 'failed', failure_reason = $2, updated_at = now()
                    WHERE id = $1 AND status = 'created'
                    """,
                    topup_id,
                    msg[:500],
                )
                emit_metric("topup_checkout_session_total", {"mode": mode, "status": "failure", "reason": "session_create_failed"})
                raise BillingError("Unable to create Stripe Checkout session") from e

        row = await self.db.fetchrow(
            """
            UPDATE fiat_topups
            SET status = 'checkout_created',
                stripe_customer_id = $2,
                stripe_checkout_session_id = $3,
                stripe_checkout_url = $4,
                mode = $5,
                expires_at = $6,
                updated_at = now()
            WHERE id = $1
              AND status IN ('created', 'failed', 'checkout_created')
            RETURNING *
            """,
            topup_id,
            customer_id,
            session["id"],
            session["url"],
            mode,
            expires_at,
        )
        if not row:
            if resume_existing:
                latest = await self.db.fetchrow("SELECT * FROM fiat_topups WHERE id = $1", topup_id)
                if latest:
                    return self._format_topup(latest)
            emit_metric("topup_checkout_session_total", {"mode": mode, "status": "failure", "reason": "persist_failed"})
            raise BillingError("Unable to persist checkout session")

        emit_metric("topup_checkout_created_total", {"mode": mode})
        emit_metric("topup_checkout_session_total", {"mode": mode, "status": "success"})
        return self._format_topup(row)

    async def get_saved_payment_method_status(self, *, user_id: str) -> dict:
        if self._resolve_mode() != "stripe":
            return {
                "available": False,
                "reason": "billing_mode_not_stripe",
                "customer_id": None,
            }
        customer_id = await self._find_user_customer(user_id)
        if not customer_id:
            return {
                "available": False,
                "reason": "missing_customer",
                "customer_id": None,
            }
        try:
            customer = self.stripe_gateway.retrieve_customer(customer_id=customer_id)
        except Exception:
            return {
                "available": False,
                "reason": "customer_lookup_failed",
                "customer_id": customer_id,
            }
        customer_get = customer.get if hasattr(customer, "get") else (lambda _k, _d=None: _d)
        customer_invoice_settings = customer_get("invoice_settings", {}) or {}
        default_pm = str((customer_invoice_settings or {}).get("default_payment_method") or "").strip()
        pm_data: dict[str, Any] | None = None
        if default_pm:
            try:
                pm_data = self.stripe_gateway.retrieve_payment_method(payment_method_id=default_pm)
            except Exception:
                pm_data = None
        if pm_data is None:
            try:
                methods = self.stripe_gateway.list_customer_card_payment_methods(customer_id=customer_id, limit=5)
            except Exception:
                methods = {"data": []}
            method_get = methods.get if hasattr(methods, "get") else (lambda _k, _d=None: _d)
            rows = method_get("data", []) or []
            if rows:
                first = rows[0]
                pm_data = first if hasattr(first, "get") else None
        if not pm_data:
            return {
                "available": False,
                "reason": "missing_payment_method",
                "customer_id": customer_id,
            }
        pm_get = pm_data.get if hasattr(pm_data, "get") else (lambda _k, _d=None: _d)
        card = pm_get("card", {}) or {}
        return {
            "available": True,
            "reason": "ok",
            "customer_id": customer_id,
            "payment_method_id": str(pm_get("id") or ""),
            "brand": str((card or {}).get("brand") or ""),
            "last4": str((card or {}).get("last4") or ""),
            "exp_month": int((card or {}).get("exp_month") or 0),
            "exp_year": int((card or {}).get("exp_year") or 0),
        }

    async def charge_saved_topup(self, *, user_id: str, amount_usd: Decimal, idempotency_key: str) -> dict:
        if self._resolve_mode() != "stripe":
            raise BillingError("Saved-card top-up requires Stripe billing mode")

        try:
            amount_usd = Decimal(str(amount_usd))
        except InvalidOperation as e:
            raise BillingError("Invalid amount") from e

        min_usd = Decimal(os.getenv("TOPUP_MIN_USD", "10"))
        max_usd = Decimal(os.getenv("TOPUP_MAX_USD", "500"))
        if amount_usd < min_usd or amount_usd > max_usd:
            raise BillingError(f"Amount must be between {min_usd} and {max_usd} USD")

        payment_status = await self.get_saved_payment_method_status(user_id=user_id)
        if not payment_status.get("available"):
            raise BillingError("No saved card available. Use checkout to add or update a card.")

        customer_id = str(payment_status.get("customer_id") or "")
        payment_method_id = str(payment_status.get("payment_method_id") or "")
        if not customer_id or not payment_method_id:
            raise BillingError("Saved card is not configured correctly")

        v_per_usd = Decimal(os.getenv("V_PER_USD", "100"))
        v_credit = (amount_usd * v_per_usd).quantize(V_SCALE)
        payload_hash = self.canonical_payload_hash(
            {"amount_usd": amount_usd, "currency": "usd", "charge_type": "saved_card"}
        )

        topup_id = ""
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
                if status == "paid":
                    return self._format_topup(existing)
                topup_id = str(existing["id"])
            else:
                topup_id = str(uuid.uuid4())
                await tx.execute(
                    """
                    INSERT INTO fiat_topups
                      (id, user_id, amount_usd, v_credit, status, idempotency_key, idempotency_payload_hash, mode, stripe_customer_id, expires_at)
                    VALUES ($1, $2, $3, $4, 'created', $5, $6, 'stripe', $7, NULL)
                    """,
                    topup_id,
                    user_id,
                    amount_usd,
                    v_credit,
                    idempotency_key,
                    payload_hash,
                    customer_id,
                )

        try:
            intent = self.stripe_gateway.create_saved_card_topup_payment_intent(
                customer_id=customer_id,
                payment_method_id=payment_method_id,
                amount_usd=amount_usd,
                topup_id=topup_id,
            )
        except Exception as e:
            await self.db.execute(
                """
                UPDATE fiat_topups
                SET status = 'failed', failure_reason = $2, updated_at = now()
                WHERE id = $1
                """,
                topup_id,
                str(e)[:500],
            )
            emit_metric("topup_saved_card_charge_total", {"status": "failure", "reason": "intent_create_failed"})
            raise BillingError("Unable to charge saved card") from e

        intent_id = str(intent.get("id") or "")
        intent_status = str(intent.get("status") or "")
        if intent_status != "succeeded":
            await self.db.execute(
                """
                UPDATE fiat_topups
                SET status = 'failed',
                    stripe_payment_intent_id = COALESCE($2, stripe_payment_intent_id),
                    failure_reason = $3,
                    updated_at = now()
                WHERE id = $1
                """,
                topup_id,
                intent_id or None,
                f"payment_intent_status:{intent_status}"[:500],
            )
            emit_metric(
                "topup_saved_card_charge_total",
                {"status": "failure", "reason": f"intent_status_{(intent_status or 'unknown')[:32]}"},
            )
            raise BillingError(f"Saved-card charge did not complete ({intent_status or 'unknown'})")

        async with self.db.transaction() as tx:
            row = await tx.fetchrow(
                """
                SELECT *
                FROM fiat_topups
                WHERE id = $1
                FOR UPDATE
                """,
                topup_id,
            )
            if not row:
                raise BillingError("Top-up not found after charge")
            await self._settle_topup_paid(
                tx=tx,
                row=row,
                provider_ref=intent_id or None,
                settlement_surface="stripe",
                provider_paid_at=self._utc_now(),
            )
            final = await tx.fetchrow("SELECT * FROM fiat_topups WHERE id = $1", topup_id)
        if not final:
            raise BillingError("Unable to fetch settled top-up")
        emit_metric("topup_saved_card_charge_total", {"status": "success"})
        return self._format_topup(final)

    async def preview_topup_checkout(self, *, user_id: str, amount_usd: Decimal) -> dict:
        try:
            amount_usd = Decimal(str(amount_usd))
        except InvalidOperation as e:
            raise BillingError("Invalid amount") from e

        min_usd = Decimal(os.getenv("TOPUP_MIN_USD", "10"))
        max_usd = Decimal(os.getenv("TOPUP_MAX_USD", "500"))
        if amount_usd < min_usd or amount_usd > max_usd:
            raise BillingError(f"Amount must be between {min_usd} and {max_usd} USD")

        v_credit_gross = (amount_usd * self._v_per_usd()).quantize(V_SCALE)
        async with self.db.transaction() as tx:
            active = await self._active_continuation_row(tx=tx, user_id=user_id)
            outstanding_v = self._money(self._row_get(active, "outstanding_v", "0"))
            repay_v = min(v_credit_gross, outstanding_v)
            net_credit_v = (v_credit_gross - repay_v).quantize(V_SCALE)
        return {
            "amount_usd": self._fmt_usd(amount_usd),
            "v_credit_gross": self._fmt_v(v_credit_gross),
            "outstanding_principal_v": self._fmt_v(outstanding_v),
            "repay_v": self._fmt_v(repay_v),
            "net_credit_v": self._fmt_v(net_credit_v),
            "preview_is_estimate": True,
            "preview_generated_at": self._preview_timestamp(),
            "preview_basis_outstanding_principal_v": self._fmt_v(outstanding_v),
        }

    async def get_continuation_status(self, *, user_id: str) -> dict:
        async with self.db.transaction() as tx:
            active = await self._active_continuation_row(tx=tx, user_id=user_id)
            if active:
                outstanding_v = self._money(self._row_get(active, "outstanding_v", "0"))
                cooldown_until = self._row_get(active, "cooldown_until")
                self._emit_outstanding_gauge(outstanding_v)
                return {
                    "eligible": False,
                    "deny_reason": "outstanding_exists",
                    "outstanding_principal_v": self._fmt_v(outstanding_v),
                    "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
                }

            latest = await self._latest_continuation_row(tx=tx, user_id=user_id)
            cooldown_until = self._row_get(latest, "cooldown_until")
            now = self._utc_now()
            has_paid = await self._has_paid_topup(tx=tx, user_id=user_id)
            if not has_paid:
                return {
                    "eligible": False,
                    "deny_reason": "no_paid_history",
                    "outstanding_principal_v": self._fmt_v(Decimal("0")),
                    "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
                }

            if cooldown_until and cooldown_until > now:
                return {
                    "eligible": False,
                    "deny_reason": "cooldown_active",
                    "outstanding_principal_v": self._fmt_v(Decimal("0")),
                    "cooldown_until": cooldown_until.isoformat(),
                }

            if user_id in self._continuation_risk_blocked_users():
                return {
                    "eligible": False,
                    "deny_reason": "risk_blocked",
                    "outstanding_principal_v": self._fmt_v(Decimal("0")),
                    "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
                }

            self._emit_outstanding_gauge(Decimal("0"))
            return {
                "eligible": True,
                "deny_reason": None,
                "outstanding_principal_v": self._fmt_v(Decimal("0")),
                "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
            }

    async def claim_continuation(
        self,
        *,
        user_id: str,
        idempotency_key: str | None = None,
    ) -> dict:
        idem = str(idempotency_key or f"continuation-{uuid.uuid4().hex[:16]}").strip()
        if not idem:
            raise BillingError("Invalid idempotency key")
        payload_hash = self.canonical_payload_hash({"action": "claim_continuation_v1"})

        async with self.db.transaction() as tx:
            replay = await tx.fetchrow(
                """
                SELECT payload_hash, response_json
                FROM continuation_claim_idempotency
                WHERE user_id = $1 AND idempotency_key = $2
                FOR UPDATE
                """,
                user_id,
                idem,
            )
            if replay:
                if str(replay["payload_hash"]) != payload_hash:
                    raise IdempotencyConflict("IDEMPOTENCY_PAYLOAD_CONFLICT")
                emit_metric("continuation_claim_idempotent_replay_total", {"outcome": "replay"})
                stored = replay["response_json"]
                if isinstance(stored, str):
                    try:
                        stored = json.loads(stored)
                    except Exception:
                        stored = {"status": "denied", "deny_reason": "idempotency_replay_decode_error"}
                return dict(stored)

            active = await self._active_continuation_row(tx=tx, user_id=user_id)
            if active:
                response = {
                    "status": "already_active",
                    "principal_v": self._fmt_v(self._row_get(active, "principal_v")),
                    "outstanding_principal_v": self._fmt_v(self._row_get(active, "outstanding_v")),
                    "cooldown_until": self._row_get(active, "cooldown_until").isoformat()
                    if self._row_get(active, "cooldown_until")
                    else None,
                }
                emit_metric("continuation_claim_attempt_total", {"outcome": "denied", "reason": "outstanding_exists"})
                await tx.execute(
                    """
                    INSERT INTO continuation_claim_idempotency (user_id, idempotency_key, payload_hash, response_json)
                    VALUES ($1, $2, $3, $4::jsonb)
                    """,
                    user_id,
                    idem,
                    payload_hash,
                    json.dumps(response),
                )
                return response

            latest = await self._latest_continuation_row(tx=tx, user_id=user_id)
            cooldown_until = self._row_get(latest, "cooldown_until")
            now = self._utc_now()
            has_paid = await self._has_paid_topup(tx=tx, user_id=user_id)
            deny_reason = None
            if not has_paid:
                deny_reason = "no_paid_history"
            elif cooldown_until and cooldown_until > now:
                deny_reason = "cooldown_active"
            elif user_id in self._continuation_risk_blocked_users():
                deny_reason = "risk_blocked"

            if deny_reason is not None:
                emit_metric("continuation_claim_attempt_total", {"outcome": "denied", "reason": deny_reason})
                self._emit_outstanding_gauge(Decimal("0"))
                response = {
                    "status": "denied",
                    "deny_reason": deny_reason,
                    "outstanding_principal_v": self._fmt_v(Decimal("0")),
                    "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
                }
                await tx.execute(
                    """
                    INSERT INTO continuation_claim_idempotency (user_id, idempotency_key, payload_hash, response_json)
                    VALUES ($1, $2, $3, $4::jsonb)
                    """,
                    user_id,
                    idem,
                    payload_hash,
                    json.dumps(response),
                )
                return response

            principal_v = self._continuation_max_v()
            cooldown_until = self._utc_now() + timedelta(hours=self._continuation_cooldown_hours())
            row = await tx.fetchrow(
                """
                INSERT INTO user_continuation_credit (user_id, principal_v, outstanding_v, status, cooldown_until)
                VALUES ($1, $2, $2, 'active', $3)
                RETURNING id, principal_v, outstanding_v, cooldown_until
                """,
                user_id,
                principal_v,
                cooldown_until,
            )
            continuation_id = str(self._row_get(row, "id", ""))
            await self.wallet.fund_from_card(
                account_id=f"user:{user_id}",
                amount_v=principal_v,
                reference_id=f"continuation:{continuation_id}",
                entry_type="continuation_credit",
                tx=tx,
            )
            self._emit_outstanding_gauge(principal_v)
            emit_metric("continuation_claim_attempt_total", {"outcome": "granted", "reason": "eligible"})
            response = {
                "status": "granted",
                "principal_v": self._fmt_v(self._row_get(row, "principal_v")),
                "outstanding_principal_v": self._fmt_v(self._row_get(row, "outstanding_v")),
                "cooldown_until": self._row_get(row, "cooldown_until").isoformat()
                if self._row_get(row, "cooldown_until")
                else None,
            }
            await tx.execute(
                """
                INSERT INTO continuation_claim_idempotency (user_id, idempotency_key, payload_hash, response_json)
                VALUES ($1, $2, $3, $4::jsonb)
                """,
                user_id,
                idem,
                payload_hash,
                json.dumps(response),
            )
            return response

    async def cancel_active_continuation(
        self,
        *,
        user_id: str,
        reason: str,
        actor: str = "system",
    ) -> dict:
        async with self.db.transaction() as tx:
            active = await self._active_continuation_row(tx=tx, user_id=user_id)
            if not active:
                return {"status": "none", "written_off_v": self._fmt_v(Decimal("0"))}

            continuation_id = str(self._row_get(active, "id", ""))
            outstanding_v = self._money(self._row_get(active, "outstanding_v", "0"))
            if outstanding_v > 0:
                await tx.execute(
                    """
                    INSERT INTO continuation_accounting_events
                      (continuation_id, user_id, event_type, amount_v, reason, actor)
                    VALUES ($1, $2, 'principal_written_off', $3, $4, $5)
                    """,
                    continuation_id,
                    user_id,
                    outstanding_v,
                    str(reason)[:200],
                    str(actor)[:80],
                )
                emit_metric("continuation_writeoff_total", {"reason": str(reason)[:64] or "unspecified"})

            await tx.execute(
                """
                UPDATE user_continuation_credit
                SET outstanding_v = 0,
                    status = 'cancelled',
                    repaid_at = NULL
                WHERE id = $1
                """,
                continuation_id,
            )
            self._emit_outstanding_gauge(Decimal("0"))
            return {"status": "cancelled", "written_off_v": self._fmt_v(outstanding_v)}

    async def _apply_continuation_repayment(
        self,
        *,
        tx: Any,
        user_id: str,
        gross_v: Decimal,
        source_reference: str,
        reason: str = "topup_settlement",
    ) -> tuple[Decimal, Decimal]:
        active = await self._active_continuation_row(tx=tx, user_id=user_id)
        if not active:
            self._emit_outstanding_gauge(Decimal("0"))
            emit_metric("continuation_repayment_total", {"outcome": "no_outstanding"})
            return Decimal("0").quantize(V_SCALE), gross_v.quantize(V_SCALE)

        continuation_id = str(self._row_get(active, "id", ""))
        outstanding_v = self._money(self._row_get(active, "outstanding_v", "0"))
        repay_v = min(gross_v.quantize(V_SCALE), outstanding_v).quantize(V_SCALE)
        net_credit_v = (gross_v - repay_v).quantize(V_SCALE)
        if repay_v <= 0:
            self._emit_outstanding_gauge(outstanding_v)
            return Decimal("0").quantize(V_SCALE), net_credit_v

        new_outstanding = (outstanding_v - repay_v).quantize(V_SCALE)
        if new_outstanding <= Decimal("0"):
            new_outstanding = Decimal("0").quantize(V_SCALE)
            await tx.execute(
                """
                UPDATE user_continuation_credit
                SET outstanding_v = 0,
                    status = 'repaid',
                    repaid_at = now()
                WHERE id = $1
                """,
                continuation_id,
            )
            emit_metric("continuation_repayment_total", {"outcome": "full"})
        else:
            await tx.execute(
                """
                UPDATE user_continuation_credit
                SET outstanding_v = $2
                WHERE id = $1
                """,
                continuation_id,
                new_outstanding,
            )
            emit_metric("continuation_repayment_total", {"outcome": "partial"})

        await tx.execute(
            """
            INSERT INTO continuation_accounting_events
              (continuation_id, user_id, event_type, amount_v, reason, actor)
            VALUES ($1, $2, 'principal_repaid', $3, $4, 'system')
            """,
            continuation_id,
            user_id,
            repay_v,
            f"{reason}:{source_reference}"[:200],
        )
        self._emit_outstanding_gauge(new_outstanding)
        return repay_v, net_credit_v

    async def _fetch_pending_topup(self, *, tx: Any, user_id: str, mode: str) -> Any | None:
        row = await tx.fetchrow(
            """
            SELECT *
            FROM fiat_topups
            WHERE user_id = $1
              AND mode = $2
              AND status IN ('created', 'checkout_created')
            ORDER BY created_at DESC
            LIMIT 1
            FOR UPDATE
            """,
            user_id,
            mode,
        )
        if not row:
            return None
        updated = await self._mark_expired_if_needed(tx=tx, row=row)
        if self._is_pending_status(str(self._row_get(updated, "status", ""))):
            return updated
        return None

    def _suggest_payload(
        self,
        *,
        low_balance: bool,
        balance_v: Decimal,
        balance_usd_equiv: Decimal,
        floor_usd: Decimal,
        mode: str,
        topup: dict | None,
        suggested_topup_usd: Decimal | None,
    ) -> dict:
        return {
            "low_balance": bool(low_balance),
            "balance_v": self._fmt_v(balance_v),
            "balance_usd_equiv": self._fmt_usd(balance_usd_equiv),
            "low_balance_floor_usd": self._fmt_usd(floor_usd),
            "suggested_topup_usd": self._fmt_usd(suggested_topup_usd) if low_balance else None,
            "topup_id": topup.get("topup_id") if topup else None,
            "status": topup.get("status") if topup else None,
            "mode": str(topup.get("mode")) if topup and topup.get("mode") else mode,
            "checkout_url": topup.get("checkout_url") if topup else None,
            "qr_value": topup.get("qr_value") if topup else None,
            "payment_methods_display": self._payment_methods_display(),
        }

    async def create_topup_suggestion(self, *, user_id: str, suggested_topup_usd: Decimal | None = None) -> dict:
        balance_v = await self.wallet.get_balance(f"user:{user_id}")
        floor_usd = self._low_balance_floor_usd()
        v_per_usd = self._v_per_usd()
        if v_per_usd <= 0:
            raise BillingError("Invalid V_PER_USD configuration")
        balance_usd = (Decimal(str(balance_v)) / v_per_usd).quantize(USD_SCALE)
        mode = self._resolve_mode()
        if balance_usd > floor_usd:
            emit_metric("topup_suggest_suppressed_total", {"reason": "above_floor"})
            return self._suggest_payload(
                low_balance=False,
                balance_v=Decimal(str(balance_v)),
                balance_usd_equiv=balance_usd,
                floor_usd=floor_usd,
                mode=mode,
                topup=None,
                suggested_topup_usd=None,
            )

        topup: dict | None = None
        async with self.db.transaction() as tx:
            pending = await self._fetch_pending_topup(tx=tx, user_id=user_id, mode=mode)
            if pending:
                emit_metric("topup_suggest_suppressed_total", {"reason": "already_pending_topup"})
                topup = self._format_topup(pending)

        if topup is None:
            amount = self._usd(suggested_topup_usd or self._default_topup_usd())
            try:
                topup = await self.create_topup_checkout(
                    user_id=user_id,
                    amount_usd=amount,
                    idempotency_key=f"suggest:{mode}:{uuid.uuid4().hex[:16]}",
                )
                emit_metric("topup_suggest_shown_total", {"mode": mode, "created": "1"})
            except Exception:
                emit_metric("topup_suggest_suppressed_total", {"reason": "provider_unavailable"})
                raise
        else:
            emit_metric("topup_suggest_shown_total", {"mode": mode, "created": "0"})

        return self._suggest_payload(
            low_balance=True,
            balance_v=Decimal(str(balance_v)),
            balance_usd_equiv=balance_usd,
            floor_usd=floor_usd,
            mode=mode,
            topup=topup,
            suggested_topup_usd=self._usd(suggested_topup_usd or self._default_topup_usd()),
        )

    async def get_topup_internal(self, *, topup_id: str) -> dict:
        row = await self.db.fetchrow(
            """
            SELECT *
            FROM fiat_topups
            WHERE id = $1
            """,
            topup_id,
        )
        if not row:
            raise NotFoundError("Top-up not found")
        return self._format_topup(row)

    async def get_topup_status(self, *, user_id: str, topup_id: str) -> dict:
        async with self.db.transaction() as tx:
            row = await tx.fetchrow(
                """
                SELECT *
                FROM fiat_topups
                WHERE id = $1 AND user_id = $2
                FOR UPDATE
                """,
                topup_id,
                user_id,
            )
            if not row:
                raise NotFoundError("Top-up not found")
            row = await self._mark_expired_if_needed(tx=tx, row=row)
            return self._format_topup(row)

    async def list_topup_history(self, *, user_id: str, limit: int = 50) -> dict:
        safe_limit = max(1, min(int(limit), 200))
        rows = await self.db.fetch(
            """
            SELECT id, status, mode, amount_usd, v_credit, created_at, updated_at
            FROM fiat_topups
            WHERE user_id = $1
            ORDER BY COALESCE(updated_at, created_at) DESC
            LIMIT $2
            """,
            user_id,
            safe_limit,
        )
        v_per_usd = self._v_per_usd()
        usd_per_v = (Decimal("1") / v_per_usd) if v_per_usd > 0 else Decimal("0")
        entries = []
        for row in rows:
            ts = self._row_get(row, "updated_at") or self._row_get(row, "created_at")
            entries.append(
                {
                    "topup_id": str(self._row_get(row, "id", "")),
                    "time": ts.isoformat() if ts else None,
                    "type": "top_up",
                    "amount_usd": self._fmt_usd(self._row_get(row, "amount_usd")),
                    "amount_v": self._fmt_v(self._row_get(row, "v_credit")),
                    "amount_v_2dp": self._fmt_v_2(self._row_get(row, "v_credit")),
                    "status": str(self._row_get(row, "status", "")),
                    "mode": str(self._row_get(row, "mode", "")),
                }
            )
        return {
            "entries": entries,
            "conversion": {
                "v_per_usd": self._fmt_v(v_per_usd),
                "usd_per_v": self._fmt_usd(usd_per_v),
            },
        }

    async def list_activity_history(self, *, user_id: str, limit: int = 50) -> dict:
        safe_limit = max(1, min(int(limit), 200))
        source_limit = 300

        topups = await self.db.fetch(
            """
            SELECT id, status, amount_usd, v_credit, updated_at, created_at
            FROM fiat_topups
            WHERE user_id = $1
              AND status IN ('paid', 'failed')
            ORDER BY COALESCE(updated_at, created_at) DESC
            LIMIT $2
            """,
            user_id,
            source_limit,
        )

        human_rounds = await self.db.fetch(
            """
            SELECT
              r.id AS round_id,
              r.game_code,
              p.net_v,
              COALESCE(r.resolved_at, p.created_at, r.started_at) AS ts
            FROM human_casino_rounds r
            JOIN human_casino_payouts p
              ON p.round_id = r.id
            WHERE r.user_id = $1
            ORDER BY COALESCE(r.resolved_at, p.created_at, r.started_at) DESC
            LIMIT $2
            """,
            user_id,
            source_limit,
        )

        legacy_rounds = await self.db.fetch(
            """
            SELECT
              id,
              game_type,
              (COALESCE(payout, 0) - COALESCE(bet_amount, 0)) AS net_v,
              created_at,
              COALESCE(is_demo, FALSE) AS is_demo
            FROM game_history
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id,
            source_limit,
        )

        def _coerce_sort_ts(raw: Any) -> datetime:
            if isinstance(raw, datetime):
                return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
            if isinstance(raw, str):
                try:
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            return datetime.min.replace(tzinfo=timezone.utc)

        entries: list[dict[str, Any]] = []

        for row in topups:
            ts_raw = self._row_get(row, "updated_at") or self._row_get(row, "created_at")
            sort_ts = _coerce_sort_ts(ts_raw)
            entries.append(
                {
                    "_sort_ts": sort_ts,
                    "time": None if sort_ts == datetime.min.replace(tzinfo=timezone.utc) else sort_ts.isoformat(),
                    "type": "top_up",
                    "status": str(self._row_get(row, "status", "")),
                    "amount_usd": self._fmt_usd(self._row_get(row, "amount_usd")),
                    "amount_v": self._fmt_v(self._row_get(row, "v_credit")),
                    "amount_v_2dp": self._fmt_v_2(self._row_get(row, "v_credit")),
                    "reference_id": str(self._row_get(row, "id", "")),
                    "game_code": None,
                    "source": "fiat_topup",
                }
            )

        for row in human_rounds:
            net_v = Decimal(str(self._row_get(row, "net_v", "0")))
            if net_v == 0:
                continue
            ts_raw = self._row_get(row, "ts")
            sort_ts = _coerce_sort_ts(ts_raw)
            entries.append(
                {
                    "_sort_ts": sort_ts,
                    "time": None if sort_ts == datetime.min.replace(tzinfo=timezone.utc) else sort_ts.isoformat(),
                    "type": "gameplay",
                    "status": "won" if net_v > 0 else "lost",
                    "amount_usd": None,
                    "amount_v": self._fmt_v(net_v),
                    "amount_v_2dp": self._fmt_v_2(net_v),
                    "reference_id": str(self._row_get(row, "round_id", "")),
                    "game_code": str(self._row_get(row, "game_code", "")),
                    "source": "human_casino",
                }
            )

        for row in legacy_rounds:
            net_v = Decimal(str(self._row_get(row, "net_v", "0")))
            if net_v == 0:
                continue
            ts_raw = self._row_get(row, "created_at")
            sort_ts = _coerce_sort_ts(ts_raw)
            is_demo = bool(self._row_get(row, "is_demo", False))
            entries.append(
                {
                    "_sort_ts": sort_ts,
                    "time": None if sort_ts == datetime.min.replace(tzinfo=timezone.utc) else sort_ts.isoformat(),
                    "type": "gameplay",
                    "status": "won" if net_v > 0 else "lost",
                    "amount_usd": None,
                    "amount_v": self._fmt_v(net_v),
                    "amount_v_2dp": self._fmt_v_2(net_v),
                    "reference_id": str(self._row_get(row, "id", "")),
                    "game_code": str(self._row_get(row, "game_type", "")),
                    "source": "legacy_game_demo" if is_demo else "legacy_game",
                }
            )

        entries.sort(key=lambda e: e.get("_sort_ts", datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        for entry in entries:
            entry.pop("_sort_ts", None)

        v_per_usd = self._v_per_usd()
        usd_per_v = (Decimal("1") / v_per_usd) if v_per_usd > 0 else Decimal("0")
        return {
            "entries": entries[:safe_limit],
            "conversion": {
                "v_per_usd": self._fmt_v(v_per_usd),
                "usd_per_v": self._fmt_usd(usd_per_v),
            },
        }

    async def get_topup_qr_svg(self, *, user_id: str, topup_id: str) -> bytes:
        status = await self.get_topup_status(user_id=user_id, topup_id=topup_id)
        value = str(status.get("qr_value") or status.get("checkout_url") or "")
        if not value:
            raise BillingError("Checkout unavailable")
        emit_metric("topup_qr_generated_total", {"surface": "ui"})
        return self._render_qr_svg(value)

    async def render_checkout_qr_svg(self, *, value: str, surface: str = "ui") -> bytes:
        raw = str(value or "").strip()
        if not raw:
            raise BillingError("Checkout URL unavailable")
        if len(raw) > 4096:
            raise BillingError("Checkout URL too long")
        emit_metric("topup_qr_generated_total", {"surface": str(surface or "ui")})
        return self._render_qr_svg(raw)

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

    async def create_user_subscription_checkout(
        self,
        *,
        user_id: str,
        monthly_amount_usd: Decimal,
    ) -> dict:
        try:
            monthly_amount_usd = Decimal(str(monthly_amount_usd))
        except InvalidOperation as e:
            raise BillingError("Invalid monthly amount") from e

        min_usd = Decimal(os.getenv("USER_SUBSCRIPTION_MIN_USD", os.getenv("TOPUP_MIN_USD", "10")))
        max_usd = Decimal(os.getenv("USER_SUBSCRIPTION_MAX_USD", os.getenv("TOPUP_MAX_USD", "500")))
        if monthly_amount_usd < min_usd or monthly_amount_usd > max_usd:
            raise BillingError(f"Monthly amount must be between {min_usd} and {max_usd} USD")

        customer_id = await self._ensure_user_subscription_customer(user_id)
        checkout_attempt_id = str(uuid.uuid4())
        session = self.stripe_gateway.create_user_subscription_checkout(
            customer_id=customer_id,
            user_id=str(user_id),
            monthly_amount_usd=monthly_amount_usd,
            checkout_attempt_id=checkout_attempt_id,
        )

        await self.db.execute(
            """
            INSERT INTO user_subscriptions (
              user_id,
              stripe_customer_id,
              stripe_subscription_status,
              has_active_subscription,
              monthly_amount_usd,
              updated_at
            )
            VALUES ($1, $2, 'incomplete', FALSE, $3, now())
            ON CONFLICT (user_id)
            DO UPDATE SET
              stripe_customer_id = EXCLUDED.stripe_customer_id,
              monthly_amount_usd = EXCLUDED.monthly_amount_usd,
              updated_at = now()
            """,
            user_id,
            customer_id,
            monthly_amount_usd,
        )

        return {
            "user_id": str(user_id),
            "checkout_attempt_id": checkout_attempt_id,
            "checkout_session_id": str(session["id"]),
            "checkout_url": str(session["url"]),
            "monthly_amount_usd": self._fmt_usd(monthly_amount_usd),
        }

    async def get_user_subscription_status(self, *, user_id: str) -> dict:
        row = await self.db.fetchrow(
            """
            SELECT user_id, stripe_customer_id, stripe_subscription_id, stripe_price_id,
                   stripe_subscription_status, has_active_subscription, cancel_at_period_end,
                   current_period_end, monthly_amount_usd, updated_at
            FROM user_subscriptions
            WHERE user_id = $1
            """,
            user_id,
        )
        if not row:
            return {
                "user_id": str(user_id),
                "exists": False,
                "stripe_subscription_status": "inactive",
                "has_active_subscription": False,
            }
        return {
            "user_id": str(row["user_id"]),
            "exists": True,
            "stripe_customer_id": row["stripe_customer_id"],
            "stripe_subscription_id": row["stripe_subscription_id"],
            "stripe_price_id": row["stripe_price_id"],
            "stripe_subscription_status": row["stripe_subscription_status"] or "inactive",
            "has_active_subscription": bool(row["has_active_subscription"]),
            "cancel_at_period_end": bool(row["cancel_at_period_end"]),
            "current_period_end": row["current_period_end"].isoformat() if row["current_period_end"] else None,
            "monthly_amount_usd": self._fmt_usd(row["monthly_amount_usd"]),
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    async def create_user_subscription_portal(self, *, user_id: str, flow_type: str | None = None) -> dict:
        row = await self.db.fetchrow(
            """
            SELECT stripe_customer_id, stripe_subscription_id
            FROM user_subscriptions
            WHERE user_id = $1
            """,
            user_id,
        )
        if not row or not row["stripe_customer_id"]:
            raise BillingError("User has no Stripe customer configured")
        if flow_type == "subscription_cancel" and not row["stripe_subscription_id"]:
            raise BillingError("No active subscription to cancel")

        try:
            url = self.stripe_gateway.create_billing_portal(
                customer_id=str(row["stripe_customer_id"]),
                flow_type=flow_type,
                subscription_id=str(row["stripe_subscription_id"]) if row["stripe_subscription_id"] else None,
            )
        except ValueError as e:
            raise BillingError(str(e)) from e
        return {"url": url}

    async def create_user_payment_method_portal(self, *, user_id: str) -> dict:
        customer_id = await self._ensure_user_customer(user_id)
        try:
            url = self.stripe_gateway.create_billing_portal(
                customer_id=customer_id,
                flow_type="payment_method_update",
            )
        except ValueError as e:
            raise BillingError(str(e)) from e
        return {"url": url}

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
                return await self._handle_checkout_completed(
                    tx=tx,
                    session=obj,
                    event_created=self._provider_paid_at_from_event(event, fallback=self._utc_now()),
                )
            if event_type in {
                "customer.subscription.created",
                "customer.subscription.updated",
                "customer.subscription.deleted",
            }:
                return await self._handle_subscription_upsert(tx=tx, subscription=obj)
            if event_type == "invoice.paid":
                return await self._apply_invoice_credit_once(tx=tx, invoice=obj)
            if event_type == "invoice.payment_failed":
                return await self._mark_subscription_past_due(tx=tx, invoice=obj)
            return {"status": "ignored"}

    async def _handle_checkout_completed(self, *, tx: Any, session: dict, event_created: datetime | None = None) -> dict:
        if session.get("mode") == "payment":
            return await self._settle_topup_from_checkout(tx=tx, session=session, provider_paid_at=event_created)
        # Subscription-mode checkout doesn't settle wallet value in this phase.
        return {"status": "ignored"}

    async def _settle_topup_from_checkout(self, *, tx: Any, session: dict, provider_paid_at: datetime | None = None) -> dict:
        if session.get("payment_status") != "paid":
            return {"status": "not-paid"}

        session_id = str(session["id"])
        row = await tx.fetchrow(
            """
            SELECT *
            FROM fiat_topups
            WHERE stripe_checkout_session_id = $1
            FOR UPDATE
            """,
            session_id,
        )
        if not row:
            return {"status": "already-settled-or-missing"}
        row = await self._mark_expired_if_needed(tx=tx, row=row)
        return await self._settle_topup_paid(
            tx=tx,
            row=row,
            provider_ref=session.get("payment_intent"),
            settlement_surface="stripe",
            provider_paid_at=provider_paid_at or self._utc_now(),
        )

    async def complete_fake_topup(self, *, topup_id: str) -> dict:
        async with self.db.transaction() as tx:
            row = await tx.fetchrow(
                """
                SELECT *
                FROM fiat_topups
                WHERE id = $1
                FOR UPDATE
                """,
                topup_id,
            )
            if not row:
                raise NotFoundError("Top-up not found")
            row = await self._mark_expired_if_needed(tx=tx, row=row)
            return await self._settle_topup_paid(
                tx=tx,
                row=row,
                provider_ref=f"fake:{topup_id}",
                settlement_surface="simulated",
                provider_paid_at=self._utc_now(),
            )

    async def _settle_topup_paid(
        self,
        *,
        tx: Any,
        row: Any,
        provider_ref: str | None,
        settlement_surface: str,
        provider_paid_at: datetime,
    ) -> dict:
        topup_id = str(self._row_get(row, "id", ""))
        user_id = str(self._row_get(row, "user_id", ""))
        status_before = str(self._row_get(row, "status", ""))
        mode = str(self._row_get(row, "mode", self._resolve_mode()))

        if status_before == "paid":
            emit_metric("topup_settlement_idempotent_replay_total", {"mode": mode})
            return {"status": "paid", "topup_id": topup_id, "idempotent": True}
        if status_before == "manual_reconciliation_required":
            return {
                "status": "manual_reconciliation_required",
                "topup_id": topup_id,
                "idempotent": True,
            }
        if status_before == "expired":
            if mode == "simulated":
                raise BillingError("SIMULATED_TOPUP_EXPIRED_CANNOT_SETTLE")
            if settlement_surface != "stripe":
                raise BillingError("Only Stripe provider settlement can reconcile expired top-ups")
            expires_at = self._row_get(row, "expires_at")
            cutoff = provider_paid_at
            if expires_at is not None:
                ts = expires_at
                if getattr(ts, "tzinfo", None) is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                cutoff = ts + timedelta(seconds=self._late_settlement_window_sec())
            if provider_paid_at > cutoff:
                await self._mark_manual_reconciliation_required(
                    tx=tx,
                    topup_id=topup_id,
                    mode=mode,
                    reason="STRIPE_LATE_SETTLEMENT_WINDOW_EXCEEDED",
                )
                raise BillingError("STRIPE_LATE_SETTLEMENT_REQUIRES_MANUAL_REVIEW")
        if status_before not in {"created", "checkout_created", "expired"}:
            raise BillingError(f"TOPUP_STATUS_NOT_SETTLEABLE:{status_before}")

        if provider_ref:
            conflict = await tx.fetchrow(
                """
                SELECT id
                FROM fiat_topups
                WHERE stripe_payment_intent_id = $1
                  AND id <> $2
                LIMIT 1
                """,
                str(provider_ref),
                topup_id,
            )
            if conflict:
                raise BillingError("PROVIDER_REFERENCE_CONFLICT")

        updated = await tx.fetchrow(
            """
            UPDATE fiat_topups
            SET status = 'paid',
                stripe_payment_intent_id = COALESCE($2, stripe_payment_intent_id),
                manual_reconciliation_required = FALSE,
                manual_reconciliation_reason = NULL,
                manual_reconciliation_marked_at = NULL,
                updated_at = now()
            WHERE id = $1
            RETURNING id, user_id, v_credit, status
            """,
            topup_id,
            str(provider_ref) if provider_ref else None,
        )
        if not updated:
            raise BillingError("Unable to settle top-up")

        gross_v = Decimal(str(self._row_get(updated, "v_credit", "0"))).quantize(V_SCALE)
        _repaid_v, net_credit_v = await self._apply_continuation_repayment(
            tx=tx,
            user_id=user_id,
            gross_v=gross_v,
            source_reference=f"fiat_topup:{topup_id}",
        )
        if net_credit_v > 0:
            await self.wallet.fund_from_card(
                account_id=f"user:{user_id}",
                amount_v=net_credit_v,
                reference_id=f"fiat_topup:{topup_id}",
                tx=tx,
            )
        emit_metric("topup_status_transition_total", {"from": status_before, "to": "paid", "mode": mode})
        emit_metric("topup_webhook_settled_total", {"mode": mode, "status": "paid"})
        return {"status": "paid", "topup_id": topup_id, "idempotent": False}

    async def _handle_subscription_upsert(self, *, tx: Any, subscription: dict) -> dict:
        metadata = subscription.get("metadata") or {}
        purpose = str(metadata.get("purpose", "")).strip().lower()

        if purpose == "user_subscription" or metadata.get("user_id"):
            user_id = await self.resolve_user_id_from_subscription(subscription, tx=tx)
            await self.sync_user_subscription_from_subscription(
                tx=tx,
                user_id=user_id,
                subscription=subscription,
            )
            return {"status": "synced", "scope": "user", "user_id": user_id}

        try:
            org_id = await self.resolve_org_id_from_subscription(subscription, tx=tx)
        except Exception:
            return {"status": "ignored", "reason": "unmapped_subscription"}
        await self.sync_org_sponsorship_from_subscription(
            tx=tx,
            org_id=org_id,
            subscription=subscription,
        )
        return {"status": "synced", "scope": "org", "org_id": org_id}

    async def sync_user_subscription_from_subscription(
        self,
        *,
        tx: Any,
        user_id: str,
        subscription: dict,
    ) -> None:
        items = (subscription.get("items") or {}).get("data") or []
        item0 = items[0] if items else {}
        price = item0.get("price") if isinstance(item0, dict) else {}
        price_id = price.get("id") if isinstance(price, dict) else None
        unit_amount = price.get("unit_amount") if isinstance(price, dict) else None
        monthly_amount_usd = None
        if unit_amount is not None:
            monthly_amount_usd = (Decimal(str(unit_amount)) / Decimal("100")).quantize(USD_SCALE)
        period_end = subscription.get("current_period_end")

        await tx.execute(
            """
            INSERT INTO user_subscriptions (
              user_id,
              stripe_customer_id,
              stripe_subscription_id,
              stripe_price_id,
              stripe_subscription_status,
              has_active_subscription,
              cancel_at_period_end,
              current_period_end,
              monthly_amount_usd,
              updated_at
            )
            VALUES (
              $1, $2, $3, $4, $5, $6, $7,
              CASE WHEN $8::bigint IS NULL THEN NULL ELSE to_timestamp($8::bigint) END,
              $9,
              now()
            )
            ON CONFLICT (user_id)
            DO UPDATE SET
              stripe_customer_id = COALESCE(EXCLUDED.stripe_customer_id, user_subscriptions.stripe_customer_id),
              stripe_subscription_id = COALESCE(EXCLUDED.stripe_subscription_id, user_subscriptions.stripe_subscription_id),
              stripe_price_id = COALESCE(EXCLUDED.stripe_price_id, user_subscriptions.stripe_price_id),
              stripe_subscription_status = EXCLUDED.stripe_subscription_status,
              has_active_subscription = EXCLUDED.has_active_subscription,
              cancel_at_period_end = EXCLUDED.cancel_at_period_end,
              current_period_end = EXCLUDED.current_period_end,
              monthly_amount_usd = COALESCE(EXCLUDED.monthly_amount_usd, user_subscriptions.monthly_amount_usd),
              updated_at = now()
            """,
            user_id,
            subscription.get("customer"),
            subscription.get("id"),
            price_id,
            subscription.get("status", "inactive"),
            self.compute_has_active_subscription(subscription),
            bool(subscription.get("cancel_at_period_end", False)),
            int(period_end) if period_end else None,
            monthly_amount_usd,
        )

    async def _apply_invoice_credit_once(self, *, tx: Any, invoice: dict) -> dict:
        user_res = await self._apply_user_subscription_credit_once(tx=tx, invoice=invoice)
        if str(user_res.get("status")) not in {"user-not-found", "ignored"}:
            return user_res
        return await self._apply_org_budget_credit_once(tx=tx, invoice=invoice)

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

    async def _apply_user_subscription_credit_once(self, *, tx: Any, invoice: dict) -> dict:
        subscription_id = invoice.get("subscription")
        invoice_id = invoice.get("id")
        if not subscription_id or not invoice_id:
            return {"status": "ignored"}

        row = await tx.fetchrow(
            """
            SELECT user_id, stripe_customer_id
            FROM user_subscriptions
            WHERE stripe_subscription_id = $1
            FOR UPDATE
            """,
            subscription_id,
        )
        if not row:
            customer_id = invoice.get("customer")
            if customer_id:
                row = await tx.fetchrow(
                    """
                    SELECT user_id, stripe_customer_id
                    FROM user_subscriptions
                    WHERE stripe_customer_id = $1
                    FOR UPDATE
                    """,
                    str(customer_id),
                )
                if row:
                    # Stripe events are not ordered; persist subscription id as soon as we can map it.
                    await tx.execute(
                        """
                        UPDATE user_subscriptions
                        SET stripe_subscription_id = COALESCE(stripe_subscription_id, $2),
                            updated_at = now()
                        WHERE user_id = $1
                        """,
                        str(row["user_id"]),
                        str(subscription_id),
                    )
        if not row:
            return {"status": "user-not-found"}

        amount_paid = (Decimal(str(invoice.get("amount_paid", 0))) / Decimal("100")).quantize(USD_SCALE)
        if amount_paid <= 0:
            return {"status": "ignored"}

        user_id = str(row["user_id"])
        v_per_usd = self._v_per_usd()
        if v_per_usd <= 0:
            raise BillingError("Invalid V_PER_USD configuration")
        v_credit = (amount_paid * v_per_usd).quantize(V_SCALE)
        idem_key = f"stripe_user_invoice:{invoice_id}"
        payload_hash = self.canonical_payload_hash(
            {
                "subscription_id": str(subscription_id),
                "invoice_id": str(invoice_id),
                "amount_paid": str(amount_paid),
                "currency": str(invoice.get("currency", "usd")),
            }
        )

        existing = await tx.fetchrow(
            """
            SELECT id, status
            FROM fiat_topups
            WHERE user_id = $1 AND idempotency_key = $2
            FOR UPDATE
            """,
            user_id,
            idem_key,
        )
        if existing and str(existing["status"]) == "paid":
            return {"status": "already_credited", "topup_id": str(existing["id"])}

        topup_id = str(existing["id"]) if existing else str(uuid.uuid4())
        payment_intent = invoice.get("payment_intent")
        if not existing:
            await tx.execute(
                """
                INSERT INTO fiat_topups
                  (id, user_id, amount_usd, v_credit, currency, status, idempotency_key, idempotency_payload_hash,
                   stripe_customer_id, stripe_payment_intent_id, mode, expires_at, updated_at)
                VALUES ($1, $2, $3, $4, 'usd', 'paid', $5, $6, $7, $8, 'stripe', NULL, now())
                """,
                topup_id,
                user_id,
                amount_paid,
                v_credit,
                idem_key,
                payload_hash,
                row["stripe_customer_id"],
                str(payment_intent) if payment_intent else None,
            )
        else:
            await tx.execute(
                """
                UPDATE fiat_topups
                SET status = 'paid',
                    amount_usd = $2,
                    v_credit = $3,
                    idempotency_payload_hash = $4,
                    stripe_customer_id = COALESCE($5, stripe_customer_id),
                    stripe_payment_intent_id = COALESCE($6, stripe_payment_intent_id),
                    mode = 'stripe',
                    updated_at = now()
                WHERE id = $1
                """,
                topup_id,
                amount_paid,
                v_credit,
                payload_hash,
                row["stripe_customer_id"],
                str(payment_intent) if payment_intent else None,
            )

        _repaid_v, net_credit_v = await self._apply_continuation_repayment(
            tx=tx,
            user_id=user_id,
            gross_v=v_credit,
            source_reference=f"fiat_topup:{topup_id}",
            reason="invoice_settlement",
        )
        if net_credit_v > 0:
            await self.wallet.fund_from_card(
                account_id=f"user:{user_id}",
                amount_v=net_credit_v,
                reference_id=f"fiat_topup:{topup_id}",
                tx=tx,
            )
        emit_metric("user_subscription_invoice_credited_total", {"status": "paid"})
        return {"status": "credited", "topup_id": topup_id, "user_id": user_id}

    async def _mark_subscription_past_due(self, *, tx: Any, invoice: dict) -> dict:
        user = await self._mark_user_subscription_past_due(tx=tx, invoice=invoice)
        if str(user.get("status")) == "past_due":
            return user
        return await self._mark_org_subscription_past_due(tx=tx, invoice=invoice)

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

    async def _mark_user_subscription_past_due(self, *, tx: Any, invoice: dict) -> dict:
        subscription_id = invoice.get("subscription")
        if not subscription_id:
            return {"status": "ignored"}
        row = await tx.fetchrow(
            """
            UPDATE user_subscriptions
            SET stripe_subscription_status = 'past_due',
                has_active_subscription = FALSE,
                updated_at = now()
            WHERE stripe_subscription_id = $1
            RETURNING user_id
            """,
            subscription_id,
        )
        if not row:
            return {"status": "user-not-found"}
        return {"status": "past_due", "scope": "user", "user_id": str(row["user_id"])}

    @staticmethod
    def _format_topup(row: Any) -> dict:
        checkout_url = BillingService._row_get(row, "stripe_checkout_url")
        mode = str(BillingService._row_get(row, "mode", "stripe"))
        status = str(BillingService._row_get(row, "status", "created"))
        return {
            "topup_id": str(BillingService._row_get(row, "id", "")),
            "status": status,
            "mode": mode,
            "amount_usd": BillingService._fmt_usd(BillingService._row_get(row, "amount_usd")),
            "v_credit": BillingService._fmt_v(BillingService._row_get(row, "v_credit")),
            "checkout_session_id": BillingService._row_get(row, "stripe_checkout_session_id"),
            "checkout_url": checkout_url,
            "qr_value": checkout_url,
            "payment_intent_id": BillingService._row_get(row, "stripe_payment_intent_id"),
            "failure_reason": BillingService._row_get(row, "failure_reason"),
            "expires_at": (
                BillingService._row_get(row, "expires_at").isoformat()
                if BillingService._row_get(row, "expires_at")
                else None
            ),
            "manual_reconciliation_required": bool(
                BillingService._row_get(row, "manual_reconciliation_required", False)
                or status == "manual_reconciliation_required"
            ),
            "manual_reconciliation_reason": BillingService._row_get(row, "manual_reconciliation_reason"),
            "updated_at": (
                BillingService._row_get(row, "updated_at").isoformat()
                if BillingService._row_get(row, "updated_at")
                else None
            ),
        }
