"""Agent runtime routes (sessions, infer, boost, and admin provisioning/policy/audit)."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from openvegas.gateway.inference import InferenceRequest
from openvegas.wallet.ledger import InsufficientBalance
from server.middleware.auth import get_current_user, require_scope
from server.services.dependencies import (
    get_agent_service,
    get_boost_service,
    get_catalog,
    get_db,
    get_gateway,
    get_org_service,
)

router = APIRouter(prefix="/v1/agent")

ALLOWED_AGENT_SCOPES = {"infer", "budget.read", "boost", "casino.play"}


class StartSessionRequest(BaseModel):
    envelope_v: Decimal | None = None


class InferRequest(BaseModel):
    session_id: str
    prompt: str
    provider: str
    model: str
    max_tokens: int = 1024


class BoostChallengeRequest(BaseModel):
    session_id: str


class BoostSubmitRequest(BaseModel):
    challenge_id: str
    artifact_text: str


class AdminCreateAgentAccountRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class AdminIssueTokenRequest(BaseModel):
    scopes: list[str]
    ttl_minutes: int = Field(default=60, ge=1, le=60 * 24 * 30)


class AdminUpdatePolicyRequest(BaseModel):
    allowed_providers: list[str] | None = None
    allowed_models: list[str] | None = None
    user_daily_cap_usd: Decimal | None = None
    byok_fallback_enabled: bool | None = None
    boost_enabled: bool | None = None
    casino_enabled: bool | None = None
    casino_agent_max_loss_v: Decimal | None = None
    casino_round_max_wager_v: Decimal | None = None
    casino_round_cooldown_ms: int | None = Field(default=None, ge=0)
    agent_default_envelope_v: Decimal | None = None
    agent_max_envelope_v: Decimal | None = None
    agent_session_ttl_sec: int | None = Field(default=None, ge=60)
    agent_infer_enabled: bool | None = None


def _decimal_to_str(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    return v


def _normalize_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    if not policy:
        return {}
    out: dict[str, Any] = {}
    for k, v in policy.items():
        out[k] = _decimal_to_str(v)
    return out


async def _require_org_admin(org_id: str, user_id: str) -> None:
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


async def _ensure_agent_belongs_to_org(org_id: str, agent_account_id: str) -> None:
    db = get_db()
    row = await db.fetchrow(
        "SELECT id FROM agent_accounts WHERE id = $1 AND org_id = $2",
        agent_account_id,
        org_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Agent account not found in org")


def _agent_policy_defaults(policy: dict[str, Any] | None) -> tuple[Decimal, Decimal, int, bool]:
    p = policy or {}
    default_env = Decimal(str(p.get("agent_default_envelope_v", os.getenv("OPENVEGAS_AGENT_DEFAULT_ENVELOPE_V", "25.0"))))
    max_env = Decimal(str(p.get("agent_max_envelope_v", os.getenv("OPENVEGAS_AGENT_MAX_ENVELOPE_V", "250.0"))))
    ttl_sec = int(p.get("agent_session_ttl_sec", os.getenv("OPENVEGAS_AGENT_SESSION_TTL_SECONDS", "1800")))
    ttl_sec = max(60, ttl_sec)
    infer_enabled = bool(p.get("agent_infer_enabled", True))
    return default_env, max_env, ttl_sec, infer_enabled


@router.post("/sessions/start")
async def start_session(req: StartSessionRequest, agent: dict = Depends(require_scope("infer"))):
    svc = get_agent_service()
    org_svc = get_org_service()
    try:
        policy = await org_svc.get_policy(agent["org_id"])
        default_env, max_env, ttl_sec, _ = _agent_policy_defaults(policy)
        envelope_v = Decimal(str(req.envelope_v)) if req.envelope_v is not None else default_env
        if envelope_v <= Decimal("0"):
            raise HTTPException(status_code=400, detail="envelope_v must be > 0")
        if envelope_v > max_env:
            raise HTTPException(status_code=400, detail=f"envelope_v exceeds org max ({max_env})")
        return await svc.start_session(
            agent_account_id=agent["agent_account_id"],
            org_id=agent["org_id"],
            envelope_v=envelope_v,
            ttl_seconds=ttl_sec,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))


@router.get("/budget")
async def budget(
    session_id: str = Query(...),
    agent: dict = Depends(require_scope("budget.read")),
):
    svc = get_agent_service()
    data = await svc.get_budget(session_id, agent_account_id=agent["agent_account_id"])
    if data.get("error"):
        raise HTTPException(404, data["error"])
    return data


@router.post("/infer")
async def infer(req: InferRequest, agent: dict = Depends(require_scope("infer"))):
    agent_svc = get_agent_service()
    catalog = get_catalog()
    gateway = get_gateway()
    org_svc = get_org_service()

    policy = await org_svc.get_policy(agent["org_id"])
    _, _, _, infer_enabled = _agent_policy_defaults(policy)
    if not infer_enabled:
        raise HTTPException(403, "Agent inference is disabled for this org")
    allowed = await org_svc.check_policy(agent["org_id"], req.provider, req.model)
    if not allowed:
        raise HTTPException(403, "Provider/model disallowed by org policy")

    mc = await catalog.get_pricing(req.provider, req.model)
    estimate = gateway._estimate_max_cost(mc, req.max_tokens)
    allowed_budget = await agent_svc.check_session_budget(
        session_id=req.session_id,
        amount_v=estimate,
        agent_account_id=agent["agent_account_id"],
    )
    if not allowed_budget:
        raise HTTPException(400, "Insufficient or inactive session budget")

    try:
        result = await gateway.infer(
            InferenceRequest(
                account_id=f"agent:{agent['agent_account_id']}",
                provider=req.provider,
                model=req.model,
                messages=[{"role": "user", "content": req.prompt}],
                max_tokens=req.max_tokens,
            )
        )
    except InsufficientBalance as e:
        raise HTTPException(400, str(e))

    await agent_svc.record_spend(
        session_id=req.session_id,
        amount_v=result.v_cost,
        agent_account_id=agent["agent_account_id"],
    )

    return {
        "text": result.text,
        "v_cost": str(result.v_cost),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }


@router.post("/boost/challenge")
async def boost_challenge(
    req: BoostChallengeRequest,
    agent: dict = Depends(require_scope("boost")),
):
    svc = get_boost_service()
    try:
        return await svc.create_challenge(
            org_id=agent["org_id"],
            session_id=req.session_id,
        )
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/boost/submit")
async def boost_submit(
    req: BoostSubmitRequest,
    agent: dict = Depends(require_scope("boost")),
):
    svc = get_boost_service()
    try:
        return await svc.submit_and_score(
            challenge_id=req.challenge_id,
            artifact_text=req.artifact_text,
            agent_account_id=agent["agent_account_id"],
            org_id=agent["org_id"],
        )
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/admin/orgs/{org_id}/accounts")
async def admin_create_agent_account(
    org_id: str,
    req: AdminCreateAgentAccountRequest,
    user: dict = Depends(get_current_user),
):
    await _require_org_admin(org_id, user["user_id"])
    svc = get_agent_service()
    try:
        return await svc.create_account(org_id=org_id, name=req.name.strip())
    except Exception as e:
        raise HTTPException(400, str(e))


@router.get("/admin/orgs/{org_id}/accounts")
async def admin_list_agent_accounts(
    org_id: str,
    user: dict = Depends(get_current_user),
):
    await _require_org_admin(org_id, user["user_id"])
    svc = get_agent_service()
    return {"accounts": await svc.list_accounts(org_id=org_id)}


@router.post("/admin/orgs/{org_id}/accounts/{agent_account_id}/tokens")
async def admin_issue_agent_token(
    org_id: str,
    agent_account_id: str,
    req: AdminIssueTokenRequest,
    user: dict = Depends(get_current_user),
):
    await _require_org_admin(org_id, user["user_id"])
    await _ensure_agent_belongs_to_org(org_id, agent_account_id)
    scopes = [str(s).strip() for s in req.scopes if str(s).strip()]
    if not scopes:
        raise HTTPException(status_code=400, detail="At least one scope is required")
    invalid = sorted(set(scopes) - ALLOWED_AGENT_SCOPES)
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid scopes: {', '.join(invalid)}")
    svc = get_agent_service()
    token = await svc.issue_token(
        agent_account_id=agent_account_id,
        scopes=scopes,
        ttl_minutes=int(req.ttl_minutes),
        created_by_user_id=user["user_id"],
    )
    return {
        "agent_account_id": agent_account_id,
        "token": token,
        "scopes": scopes,
        "ttl_minutes": int(req.ttl_minutes),
    }


@router.get("/admin/orgs/{org_id}/policies")
async def admin_get_org_policy(
    org_id: str,
    user: dict = Depends(get_current_user),
):
    await _require_org_admin(org_id, user["user_id"])
    svc = get_org_service()
    policy = await svc.get_policy(org_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Org policy not found")
    return {"org_id": org_id, "policy": _normalize_policy(policy)}


@router.patch("/admin/orgs/{org_id}/policies")
async def admin_update_org_policy(
    org_id: str,
    req: AdminUpdatePolicyRequest,
    user: dict = Depends(get_current_user),
):
    await _require_org_admin(org_id, user["user_id"])
    fields = req.model_dump(exclude_none=True)
    svc = get_org_service()
    await svc.set_policy(org_id, **fields)
    policy = await svc.get_policy(org_id)
    return {"org_id": org_id, "policy": _normalize_policy(policy or {})}


@router.get("/admin/orgs/{org_id}/audit")
async def admin_org_agent_audit(
    org_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    await _require_org_admin(org_id, user["user_id"])
    db = get_db()
    safe_limit = max(1, min(int(limit), 200))

    agent_sessions = await db.fetch(
        """
        SELECT s.id, s.agent_account_id, a.name AS agent_name, s.envelope_v, s.spent_v, s.status, s.started_at, s.ended_at
        FROM agent_sessions s
        JOIN agent_accounts a ON a.id = s.agent_account_id
        WHERE s.org_id = $1
        ORDER BY s.started_at DESC
        LIMIT $2
        """,
        org_id,
        safe_limit,
    )
    casino_sessions = await db.fetch(
        """
        SELECT cs.id, cs.agent_account_id, a.name AS agent_name, cs.status, cs.rounds_played, cs.net_pnl_v, cs.started_at, cs.ended_at
        FROM casino_sessions cs
        JOIN agent_accounts a ON a.id = cs.agent_account_id
        WHERE cs.org_id = $1
        ORDER BY cs.started_at DESC
        LIMIT $2
        """,
        org_id,
        safe_limit,
    )
    casino_payouts = await db.fetch(
        """
        SELECT cp.round_id, cp.wager_v, cp.payout_v, cp.net_v, cp.created_at, cr.session_id, cs.agent_account_id, a.name AS agent_name
        FROM casino_payouts cp
        JOIN casino_rounds cr ON cr.id = cp.round_id
        JOIN casino_sessions cs ON cs.id = cr.session_id
        JOIN agent_accounts a ON a.id = cs.agent_account_id
        WHERE cs.org_id = $1
        ORDER BY cp.created_at DESC
        LIMIT $2
        """,
        org_id,
        safe_limit,
    )

    return {
        "org_id": org_id,
        "counts": {
            "agent_sessions": len(agent_sessions),
            "casino_sessions": len(casino_sessions),
            "casino_payouts": len(casino_payouts),
        },
        "agent_sessions": [
            {
                "session_id": str(r["id"]),
                "agent_account_id": str(r["agent_account_id"]),
                "agent_name": str(r["agent_name"]),
                "envelope_v": str(r["envelope_v"]),
                "spent_v": str(r["spent_v"]),
                "status": str(r["status"]),
                "started_at": r["started_at"].isoformat() if r.get("started_at") else None,
                "ended_at": r["ended_at"].isoformat() if r.get("ended_at") else None,
            }
            for r in agent_sessions
        ],
        "casino_sessions": [
            {
                "casino_session_id": str(r["id"]),
                "agent_account_id": str(r["agent_account_id"]),
                "agent_name": str(r["agent_name"]),
                "status": str(r["status"]),
                "rounds_played": int(r["rounds_played"]),
                "net_pnl_v": str(r["net_pnl_v"]),
                "started_at": r["started_at"].isoformat() if r.get("started_at") else None,
                "ended_at": r["ended_at"].isoformat() if r.get("ended_at") else None,
            }
            for r in casino_sessions
        ],
        "casino_payouts": [
            {
                "round_id": str(r["round_id"]),
                "session_id": str(r["session_id"]),
                "agent_account_id": str(r["agent_account_id"]),
                "agent_name": str(r["agent_name"]),
                "wager_v": str(r["wager_v"]),
                "payout_v": str(r["payout_v"]),
                "net_v": str(r["net_v"]),
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            }
            for r in casino_payouts
        ],
    }
