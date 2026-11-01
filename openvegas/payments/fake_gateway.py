"""Simulated billing gateway used for local/dev top-up flows."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any


class FakeGateway:
    """Drop-in gateway with Stripe-like method names and simulated outputs."""

    mode = "simulated"

    def create_customer(
        self,
        *,
        email: str | None,
        name: str | None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        _ = (email, name, metadata)
        return {"id": f"fake_cus_{uuid.uuid4().hex[:16]}"}

    def create_topup_checkout(self, *, customer_id: str, amount_usd: Decimal, topup_id: str) -> dict[str, Any]:
        _ = (customer_id, amount_usd)
        return {
            "id": f"fake_cs_{uuid.uuid4().hex[:16]}",
            "url": f"https://checkout.openvegas.local/topup/{topup_id}",
            "payment_intent": f"fake_pi_{uuid.uuid4().hex[:16]}",
        }

    def create_org_subscription_checkout(
        self,
        *,
        customer_id: str,
        price_id: str,
        org_id: str,
        checkout_attempt_id: str,
    ) -> dict[str, Any]:
        _ = (customer_id, price_id, org_id, checkout_attempt_id)
        raise RuntimeError("Org subscription checkout is unavailable in simulated billing mode")

    def create_billing_portal(self, *, customer_id: str, flow_type: str | None = None, subscription_id: str | None = None) -> str:
        _ = (customer_id, flow_type, subscription_id)
        raise RuntimeError("Billing portal is unavailable in simulated billing mode")

    def construct_event(self, raw_body: bytes, signature: str) -> dict[str, Any]:
        _ = (raw_body, signature)
        raise RuntimeError("Stripe webhook events are unavailable in simulated billing mode")
