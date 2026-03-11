"""Stripe API adapter with version-tolerant request options handling."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any


class StripeGateway:
    """Thin wrapper around stripe-python primitives used by billing service."""

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

