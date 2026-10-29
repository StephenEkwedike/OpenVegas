"""Thin native-host account bridge; no model orchestration or reward minting."""
from __future__ import annotations

import os
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from server.middleware.auth import get_current_user
from server.routes.ui_auth import _assert_same_origin
from server.services.dependencies import get_redis, get_store_service, get_wallet
from server.services.integration_auth import IntegrationAuth, digest


async def enabled(response: Response):
    response.headers["Cache-Control"] = "no-store"
    if os.getenv("OPENVEGAS_NATIVE_INTEGRATIONS_ENABLED", "0").lower() not in {"1", "true"}:
        raise HTTPException(503, "Native integration setup is not enabled on this backend.")


router = APIRouter(prefix="/integrations", tags=["integrations"], dependencies=[Depends(enabled)])


def service():
    return IntegrationAuth(get_redis())


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Start(Input):
    code_challenge: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$")


class Approval(Input):
    user_code: str = Field(min_length=12, max_length=12, pattern=r"^[A-F0-9]{12}$")
    approved: StrictBool


class Exchange(Input):
    device_code: str = Field(min_length=40, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    verifier: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")


class Host(Input):
    host: Literal["claude", "codex"]


class Equip(Input):
    slot: Literal["companion", "completion"]
    item_id: str | None = Field(default=None, min_length=1, max_length=128)


class Event(Input):
    event_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    phase: Literal["start", "waiting", "stopped", "failed", "cancelled", "session_end"]


def bearer(request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "OpenVegas sign-in required.")
    return auth[7:]


async def account(request: Request):
    token = bearer(request)
    if token.startswith(("ov_device_", "ov_host_")):
        value = await service().resolve(token)
        if value["kind"] != "device":
            raise HTTPException(403, "Host grants cannot delegate credentials.")
        return value
    return await get_current_user(request)


async def host_account(request: Request):
    value = await service().resolve(bearer(request))
    if value["kind"] != "host":
        raise HTTPException(403, "A host-scoped grant is required.")
    return value


User = Annotated[dict, Depends(get_current_user)]
Account = Annotated[dict, Depends(account)]
HostAccount = Annotated[dict, Depends(host_account)]


@router.post("/device/start")
async def start(req: Start, request: Request):
    await service().rate("start:" + (request.client.host if request.client else "unknown"), 10)
    return await service().start(req.code_challenge)


@router.post("/device/approve")
async def approve(req: Approval, request: Request, user: User):
    _assert_same_origin(request)
    await service().rate("approve:" + user["user_id"], 10)
    await service().approve(req.user_code, user["user_id"], req.approved)
    return {"approved": req.approved}


@router.post("/device/exchange")
async def exchange(req: Exchange):
    await service().rate("poll:" + digest(req.device_code), 35)
    return await service().exchange(req.device_code, req.verifier)


@router.post('/game-login/start')
async def game_start(req:Start,request:Request):
    await service().rate('game-start:'+(request.client.host if request.client else 'unknown'),10)
    return await service().start(req.code_challenge,purpose='game')


@router.post('/game-login/approve')
async def game_approve(req:Approval,request:Request,user:User):
    _assert_same_origin(request)
    await service().rate('game-approve:'+user['user_id'],10)
    await service().approve(req.user_code,user['user_id'],req.approved,purpose='game')
    return {'approved':req.approved}


@router.post('/game-login/exchange')
async def game_exchange(req:Exchange):
    await service().rate('game-poll:'+digest(req.device_code),35)
    return await service().exchange(req.device_code,req.verifier,purpose='game')


@router.post("/lease")
async def lease(req: Host, request: Request, user: Account):
    if user.get("kind") != "device":
        raise HTTPException(403, "Enroll a revocable device before requesting a host grant.")
    await service().rate("lease:" + user["user_id"])
    parent = digest(bearer(request))
    return await service().lease(user["user_id"], req.host, parent)


@router.post("/device/enroll")
async def enroll(user: User):
    await service().rate("enroll:" + user["user_id"], 10)
    return await service().enroll(user["user_id"])


@router.get("/account")
async def me(user: Account):
    balance = await get_wallet().get_balance("user:" + user["user_id"])
    return {"user_id": user["user_id"], "balance": str(balance), "scope": "integrations"}


@router.post("/revoke")
async def revoke(request: Request, user: Account):
    if user.get("kind") != "device":
        raise HTTPException(403, "A device credential is required for revocation.")
    await service().revoke(bearer(request))
    return {"revoked": True}


@router.get("/state")
async def state(user: HostAccount):
    balance = await get_wallet().get_balance("user:" + user["user_id"])
    owned = await get_store_service().list_owned(user["user_id"])
    return {"user_id": user["user_id"], "balance": str(balance), "cosmetics": owned,
            "billing_mode": "host_managed",
            "gateway_available": os.getenv("OPENVEGAS_NATIVE_BILLING_ENABLED", "0").lower() in {"1", "true"},
            "xp": None, "rewards": "disabled_by_design",
            "tracking": "not_certified",
            "notice": "Activity does not earn credits. Existing host billing and OpenVegas game payouts are unchanged."}


@router.post("/cosmetics/equip")
async def equip(req: Equip, user: HostAccount):
    from openvegas.store.service import StoreError
    try:
        return await get_store_service().equip(user["user_id"], req.slot, req.item_id)
    except StoreError:
        raise HTTPException(403, "Cosmetic is not available for this account.") from None


@router.post("/events")
async def event(req: Event, user: HostAccount):
    return await service().observe(user["user_id"], user["host"], req.event_id, req.phase)
