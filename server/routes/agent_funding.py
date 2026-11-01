"""Human owner controls for agent payments; never exposed to agent bearer tokens."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from openvegas.payments.agent_funding import AgentFunding, public_order, require_enabled
from openvegas.payments.service import BillingError, IdempotencyConflict
from server.middleware.auth import get_current_user
from server.services.dependencies import get_billing_service, get_db, get_wallet

router = APIRouter(prefix="/v1/agent/organizations", tags=["agent"])
User = Annotated[dict, Depends(get_current_user)]


class Checkout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_usd: str
    idempotency_key: str = Field(min_length=1, max_length=120)
    save_card: StrictBool = False


class AutoTopup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    threshold_v: str = "100"
    amount_usd: str = "10"
    monthly_cap_usd: str = "50"
    consent: StrictBool = False


class ReviewPayment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cancel: StrictBool = False


def service(*, payments=False):
    gateway = get_billing_service().stripe_gateway if payments else None
    return AgentFunding(get_db(), get_wallet(), gateway)


@router.get("/{org_id}/agents/{agent_id}/funding")
async def funding(org_id: UUID, agent_id: UUID, user: User, response: Response):
    response.headers["Cache-Control"] = "no-store"
    svc = service()
    try:
        account = await svc.authorize(
            svc.db, user["user_id"], str(org_id), str(agent_id), active=False
        )
    except BillingError as exc:
        raise HTTPException(403, str(exc)) from None
    enabled = False
    reason = "Agent payments are disabled on this server."
    if account["is_demo"]:
        reason = "Demo workspace: no card payments or automatic charges."
    elif account["status"] != "active":
        reason = "This agent needs an account/payment review before funding."
    else:
        try:
            require_enabled(service(payments=True).gateway)
            enabled = True
            reason = "Fund only the selected agent. No payment is made by viewing this page."
        except (BillingError, RuntimeError):
            pass
    orders = await svc.db.fetch(
        "SELECT * FROM agent_funding_orders WHERE org_id=$1 AND agent_id=$2 ORDER BY created_at DESC LIMIT 50",
        str(org_id),
        str(agent_id),
    )
    p = await svc.db.fetchrow(
        "SELECT enabled,threshold_v,amount_usd,monthly_cap_usd,pause_reason FROM agent_auto_topups WHERE agent_id=$1",
        str(agent_id),
    )
    return {
        "enabled": enabled,
        "reason": reason,
        "orders": [public_order(r) for r in orders],
        "auto_topup": {
            k: str(v) if k in {"threshold_v", "amount_usd", "monthly_cap_usd"} else v
            for k, v in dict(p).items()
        }
        if p
        else {"enabled": False},
    }


@router.post("/{org_id}/agents/{agent_id}/funding/checkout")
async def checkout(org_id: UUID, agent_id: UUID, body: Checkout, user: User, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service(payments=True).checkout(
            user_id=user["user_id"], org_id=str(org_id), agent_id=str(agent_id), **body.model_dump()
        )
    except IdempotencyConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except BillingError as exc:
        raise HTTPException(400, str(exc)) from None
    except RuntimeError:
        raise HTTPException(503, "Agent payment provider is not configured") from None


@router.put("/{org_id}/agents/{agent_id}/funding/auto")
async def auto(org_id: UUID, agent_id: UUID, body: AutoTopup, user: User, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service(payments=body.enabled).configure_auto(
            user_id=user["user_id"], org_id=str(org_id), agent_id=str(agent_id), **body.model_dump()
        )
    except BillingError as exc:
        raise HTTPException(400, str(exc)) from None
    except RuntimeError:
        raise HTTPException(503, "Agent payment provider is not configured") from None


@router.post("/{org_id}/agents/{agent_id}/funding/{order_id}/review")
async def review(
    org_id: UUID,
    agent_id: UUID,
    order_id: UUID,
    body: ReviewPayment,
    user: User,
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service(payments=True).review_payment(
            user_id=user["user_id"],
            org_id=str(org_id),
            agent_id=str(agent_id),
            order_id=str(order_id),
            cancel=body.cancel,
        )
    except BillingError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:  # noqa: BLE001 - never expose provider errors or create a replacement charge.
        raise HTTPException(
            503, "Payment status unavailable. No replacement charge was requested."
        ) from None
