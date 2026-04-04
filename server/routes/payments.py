"""Billing routes — card topups, org subscription checkout, and Stripe webhooks."""

from __future__ import annotations

import os
import uuid
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from openvegas.payments.service import BillingError, IdempotencyConflict, NotFoundError
from server.middleware.auth import get_current_user
from server.services.dependencies import get_billing_service, get_db

router = APIRouter(prefix="/billing")


class TopupCheckoutRequest(BaseModel):
    amount_usd: str
    idempotency_key: str | None = None


class TopupSavedChargeRequest(BaseModel):
    amount_usd: str
    idempotency_key: str | None = None


class TopupPreviewRequest(BaseModel):
    amount_usd: str


class UserSubscriptionCheckoutRequest(BaseModel):
    monthly_amount_usd: str
    idempotency_key: str | None = None


class ContinuationClaimRequest(BaseModel):
    idempotency_key: str | None = None


class PortalSessionRequest(BaseModel):
    flow_type: Literal["subscription_cancel", "payment_method_update"] | None = None


class QRRenderRequest(BaseModel):
    value: str


class TopupSuggestRequest(BaseModel):
    suggested_topup_usd: str | None = None


class FakeCompleteRequest(BaseModel):
    topup_id: str


def _require_valid_topup_id(raw: str) -> str:
    try:
        return str(uuid.UUID(str(raw)))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid topup_id")


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


@router.post("/topups/preview")
async def preview_topup_checkout(req: TopupPreviewRequest, user: dict = Depends(get_current_user)):
    svc = get_billing_service()
    try:
        amount = Decimal(req.amount_usd)
    except (InvalidOperation, TypeError):
        raise HTTPException(status_code=400, detail="Invalid amount_usd")
    try:
        return await svc.preview_topup_checkout(user_id=user["user_id"], amount_usd=amount)
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/topups/saved-payment-method")
async def get_saved_payment_method(user: dict = Depends(get_current_user)):
    svc = get_billing_service()
    try:
        return await svc.get_saved_payment_method_status(user_id=user["user_id"])
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/topups/charge-saved")
async def charge_saved_topup(req: TopupSavedChargeRequest, user: dict = Depends(get_current_user)):
    svc = get_billing_service()
    key = req.idempotency_key or f"saved-{uuid.uuid4().hex[:12]}"
    try:
        amount = Decimal(req.amount_usd)
    except (InvalidOperation, TypeError):
        raise HTTPException(status_code=400, detail="Invalid amount_usd")
    try:
        return await svc.charge_saved_topup(
            user_id=user["user_id"],
            amount_usd=amount,
            idempotency_key=key,
        )
    except IdempotencyConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/continuation/status")
async def continuation_status(user: dict = Depends(get_current_user)):
    svc = get_billing_service()
    try:
        return await svc.get_continuation_status(user_id=user["user_id"])
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/continuation/claim")
async def continuation_claim(
    request: Request,
    req: ContinuationClaimRequest | None = None,
    user: dict = Depends(get_current_user),
):
    svc = get_billing_service()
    header_key = str(request.headers.get("Idempotency-Key", "")).strip()
    body_key = str(req.idempotency_key).strip() if req and req.idempotency_key else ""
    idem_key = header_key or body_key or None
    try:
        return await svc.claim_continuation(user_id=user["user_id"], idempotency_key=idem_key)
    except IdempotencyConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/topups/suggest")
