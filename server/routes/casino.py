"""Agent-only casino API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.middleware.auth import require_scope, reject_human_users
from server.services.dependencies import get_casino_service
from openvegas.wallet.ledger import InsufficientBalance

router = APIRouter(prefix="/v1/agent/casino")


class StartSessionRequest(BaseModel):
    agent_session_id: str
    max_loss_v: float


class StartRoundRequest(BaseModel):
    casino_session_id: str
    game_code: str
    wager_v: float


class ActionRequest(BaseModel):
    action: str
    payload: dict = {}
    idempotency_key: str


@router.post("/sessions/start")
async def start_casino_session(
    req: StartSessionRequest,
    agent: dict = Depends(require_scope("casino.play")),
    _=Depends(reject_human_users),
):
    svc = get_casino_service()
    from decimal import Decimal
    try:
        return await svc.start_session(
            org_id=agent["org_id"],
            agent_account_id=agent["agent_account_id"],
            agent_session_id=req.agent_session_id,
            max_loss_v=Decimal(str(req.max_loss_v)),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/games")
async def list_games(
    agent: dict = Depends(require_scope("casino.play")),
    _=Depends(reject_human_users),
):
    from server.services.dependencies import get_db
    db = get_db()
    rows = await db.fetch("SELECT * FROM casino_game_catalog WHERE enabled = TRUE")
    return {"games": [dict(r) for r in rows]}


@router.post("/rounds/start")
async def start_round(
    req: StartRoundRequest,
    agent: dict = Depends(require_scope("casino.play")),
    _=Depends(reject_human_users),
):
    svc = get_casino_service()
    from decimal import Decimal
    try:
        return await svc.start_round(
            session_id=req.casino_session_id,
            game_code=req.game_code,
            wager_v=Decimal(str(req.wager_v)),
            agent_account_id=agent["agent_account_id"],
        )
    except (ValueError, InsufficientBalance) as e:
        raise HTTPException(400, str(e))


@router.post("/rounds/{round_id}/action")
async def submit_action(
    round_id: str,
    req: ActionRequest,
    agent: dict = Depends(require_scope("casino.play")),
    _=Depends(reject_human_users),
):
    svc = get_casino_service()
    try:
        return await svc.apply_action(
            round_id, req.action, req.payload,
            req.idempotency_key, agent["agent_account_id"],
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/rounds/{round_id}/resolve")
async def resolve_round(
    round_id: str,
    agent: dict = Depends(require_scope("casino.play")),
    _=Depends(reject_human_users),
):
    svc = get_casino_service()
    try:
        return await svc.resolve_round(round_id, agent["agent_account_id"])
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/rounds/{round_id}/verify")
async def verify_round(
    round_id: str,
    agent: dict = Depends(require_scope("casino.play")),
):
    from server.services.dependencies import get_db
    db = get_db()
    row = await db.fetchrow(
        """SELECT cv.*, cr.game_code FROM casino_verifications cv
           JOIN casino_rounds cr ON cv.round_id = cr.id
           JOIN casino_sessions cs ON cr.session_id = cs.id
           WHERE cv.round_id = $1 AND cs.agent_account_id = $2""",
        round_id, agent["agent_account_id"],
    )
    if not row:
        raise HTTPException(
            404, "Verification data not found — round may not be resolved yet, or does not belong to this agent"
        )
    return {
        "round_id": round_id,
        "rng_commit": row["commit_hash"],
        "rng_reveal": row["reveal_seed"],
        "client_seed": row["client_seed"],
        "nonce": row["nonce"],
        "game_code": row["game_code"],
    }


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    agent: dict = Depends(require_scope("casino.play")),
):
    svc = get_casino_service()
    try:
        return await svc.get_session(session_id, agent["agent_account_id"])
    except ValueError as e:
        raise HTTPException(404, str(e))
