"""Billing routes — card topups, org subscription checkout, and Stripe webhooks."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from openvegas.payments.service import BillingError, IdempotencyConflict, NotFoundError
from server.middleware.auth import get_current_user
from server.services.dependencies import get_billing_service, get_db

router = APIRouter(prefix="/billing")


class TopupCheckoutRequest(BaseModel):
    amount_usd: str
    idempotency_key: str | None = None


class PortalSessionRequest(BaseModel):
    flow_type: Literal["subscription_cancel", "payment_method_update"] | None = None


async def require_org_admin(org_id: str, user_id: str) -> None:
    db = get_db()
    row = await db.fetchrow(
        """
        SELECT role
        FROM org_members
        WHERE org_id = $1
          AND user_id = $2
          AND status = 'active'
        """,
        org_id,
        user_id,
    )
    if not row or str(row["role"]) not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Org owner/admin access required")


@router.post("/topups/checkout")
async def create_topup_checkout(req: TopupCheckoutRequest, user: dict = Depends(get_current_user)):
    svc = get_billing_service()
    key = req.idempotency_key or f"cli-{uuid.uuid4().hex[:12]}"
    try:
        amount = Decimal(req.amount_usd)
    except (InvalidOperation, TypeError):
        raise HTTPException(status_code=400, detail="Invalid amount_usd")

    try:
        return await svc.create_topup_checkout(
            user_id=user["user_id"],
            amount_usd=amount,
            idempotency_key=key,
        )
    except IdempotencyConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/topups/{topup_id}")
async def get_topup_status(topup_id: str, user: dict = Depends(get_current_user)):
    svc = get_billing_service()
    try:
        return await svc.get_topup_status(user_id=user["user_id"], topup_id=topup_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/topups/{topup_id}/verify")
async def verify_topup_status(topup_id: str, user: dict = Depends(get_current_user)):
    """Informational endpoint only; never settles funds."""
    return await get_topup_status(topup_id, user)


@router.post("/orgs/{org_id}/subscription/checkout")
async def create_org_subscription_checkout(org_id: str, user: dict = Depends(get_current_user)):
    await require_org_admin(org_id, user["user_id"])
    svc = get_billing_service()
    try:
        return await svc.create_org_subscription_checkout(org_id=org_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orgs/{org_id}/subscription/status")
async def get_org_subscription_status(org_id: str, user: dict = Depends(get_current_user)):
    await require_org_admin(org_id, user["user_id"])
    svc = get_billing_service()
    try:
        return await svc.get_org_subscription_status(org_id=org_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/orgs/{org_id}/portal-session")
async def create_org_portal_session(
    org_id: str,
    req: PortalSessionRequest | None = None,
    user: dict = Depends(get_current_user),
):
    await require_org_admin(org_id, user["user_id"])
    svc = get_billing_service()
    try:
        return await svc.create_org_billing_portal(
            org_id=org_id,
            flow_type=(req.flow_type if req else None),
        )
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    sig = request.headers.get("stripe-signature", "")
    raw = await request.body()
    svc = get_billing_service()
    try:
        return await svc.handle_webhook(raw_body=raw, signature=sig)
    except Exception as e:
        # Avoid leaking internal details to webhook caller
        raise HTTPException(status_code=400, detail=f"Webhook error: {e}")