async def suggest_topup(req: TopupSuggestRequest | None = None, user: dict = Depends(get_current_user)):
    svc = get_billing_service()
    try:
        suggested: Decimal | None = None
        if req and req.suggested_topup_usd is not None:
            try:
                suggested = Decimal(req.suggested_topup_usd)
            except (InvalidOperation, TypeError):
                raise HTTPException(status_code=400, detail="Invalid suggested_topup_usd")
        return await svc.create_topup_suggestion(
            user_id=user["user_id"],
            suggested_topup_usd=suggested,
        )
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/topups")
async def list_topups(user: dict = Depends(get_current_user), limit: int = 50):
    svc = get_billing_service()
    try:
        return await svc.list_topup_history(user_id=user["user_id"], limit=limit)
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/activity")
async def list_activity(response: Response, user: dict = Depends(get_current_user), limit: int = 50):
    svc = get_billing_service()
    try:
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return await svc.list_activity_history(user_id=user["user_id"], limit=limit)
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/topups/{topup_id}")
async def get_topup_status(topup_id: str, user: dict = Depends(get_current_user)):
    svc = get_billing_service()
    topup_id = _require_valid_topup_id(topup_id)
    try:
        return await svc.get_topup_status(user_id=user["user_id"], topup_id=topup_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/topups/{topup_id}/qr.svg")
async def get_topup_qr_svg(topup_id: str, user: dict = Depends(get_current_user)):
    svc = get_billing_service()
    topup_id = _require_valid_topup_id(topup_id)
    try:
        payload = await svc.get_topup_qr_svg(user_id=user["user_id"], topup_id=topup_id)
        return Response(
            content=payload,
            media_type="image/svg+xml",
            headers={"Cache-Control": "private, no-store"},
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BillingError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/qr.svg")
async def render_checkout_qr_svg(req: QRRenderRequest, user: dict = Depends(get_current_user)):
    svc = get_billing_service()
    try:
        payload = await svc.render_checkout_qr_svg(value=req.value, surface="ui")
        return Response(
            content=payload,
            media_type="image/svg+xml",
            headers={"Cache-Control": "private, no-store"},
        )
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/topups/{topup_id}/verify")
async def verify_topup_status(topup_id: str, user: dict = Depends(get_current_user)):
    """Informational endpoint only; never settles funds."""
    return await get_topup_status(topup_id, user)


@router.post("/webhook/fake/complete")
async def fake_complete(req: FakeCompleteRequest, request: Request):
    if os.getenv("OPENVEGAS_BILLING_FAKE_WEBHOOK_ENABLED", "0") != "1":
        raise HTTPException(status_code=404, detail="Not found")
    if os.getenv("OPENVEGAS_BILLING_PROVIDER", "hybrid").strip().lower() == "stripe":
        raise HTTPException(status_code=403, detail="Fake completion disabled in stripe mode")

    expected_secret = os.getenv("OPENVEGAS_FAKE_WEBHOOK_SECRET", "").strip()
    if expected_secret:
        actual_secret = request.headers.get("X-OpenVegas-Fake-Webhook-Secret", "").strip()
        if actual_secret != expected_secret:
            raise HTTPException(status_code=403, detail="Invalid fake webhook secret")

    svc = get_billing_service()
    topup_id = _require_valid_topup_id(req.topup_id)
    try:
        topup = await svc.get_topup_internal(topup_id=topup_id)
        topup_mode = ""
        if isinstance(topup, dict):
            topup_mode = str(topup.get("mode") or "")
        else:
            topup_mode = str(getattr(topup, "mode", "") or "")
        if topup_mode != "simulated":
            raise HTTPException(status_code=409, detail="Top-up is not simulated")
        return await svc.complete_fake_topup(topup_id=topup_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BillingError as e:
        raise HTTPException(status_code=409, detail=str(e))


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


@router.post("/subscription/checkout")
async def create_user_subscription_checkout(
    req: UserSubscriptionCheckoutRequest,
    user: dict = Depends(get_current_user),
):
    svc = get_billing_service()
    try:
        amount = Decimal(req.monthly_amount_usd)
    except (InvalidOperation, TypeError):
        raise HTTPException(status_code=400, detail="Invalid monthly_amount_usd")

    try:
        return await svc.create_user_subscription_checkout(
            user_id=user["user_id"],
            monthly_amount_usd=amount,
        )
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/subscription/status")
async def get_user_subscription_status(user: dict = Depends(get_current_user)):
    svc = get_billing_service()
    try:
        return await svc.get_user_subscription_status(user_id=user["user_id"])
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/subscription/portal-session")
async def create_user_subscription_portal_session(
    req: PortalSessionRequest | None = None,
    user: dict = Depends(get_current_user),
):
    svc = get_billing_service()
    try:
        return await svc.create_user_subscription_portal(
            user_id=user["user_id"],
            flow_type=(req.flow_type if req else None),
        )
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/topups/payment-method-portal-session")
async def create_topup_payment_method_portal_session(user: dict = Depends(get_current_user)):
    svc = get_billing_service()
    try:
        return await svc.create_user_payment_method_portal(user_id=user["user_id"])
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
