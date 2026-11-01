"""Mint routes — challenge creation and Tier 1 verification."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.middleware.auth import get_current_user
from server.services.dependencies import get_mint_service, get_fraud_engine
from openvegas.mint.engine import MintError

router = APIRouter()


class ChallengeRequest(BaseModel):
    amount_usd: float
    provider: str
    mode: str = "solo"


class VerifyRequest(BaseModel):
    challenge_id: str
    nonce: str
    provider: str
    model: str
    tier: str = "proxied"
    api_key: str


@router.post("/challenge")
async def create_challenge(
    req: ChallengeRequest,
    user: dict = Depends(get_current_user),
):
    fraud = get_fraud_engine()
    try:
        await fraud.check_mint(user["user_id"], req.amount_usd, "0.0.0.0")
    except Exception as e:
        raise HTTPException(429, str(e))

    mint_svc = get_mint_service()
    # Look up default model for provider from catalog
    catalog = mint_svc.catalog
    models = await catalog.list_models(req.provider)
    if not models:
        raise HTTPException(400, f"No enabled models for provider {req.provider}")
    model_id = models[0]["model_id"]

    challenge = await mint_svc.create_challenge(
        user_id=user["user_id"],
        amount_usd=req.amount_usd,
        provider=req.provider,
        model=model_id,
        mode=req.mode,
    )
    return {
        "id": challenge.id,
        "nonce": challenge.nonce,
        "provider": challenge.provider,
        "model": challenge.model,
        "mode": challenge.mode,
        "task_prompt": challenge.task_prompt,
        "max_credit_v": str(challenge.max_credit_v),
        "expires_at": challenge.expires_at.isoformat(),
    }


@router.post("/verify")
async def verify_mint(
    req: VerifyRequest,
    user: dict = Depends(get_current_user),
):
    if req.tier != "proxied":
        raise HTTPException(400, "Only proxied mint (Tier 1) is available in MVP.")

    mint_svc = get_mint_service()
    try:
        result = await mint_svc.verify_and_credit(
            challenge_id=req.challenge_id,
            user_id=user["user_id"],
            nonce=req.nonce,
            provider=req.provider,
            model=req.model,
            api_key=req.api_key,
        )
        return result
    except MintError as e:
        raise HTTPException(400, str(e))
