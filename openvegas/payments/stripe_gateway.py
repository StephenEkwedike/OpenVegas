"""Stripe API adapter with version-tolerant request options handling."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any


class StripeGateway:
    """Thin wrapper around stripe-python primitives used by billing service."""

    mode = "stripe"

    def __init__(self, stripe_mod: Any | None = None):
        if stripe_mod is None:
            try:
                import stripe as stripe_mod  # type: ignore
            except Exception as e:  # pragma: no cover - exercised in runtime
                raise RuntimeError(
                    "stripe-python is required for billing routes. Install pinned version first."
                ) from e

        self.stripe = stripe_mod
        key = os.getenv("STRIPE_SECRET_KEY", "").strip()
        if not key:
            raise RuntimeError("STRIPE_SECRET_KEY is required for billing operations")
        self.stripe.api_key = key

    def _session_create(self, *, idempotency_key: str, **params) -> dict:
        """Create checkout session with SDK-compatible idempotency options."""
        try:
            # Modern stripe-python supports request options directly as kwargs.
            return self.stripe.checkout.Session.create(
                idempotency_key=idempotency_key,
                **params,
            )
        except TypeError:
            # Older/newer variants may expect request options under `options`.
            return self.stripe.checkout.Session.create(
                **params,
                options={"idempotency_key": idempotency_key},
            )

    def _payment_intent_create(self, *, idempotency_key: str, **params) -> dict:
        """Create payment intent with SDK-compatible idempotency options."""
        try:
            return self.stripe.PaymentIntent.create(
                idempotency_key=idempotency_key,
                **params,
            )
        except TypeError:
            return self.stripe.PaymentIntent.create(
                **params,
                options={"idempotency_key": idempotency_key},
            )

    def create_customer(
        self,
        *,
        email: str | None,
        name: str | None,
        metadata: dict[str, str] | None = None,
    ) -> dict:
        payload: dict[str, Any] = {}
        if email:
            payload["email"] = email
        if name:
            payload["name"] = name
        if metadata:
            payload["metadata"] = metadata
        return self.stripe.Customer.create(**payload)

    def create_topup_checkout(self, *, customer_id: str, amount_usd: Decimal, topup_id: str) -> dict:
        cents = int((amount_usd * Decimal("100")).quantize(Decimal("1")))
        return self._session_create(
            idempotency_key=f"topup-checkout:{topup_id}",
            mode="payment",
            customer=customer_id,
            client_reference_id=topup_id,
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "OpenVegas $V Top-up"},
                    "unit_amount": cents,
                },
                "quantity": 1,
            }],
            success_url=os.environ["CHECKOUT_SUCCESS_URL"],
            cancel_url=os.environ["CHECKOUT_CANCEL_URL"],
            metadata={"topup_id": topup_id},
            payment_intent_data={"setup_future_usage": "off_session"},
        )

    def retrieve_customer(self, *, customer_id: str) -> dict:
        return self.stripe.Customer.retrieve(customer_id)

    def list_customer_card_payment_methods(self, *, customer_id: str, limit: int = 5) -> dict:
        return self.stripe.PaymentMethod.list(
            customer=customer_id,
            type="card",
            limit=max(1, min(int(limit), 20)),
        )

    def retrieve_payment_method(self, *, payment_method_id: str) -> dict:
        return self.stripe.PaymentMethod.retrieve(payment_method_id)

    def create_saved_card_topup_payment_intent(
        self,
        *,
        customer_id: str,
        payment_method_id: str,
        amount_usd: Decimal,
        topup_id: str,
    ) -> dict:
        cents = int((amount_usd * Decimal("100")).quantize(Decimal("1")))
        return self._payment_intent_create(
            idempotency_key=f"topup-saved-charge:{topup_id}",
            amount=cents,
            currency="usd",
            customer=customer_id,
            payment_method=payment_method_id,
            off_session=True,
            confirm=True,
            metadata={"topup_id": topup_id},
        )

    def create_org_subscription_checkout(
        self,
        *,
        customer_id: str,
        price_id: str,
        org_id: str,
        checkout_attempt_id: str,
    ) -> dict:
        return self._session_create(
            idempotency_key=f"org-sub-checkout:{org_id}:{checkout_attempt_id}",
            mode="subscription",
            customer=customer_id,
            client_reference_id=org_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=os.environ["CHECKOUT_SUCCESS_URL"],
            cancel_url=os.environ["CHECKOUT_CANCEL_URL"],
            metadata={"org_id": org_id, "purpose": "org_sponsorship"},
            subscription_data={
                "metadata": {"org_id": org_id, "purpose": "org_sponsorship"},
            },  # persisted onto Subscription for customer.subscription.* webhooks
        )

    def create_user_subscription_checkout(
        self,
        *,
        customer_id: str,
        user_id: str,
        monthly_amount_usd: Decimal,
        checkout_attempt_id: str,
    ) -> dict:
        cents = int((monthly_amount_usd * Decimal("100")).quantize(Decimal("1")))
        return self._session_create(
            idempotency_key=f"user-sub-checkout:{user_id}:{checkout_attempt_id}",
            mode="subscription",
            customer=customer_id,
            client_reference_id=user_id,
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "OpenVegas Monthly Auto Top-up"},
                    "recurring": {"interval": "month", "interval_count": 1},
                    "unit_amount": cents,
                },
                "quantity": 1,
            }],
            success_url=os.environ["CHECKOUT_SUCCESS_URL"],
            cancel_url=os.environ["CHECKOUT_CANCEL_URL"],
            metadata={"user_id": user_id, "purpose": "user_subscription"},
            subscription_data={
                "metadata": {"user_id": user_id, "purpose": "user_subscription"},
            },
        )

    def create_billing_portal(self, *, customer_id: str, flow_type: str | None = None, subscription_id: str | None = None) -> str:
        payload: dict[str, Any] = {
            "customer": customer_id,
            "return_url": os.environ["APP_BASE_URL"] + "/ui",
        }
        if flow_type == "subscription_cancel":
            if not subscription_id:
                raise ValueError("subscription_id required for subscription_cancel flow")
            payload["flow_data"] = {
                "type": "subscription_cancel",
                "subscription_cancel": {"subscription": subscription_id},
                "after_completion": {
                    "type": "redirect",
                    "redirect": {"return_url": os.environ["APP_BASE_URL"] + "/ui?billing=done"},
                },
            }
        elif flow_type == "payment_method_update":
            payload["flow_data"] = {"type": "payment_method_update"}
        portal = self.stripe.billing_portal.Session.create(**payload)
        return portal["url"]

    def construct_event(self, raw_body: bytes, signature: str) -> dict:
        return self.stripe.Webhook.construct_event(
            payload=raw_body,
            sig_header=signature,
            secret=os.environ["STRIPE_WEBHOOK_SECRET"],
        )
