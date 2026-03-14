"""Inference routes — AI gateway."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from openvegas.contracts.errors import APIErrorCode, ContractError
from server.middleware.auth import get_current_user
from server.services.dependencies import (
    get_fraud_engine,
    get_gateway,
    get_llm_mode_service,
    get_provider_thread_service,
)
from openvegas.gateway.catalog import ModelDisabled
from openvegas.gateway.inference import InferenceRequest
from openvegas.wallet.ledger import InsufficientBalance

router = APIRouter()


class AskRequest(BaseModel):
    prompt: str
    provider: str
    model: str
    idempotency_key: str | None = None
    thread_id: str | None = None
    conversation_mode: str | None = None
    persist_context: bool = True


class ModeUpdateRequest(BaseModel):
    llm_mode: str | None = None
    conversation_mode: str | None = None


@router.get("/mode")
async def get_mode(user: dict = Depends(get_current_user)):
    mode_svc = get_llm_mode_service()
    resolved = await mode_svc.resolve_for_user(user_id=user["user_id"])
    return resolved.as_dict()


@router.post("/mode")
async def set_mode(req: ModeUpdateRequest, user: dict = Depends(get_current_user)):
    mode_svc = get_llm_mode_service()
    resolved = await mode_svc.resolve_for_user(
        user_id=user["user_id"],
        requested_mode=req.llm_mode,
        requested_conversation_mode=req.conversation_mode,
    )
    return resolved.as_dict()


@router.post("/ask")
async def ask(
    req: AskRequest,
    user: dict = Depends(get_current_user),
):
    fraud = get_fraud_engine()
    try:
        await fraud.check_inference(user["user_id"])
    except Exception as e:
        return JSONResponse(
            status_code=429,
            content={"error": "rate_limited", "detail": str(e)},
        )

    mode_svc = get_llm_mode_service()
    mode_state = await mode_svc.resolve_for_user(user_id=user["user_id"])
    mode_payload = mode_state.as_dict() if hasattr(mode_state, "as_dict") else dict(mode_state)
    if mode_payload.get("effective_mode") == "byok":
        return JSONResponse(
            status_code=400,
            content={
                "error": APIErrorCode.BYOK_NOT_ALLOWED.value,
                "detail": "BYOK inference mode is not enabled for /inference/ask yet.",
                **mode_payload,
            },
        )

    try:
        thread_ctx = await get_provider_thread_service().prepare_thread(
            user_id=user["user_id"],
            provider=req.provider,
            model_id=req.model,
            thread_id=req.thread_id,
            conversation_mode=req.conversation_mode or mode_payload.get("conversation_mode"),
        )
    except ContractError as e:
        status = 503 if e.code == APIErrorCode.PROVIDER_UNAVAILABLE else 400
        return JSONResponse(
            status_code=status,
            content={"error": e.code.value, "detail": e.detail, **mode_payload},
        )

    gateway = get_gateway()
    try:
        result = await gateway.infer(InferenceRequest(
            account_id=f"user:{user['user_id']}",
            provider=req.provider,
            model=req.model,
            messages=[{"role": "user", "content": req.prompt}],
            idempotency_key=req.idempotency_key,
        ))
        await get_provider_thread_service().append_exchange(
            thread_ctx=thread_ctx,
            prompt=req.prompt,
            response_text=result.text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            persist_context=req.persist_context,
        )
        return {
            "text": result.text,
            "v_cost": str(result.v_cost),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "provider_request_id": result.provider_request_id,
            "thread_id": thread_ctx.thread_id,
            "thread_status": thread_ctx.thread_status,
            **mode_payload,
        }
    except ContractError as e:
        status = 503 if e.code == APIErrorCode.PROVIDER_UNAVAILABLE else 400
        return JSONResponse(
            status_code=status,
            content={"error": e.code.value, "detail": e.detail, **mode_payload},
        )
    except ModelDisabled as e:
        return JSONResponse(
            status_code=400,
            content={"error": "model_disabled", "detail": str(e), **mode_payload},
        )
    except InsufficientBalance as e:
        return JSONResponse(
            status_code=400,
            content={
                "error": APIErrorCode.INSUFFICIENT_BALANCE.value,
                "detail": str(e),
                **mode_payload,
            },
        )
