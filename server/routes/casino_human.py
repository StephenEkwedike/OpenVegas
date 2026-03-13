"""Human casino API routes."""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from openvegas.wallet.ledger import InsufficientBalance
from server.middleware.auth import get_current_user
from server.services.dependencies import get_human_casino_service
from server.services.demo_admin import is_demo_admin_user

router = APIRouter(prefix="/casino/human")


class StartSessionRequest(BaseModel):
    max_loss_v: float = 100.0
    max_rounds: int = 100
    idempotency_key: str


class StartRoundRequest(BaseModel):
    casino_session_id: str
    game_code: str
    wager_v: float
    idempotency_key: str


class ActionRequest(BaseModel):
    action: str
    payload: dict = {}
    idempotency_key: str


class ResolveRequest(BaseModel):
    idempotency_key: str


class DemoAutoplayRequest(BaseModel):
    casino_session_id: str
    game_code: str
    wager_v: float
    idempotency_key: str


def _is_demo_admin(user_id: str) -> bool:
    if os.getenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "0") != "1":
        return False
    return is_demo_admin_user(user_id)


def _json_response(status_code: int, body_text: str) -> Response:
    return Response(content=body_text, status_code=status_code, media_type="application/json")


def _map_value_error(e: ValueError) -> HTTPException:
    msg = str(e)
    if "idempotency_conflict" in msg:
        return HTTPException(status_code=409, detail="Idempotency key conflict")
    if "not found" in msg.lower():
        return HTTPException(status_code=404, detail=msg)
    if "unknown game" in msg.lower():
        return HTTPException(status_code=400, detail=msg)
    return HTTPException(status_code=400, detail=msg)


@router.post("/sessions/start")
async def start_session(req: StartSessionRequest, user: dict = Depends(get_current_user)):
    svc = get_human_casino_service()
    try:
        resp = await svc.start_session(
            user_id=user["user_id"],
            max_loss_v=Decimal(str(req.max_loss_v)),
            max_rounds=int(req.max_rounds),
            idempotency_key=req.idempotency_key,
        )
        return _json_response(resp.status_code, resp.body_text)
    except ValueError as e:
        raise _map_value_error(e)


@router.get("/games")
async def list_games(user: dict = Depends(get_current_user)):
    _ = user
    return await get_human_casino_service().list_games()


@router.post("/rounds/start")
async def start_round(req: StartRoundRequest, user: dict = Depends(get_current_user)):
    svc = get_human_casino_service()
    try:
        resp = await svc.start_round(
            user_id=user["user_id"],
            session_id=req.casino_session_id,
            game_code=req.game_code,
            wager_v=Decimal(str(req.wager_v)),
            idempotency_key=req.idempotency_key,
        )
        return _json_response(resp.status_code, resp.body_text)
    except InsufficientBalance as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise _map_value_error(e)


@router.post("/rounds/{round_id}/action")
async def action_round(round_id: str, req: ActionRequest, user: dict = Depends(get_current_user)):
    svc = get_human_casino_service()
    try:
        resp = await svc.apply_action(
            user_id=user["user_id"],
            round_id=round_id,
            action=req.action,
            payload=req.payload,
            idempotency_key=req.idempotency_key,
        )
        return _json_response(resp.status_code, resp.body_text)
    except ValueError as e:
        raise _map_value_error(e)


@router.post("/rounds/{round_id}/resolve")
async def resolve_round(round_id: str, req: ResolveRequest, user: dict = Depends(get_current_user)):
    svc = get_human_casino_service()
    try:
        resp = await svc.resolve_round(
            user_id=user["user_id"],
            round_id=round_id,
            idempotency_key=req.idempotency_key,
        )
        return _json_response(resp.status_code, resp.body_text)
    except ValueError as e:
        raise _map_value_error(e)


@router.get("/rounds/{round_id}/verify")
async def verify_round(round_id: str, user: dict = Depends(get_current_user)):
    try:
        return await get_human_casino_service().verify_round(user_id=user["user_id"], round_id=round_id)
    except ValueError as e:
        raise _map_value_error(e)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, user: dict = Depends(get_current_user)):
    try:
        return await get_human_casino_service().get_session(user_id=user["user_id"], session_id=session_id)
    except ValueError as e:
        raise _map_value_error(e)


@router.post("/rounds/demo-autoplay")
async def demo_autoplay(req: DemoAutoplayRequest, user: dict = Depends(get_current_user)):
    if not _is_demo_admin(user["user_id"]):
        raise HTTPException(status_code=403, detail="Demo mode not allowed")
    svc = get_human_casino_service()
    try:
        resp = await svc.demo_autoplay(
            user_id=user["user_id"],
            session_id=req.casino_session_id,
            game_code=req.game_code,
            wager_v=Decimal(str(req.wager_v)),
            idempotency_key=req.idempotency_key or f"demo-{uuid.uuid4()}",
        )
        return _json_response(resp.status_code, resp.body_text)
    except InsufficientBalance as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise _map_value_error(e)
