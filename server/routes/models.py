"""Model catalog routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.providers import model_switch_enabled, provider_descriptors
from server.middleware.auth import get_current_user
from server.services.dependencies import get_catalog

router = APIRouter()


@router.get("/models")
async def list_models(
    provider: str | None = Query(None),
    user: dict = Depends(get_current_user),
):
    catalog = get_catalog()
    try:
        models = await catalog.list_descriptors(provider=provider)
    except ContractError as exc:
        raise HTTPException(422, detail={"error": exc.code.value, "message": exc.detail}) from None
    return {
        "models": models,
        "providers": provider_descriptors(),
        "switching_enabled": model_switch_enabled(),
    }


class ModelSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=200)
    required_capabilities: list[str] = Field(default_factory=list, max_length=16)
    max_tokens: int | None = Field(default=None, ge=1, le=10_000_000)


@router.post("/models/validate")
async def validate_model_selection(
    request: ModelSelectionRequest,
    user: dict = Depends(get_current_user),
):
    """Read-only preflight; caller must not mutate its selection until this succeeds."""
    if not model_switch_enabled():
        raise HTTPException(
            409,
            detail={
                "error": "model_switch_disabled",
                "message": "Model switching is disabled by the server operator.",
            },
        )
    try:
        model = await get_catalog().validate_selection(
            request.provider,
            request.model,
            required_capabilities=request.required_capabilities,
            max_tokens=request.max_tokens,
        )
    except ContractError as exc:
        status = 409 if exc.code == APIErrorCode.PROVIDER_UNAVAILABLE else 422
        raise HTTPException(
            status, detail={"error": exc.code.value, "message": exc.detail}
        ) from None
    return {"model": model, "selection_valid": True, "state_changed": False}


# Strip submitted inputs from validation failures on continuity routes. Default
# Pydantic errors can otherwise reflect a rejected credential or transcript.
class _CanonicalRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request):
            from fastapi.responses import JSONResponse

            try:
                return await handler(request)
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "private, no-store"}
                raise
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": "Invalid canonical request; credentials, attachments, tools and caller history are not accepted."
                    },
                    headers={"Cache-Control": "private, no-store"},
                )

        return guarded


router.route_class = _CanonicalRoute


# Canonical continuity is deliberately opt-in and text-only. These endpoints never
# accept caller-owned history, provider credentials or a caller-selected user ID.
class CanonicalCreateRequest(ModelSelectionRequest):
    max_tokens: int = Field(default=1024, ge=1, le=1024, strict=True)


class CanonicalSwitchRequest(CanonicalCreateRequest):
    thread_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    commit: bool = Field(default=False, strict=True)
    expected_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CanonicalAskRequest(CanonicalCreateRequest):
    thread_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt: str = Field(min_length=1, max_length=64000)
    idempotency_key: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")


def _continuity_failure(exc):
    from openvegas.gateway.conversation import ContinuityError
    from openvegas.wallet.ledger import InsufficientBalance

    if isinstance(exc, ContractError):
        detail = {"error": exc.code.value, "message": exc.detail}
    elif isinstance(exc, InsufficientBalance):
        detail = {
            "error": "insufficient_balance",
            "message": "Insufficient balance; no switch was applied.",
        }
    elif isinstance(exc, ContinuityError):
        detail = {"error": "continuity_unavailable", "message": str(exc)}
    else:
        detail = {
            "error": "continuity_unavailable",
            "message": "Model unavailable; refresh the catalog.",
        }
    return HTTPException(409, detail=detail, headers={"Cache-Control": "private, no-store"})


async def _canonical_access_gate(user: dict) -> None:
    """Same shared inference velocity key and managed-account policy for all writes."""
    from server.services.dependencies import get_fraud_engine, get_llm_mode_service

    if user.get("account_type", "human") != "human":
        raise HTTPException(403, "Canonical conversations require a human account.")
    try:
        allowed = await get_fraud_engine().check_inference(user["user_id"])
    except Exception:  # noqa: BLE001 - Fail closed without reflecting limiter diagnostics.
        raise HTTPException(429, "Inference rate limit reached.") from None
    if allowed is False:
        raise HTTPException(429, "Inference rate limit reached.")
    mode = await get_llm_mode_service().resolve_for_user(user_id=user["user_id"])
    mode = mode.as_dict() if hasattr(mode, "as_dict") else dict(mode)
    if mode.get("effective_mode") != "wrapper":
        raise HTTPException(400, "Canonical continuity requires managed API access, not BYOK.")
    if mode.get("conversation_mode") != "persistent":
        raise HTTPException(400, "Canonical continuity requires explicit persistent conversation mode.")


@router.post("/models/conversations")
async def create_canonical_conversation(
    request: CanonicalCreateRequest, user: Annotated[dict, Depends(get_current_user)]
):
    from dataclasses import asdict

    from fastapi.responses import JSONResponse

    from openvegas.gateway.conversation import ContinuityError
    from server.services.dependencies import get_provider_thread_service

    await _canonical_access_gate(user)
    try:
        plan = await get_provider_thread_service().create_canonical_thread(
            user_id=user["user_id"],
            provider=request.provider,
            model_id=request.model,
            catalog=get_catalog(),
            max_output_tokens=request.max_tokens,
            required_capabilities=request.required_capabilities,
        )
    except (ContinuityError, ContractError) as exc:
        raise _continuity_failure(exc) from None
    return JSONResponse(asdict(plan), headers={"Cache-Control": "private, no-store"})


@router.post("/models/switch")
async def switch_canonical_conversation(
    request: CanonicalSwitchRequest, user: Annotated[dict, Depends(get_current_user)]
):
    from dataclasses import asdict

    from fastapi.responses import JSONResponse

    from openvegas.gateway.conversation import ContinuityError
    from server.services.dependencies import get_provider_thread_service

    await _canonical_access_gate(user)
    try:
        plan = await get_provider_thread_service().canonical_switch(
            user_id=user["user_id"],
            thread_id=request.thread_id,
            provider=request.provider,
            model_id=request.model,
            catalog=get_catalog(),
            expected_revision=request.expected_revision,
            commit=request.commit,
            max_output_tokens=request.max_tokens,
            required_capabilities=request.required_capabilities,
        )
    except (ContinuityError, ContractError) as exc:
        raise _continuity_failure(exc) from None
    return JSONResponse(asdict(plan), headers={"Cache-Control": "private, no-store"})


@router.post("/models/conversations/ask")
async def ask_canonical_conversation(
    request: CanonicalAskRequest, user: Annotated[dict, Depends(get_current_user)]
):
    from fastapi.responses import JSONResponse

    from openvegas.gateway.catalog import ModelDisabled
    from openvegas.gateway.conversation import ContinuityError
    from openvegas.security.policy import enforce_before_tool_call
    from openvegas.wallet.ledger import InsufficientBalance
    from server.services.dependencies import (
        get_gateway,
        get_provider_thread_service,
    )

    await _canonical_access_gate(user)
    from openvegas.security.policy import contains_obvious_secret
    if contains_obvious_secret(request.prompt):
        raise HTTPException(400, "Secret-like input is blocked.")
    policy = enforce_before_tool_call(user["user_id"], "inference", {
        "prompt": request.prompt, "provider": request.provider, "model": request.model,
    })
    if not policy.allow:
        raise HTTPException(400, "Inference policy blocked this request.")
    try:
        # Check requested capabilities even though this endpoint never enables tools.
        await get_catalog().validate_selection(
            request.provider,
            request.model,
            required_capabilities=request.required_capabilities,
            max_tokens=request.max_tokens,
        )
        result = await get_provider_thread_service().infer_canonical(
            user_id=user["user_id"],
            thread_id=request.thread_id,
            provider=request.provider,
            model_id=request.model,
            catalog=get_catalog(),
            gateway=get_gateway(),
            expected_revision=request.expected_revision,
            prompt=request.prompt,
            idempotency_key=request.idempotency_key,
            max_output_tokens=request.max_tokens,
        )
    except (ContinuityError, ContractError, ModelDisabled, InsufficientBalance) as exc:
        raise _continuity_failure(exc) from None
    return JSONResponse(result, headers={"Cache-Control": "private, no-store"})
