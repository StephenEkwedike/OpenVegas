"""Authenticated, default-off transport for owned task-boundary handoffs.

Preparation and confirmation do not execute inference. The caller must retain
its old selection until a matching confirmation has been received.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.contracts.native_handoff import (
    ConfirmNativeHandoff,
    NativeHandoffResponse,
    PrepareNativeHandoff,
)
from server.middleware.auth import get_current_user
from server.middleware.private_validation import PrivateInferenceRoute
from server.services.dependencies import get_db, get_fraud_engine
from server.services.native_handoff_service import (
    HandoffPreview,
    HandoffSelection,
    NativeHandoffService,
)

_HEADERS = {"Cache-Control": "private, no-store"}
_DETAIL = "Model handoff could not be verified; the current selection was not changed."
MAX_BODY_BYTES = 16 * 1024


class PrivateHandoffRoute(PrivateInferenceRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded_handler(request):
            def rejected():
                return JSONResponse(status_code=413, content={"detail": "Handoff request is too large."},
                                    headers=_HEADERS)

            length = request.headers.get("content-length")
            if length is not None and (not length.isascii() or not length.isdecimal()
                                       or len(length) > 10 or int(length) > MAX_BODY_BYTES):
                return rejected()
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_BODY_BYTES:
                    return rejected()
                body.extend(chunk)
            sent = False

            async def receive():
                nonlocal sent
                if sent:
                    return await request.receive()
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            return await handler(Request(request.scope, receive=receive))

        return bounded_handler


router = APIRouter(prefix="/agent/native-handoffs", tags=["native-handoffs"],
                   route_class=PrivateHandoffRoute)


def _failure(exc=None):
    allowed = {APIErrorCode.HANDOFF_BLOCKED, APIErrorCode.STALE_PROJECTION,
               APIErrorCode.IDEMPOTENCY_CONFLICT}
    code = exc.code if isinstance(exc, ContractError) and exc.code in allowed else APIErrorCode.HANDOFF_BLOCKED
    return JSONResponse(status_code=409, content={"error": code.value, "detail": _DETAIL}, headers=_HEADERS)


async def _rate_gate(user_id):
    try:
        allowed = await get_fraud_engine().check_inference(user_id)
    except Exception:  # noqa: BLE001 - Limiter diagnostics may contain private storage details.
        allowed = False
    if allowed is True:
        return None
    return JSONResponse(status_code=429,
        content={"error": "rate_limited", "detail": "Model switching is temporarily rate limited. Try again later."},
        headers=_HEADERS)


def _response(preview):
    if type(preview) is not HandoffPreview or type(preview.selection) is not HandoffSelection:
        raise ValueError(_DETAIL)
    # No dict passthrough from internal records: only this typed summary crosses
    # the transport boundary, even if storage grows more private fields later.
    value = NativeHandoffResponse.model_validate({
        "handoff_id": preview.handoff_id,
        "handoff_sha256": preview.handoff_sha256,
        "selection": {"provider": "openrouter", "enable_tools": True, **asdict(preview.selection)},
        "expires_at": preview.expires_at,
        "task_count": preview.task_count,
        "file_count": preview.file_count,
        "unique_file_count": preview.unique_file_count,
        "observation_count": preview.observation_count,
        "destination_scope": preview.destination_scope,
    })
    return JSONResponse(content=value.model_dump(mode="json"), headers=_HEADERS)


@router.post("/prepare")
async def prepare_native_handoff(request: PrepareNativeHandoff,
                                  user: Annotated[dict, Depends(get_current_user)]):
    try:
        from openvegas.agent.native_handoff_store import _enabled
        _enabled()
        limited = await _rate_gate(user["user_id"])
        if limited is not None:
            return limited
        selection = HandoffSelection(request.selection.model, request.selection.enable_web_search,
                                     request.selection.reasoning_effort, request.selection.max_tokens)
        preview = await NativeHandoffService(get_db()).prepare(
            user_id=user["user_id"], source_scope=request.source_scope,
            source_ref=request.source_ref, selection=selection,
            idempotency_key=request.idempotency_key,
        )
        if preview.selection != selection:
            raise ValueError(_DETAIL)
        return _response(preview)
    except Exception as exc:  # noqa: BLE001 - private errors must not echo SQL, inputs or parser context.
        return _failure(exc)


@router.post("/confirm")
async def confirm_native_handoff(request: ConfirmNativeHandoff,
                                  user: Annotated[dict, Depends(get_current_user)]):
    try:
        from openvegas.agent.native_handoff_store import _enabled
        _enabled()
        limited = await _rate_gate(user["user_id"])
        if limited is not None:
            return limited
        preview = await NativeHandoffService(get_db()).confirm(
            user_id=user["user_id"], handoff_id=request.handoff_id,
            handoff_sha256=request.handoff_sha256, destination_scope=request.destination_scope,
            idempotency_key=request.idempotency_key,
        )
        if (preview.handoff_id != request.handoff_id
                or preview.handoff_sha256 != request.handoff_sha256
                or preview.destination_scope != request.destination_scope):
            raise ValueError(_DETAIL)
        return _response(preview)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
