"""Agent orchestration routes (run lifecycle + tool runtime callbacks)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from openvegas.agent.tool_stream import stream_tool_events
from openvegas.contracts.errors import APIErrorCode, ContractError
from server.middleware.auth import get_current_user
from server.services.dependencies import get_agent_orchestration_service

router = APIRouter(prefix="/agent/runs", tags=["agent-orchestration"])


class CreateRunRequest(BaseModel):
    state: str = "created"
    is_resumable: bool = False
    expires_in_seconds: int | None = Field(default=None, ge=1)


class TransitionRequest(BaseModel):
    action: str
    payload: dict[str, Any] | None = None
    idempotency_key: str
    expected_run_version: int = Field(ge=0)
    expected_valid_actions_signature: str


class ConsumeApprovalRequest(BaseModel):
    idempotency_key: str
    expected_run_version: int = Field(ge=0)
    expected_valid_actions_signature: str


class CancelRequest(BaseModel):
    idempotency_key: str
    expected_run_version: int = Field(ge=0)
    expected_valid_actions_signature: str


class RegisterWorkspaceRequest(BaseModel):
    runtime_session_id: str
    workspace_root: str
    workspace_fingerprint: str
    git_root: str | None = None


class ProposeToolRequest(BaseModel):
    runtime_session_id: str
    expected_run_version: int = Field(ge=0)
    expected_valid_actions_signature: str
    idempotency_key: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    shell_mode: str | None = None
    timeout_sec: int | None = Field(default=None, ge=1)
    plan_mode: bool = False
    native_inference_request_id: str | None = Field(default=None, max_length=36)
    native_provider_call_id: str | None = Field(default=None, max_length=256)


class StartToolRequest(BaseModel):
    runtime_session_id: str
    tool_call_id: str
    execution_token: str
    expected_run_version: int = Field(ge=0)
    expected_valid_actions_signature: str
    idempotency_key: str


class HeartbeatToolRequest(BaseModel):
    runtime_session_id: str
    tool_call_id: str
    execution_token: str


class CancelToolRequest(BaseModel):
    runtime_session_id: str
    execution_token: str


class ToolResultRequest(BaseModel):
    runtime_session_id: str
    tool_call_id: str
    execution_token: str
    result_status: str
    result_payload: dict[str, Any] = Field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stderr_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_submission_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def _status_for_error(code: APIErrorCode) -> int:
    if code in {
        APIErrorCode.STALE_PROJECTION,
        APIErrorCode.INVALID_TRANSITION,
        APIErrorCode.APPROVAL_REQUIRED,
        APIErrorCode.ACTIVE_MUTATION_IN_PROGRESS,
        APIErrorCode.RUN_NOT_RESUMABLE,
        APIErrorCode.IDEMPOTENCY_CONFLICT,
        APIErrorCode.HANDOFF_BLOCKED,
        APIErrorCode.MUTATION_UNCERTAIN,
        APIErrorCode.TOOL_NOT_ALLOWED_IN_PLAN_MODE,
        APIErrorCode.TOOL_TIMEOUT,
        APIErrorCode.TOOL_EXECUTION_FAILED,
        APIErrorCode.EDITOR_UNAVAILABLE,
        APIErrorCode.EDITOR_OPEN_FAILED,
        APIErrorCode.WORKSPACE_PATH_OUT_OF_BOUNDS,
        APIErrorCode.BINARY_FILE_UNSUPPORTED,
        APIErrorCode.UNSUPPORTED_PLATFORM,
        APIErrorCode.LEASE_DELETE_MISMATCH,
    }:
        return 409
    if code == APIErrorCode.INSUFFICIENT_BALANCE:
        return 400
    return 400


@router.post("")
async def create_run(req: CreateRunRequest, user: dict = Depends(get_current_user)):
    svc = get_agent_orchestration_service()
    expires_at = None
    if req.expires_in_seconds:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(req.expires_in_seconds))
    try:
        return await svc.create_run(
            user_id=user["user_id"],
            state=req.state,
            is_resumable=req.is_resumable,
            expires_at=expires_at,
        )
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.get("/{run_id}")
async def get_run(run_id: str, user: dict = Depends(get_current_user)):
    svc = get_agent_orchestration_service()
    try:
        return await svc.get_run(user_id=user["user_id"], run_id=run_id)
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.post("/{run_id}/transition")
async def transition(run_id: str, req: TransitionRequest, user: dict = Depends(get_current_user)):
    svc = get_agent_orchestration_service()
    try:
        result = await svc.transition_run(
            user_id=user["user_id"],
            actor_role=user.get("role", "authenticated"),
            run_id=run_id,
            idempotency_key=req.idempotency_key,
            expected_run_version=req.expected_run_version,
            expected_valid_actions_signature=req.expected_valid_actions_signature,
            action=req.action,
            payload=req.payload,
        )
        return JSONResponse(status_code=result.status_code, content=result.payload)
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.post("/{run_id}/approvals/{tool_call_id}/{approval_id}/consume")
async def consume_approval(
    run_id: str,
    tool_call_id: str,
    approval_id: str,
    req: ConsumeApprovalRequest,
    user: dict = Depends(get_current_user),
):
    svc = get_agent_orchestration_service()
    try:
        result = await svc.consume_approval(
            user_id=user["user_id"],
            actor_role=user.get("role", "authenticated"),
            run_id=run_id,
            tool_call_id=tool_call_id,
            approval_id=approval_id,
            idempotency_key=req.idempotency_key,
            expected_run_version=req.expected_run_version,
            expected_valid_actions_signature=req.expected_valid_actions_signature,
        )
        return JSONResponse(status_code=result.status_code, content=result.payload)
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.post("/{run_id}/cancel")
async def cancel(run_id: str, req: CancelRequest, user: dict = Depends(get_current_user)):
    svc = get_agent_orchestration_service()
    try:
        result = await svc.transition_run(
            user_id=user["user_id"],
            actor_role=user.get("role", "authenticated"),
            run_id=run_id,
            idempotency_key=req.idempotency_key,
            expected_run_version=req.expected_run_version,
            expected_valid_actions_signature=req.expected_valid_actions_signature,
            action="cancel",
            payload={},
        )
        return JSONResponse(status_code=result.status_code, content=result.payload)
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.post("/{run_id}/ui/handoff-check")
async def ui_handoff_check(run_id: str, user: dict = Depends(get_current_user)):
    svc = get_agent_orchestration_service()
    try:
        payload = await svc.check_handoff_block(user_id=user["user_id"], run_id=run_id)
        if payload.get("error") == APIErrorCode.HANDOFF_BLOCKED.value:
            return JSONResponse(status_code=409, content=payload)
        return payload
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.post("/{run_id}/session/register-workspace")
async def register_workspace(run_id: str, req: RegisterWorkspaceRequest, user: dict = Depends(get_current_user)):
    svc = get_agent_orchestration_service()
    try:
        return await svc.register_workspace(
            user_id=user["user_id"],
            run_id=run_id,
            runtime_session_id=req.runtime_session_id,
            workspace_root=req.workspace_root,
            workspace_fingerprint=req.workspace_fingerprint,
            git_root=req.git_root,
        )
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.post("/{run_id}/tools/propose")
async def propose_tool(run_id: str, req: ProposeToolRequest, user: dict = Depends(get_current_user)):
    svc = get_agent_orchestration_service()
    try:
        result = await svc.propose_tool_call(
            user_id=user["user_id"],
            actor_role=user.get("role", "authenticated"),
            run_id=run_id,
            runtime_session_id=req.runtime_session_id,
            expected_run_version=req.expected_run_version,
            expected_valid_actions_signature=req.expected_valid_actions_signature,
            idempotency_key=req.idempotency_key,
            tool_name=req.tool_name,
            arguments=req.arguments,
            shell_mode=req.shell_mode,
            timeout_sec=req.timeout_sec,
            plan_mode=req.plan_mode,
            **({"native_inference_request_id": req.native_inference_request_id,
                "native_provider_call_id": req.native_provider_call_id}
               if req.native_inference_request_id is not None or req.native_provider_call_id is not None else {}),
        )
        return JSONResponse(status_code=result.status_code, content=result.payload)
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.get("/{run_id}/native-tool-receipts")
async def native_tool_receipts(
    run_id: str, runtime_session_id: str, provider: str, model: str,
    user: dict = Depends(get_current_user),
):
    try:
        return await get_agent_orchestration_service().native_tool_receipts(
            user_id=user["user_id"], run_id=run_id, runtime_session_id=runtime_session_id,
            provider=provider, model=model,
        )
    except ContractError as error:
        return JSONResponse(status_code=_status_for_error(error.code),
                            content={"error": error.code.value, "detail": error.detail})


@router.post("/{run_id}/tools/start")
async def start_tool(run_id: str, req: StartToolRequest, user: dict = Depends(get_current_user)):
    svc = get_agent_orchestration_service()
    try:
        result = await svc.start_tool_call(
            user_id=user["user_id"],
            actor_role=user.get("role", "authenticated"),
            run_id=run_id,
            runtime_session_id=req.runtime_session_id,
            tool_call_id=req.tool_call_id,
            execution_token=req.execution_token,
            expected_run_version=req.expected_run_version,
            expected_valid_actions_signature=req.expected_valid_actions_signature,
            idempotency_key=req.idempotency_key,
        )
        return JSONResponse(status_code=result.status_code, content=result.payload)
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.post("/{run_id}/tools/heartbeat")
async def heartbeat_tool(run_id: str, req: HeartbeatToolRequest, user: dict = Depends(get_current_user)):
    svc = get_agent_orchestration_service()
    try:
        payload = await svc.heartbeat_tool_call(
            user_id=user["user_id"],
            run_id=run_id,
            runtime_session_id=req.runtime_session_id,
            tool_call_id=req.tool_call_id,
            execution_token=req.execution_token,
        )
        return JSONResponse(status_code=200, content=payload)
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.post("/{run_id}/tools/{tool_call_id}/cancel")
async def cancel_tool(
    run_id: str,
    tool_call_id: str,
    req: CancelToolRequest,
    user: dict = Depends(get_current_user),
):
    svc = get_agent_orchestration_service()
    try:
        result = await svc.cancel_tool_call(
            user_id=user["user_id"],
            actor_role=user.get("role", "authenticated"),
            run_id=run_id,
            runtime_session_id=req.runtime_session_id,
            tool_call_id=tool_call_id,
            execution_token=req.execution_token,
        )
        return JSONResponse(status_code=result.status_code, content=result.payload)
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.post("/{run_id}/tools/result")
async def result_tool(run_id: str, req: ToolResultRequest, user: dict = Depends(get_current_user)):
    svc = get_agent_orchestration_service()
    try:
        result = await svc.result_tool_call(
            user_id=user["user_id"],
            actor_role=user.get("role", "authenticated"),
            run_id=run_id,
            runtime_session_id=req.runtime_session_id,
            tool_call_id=req.tool_call_id,
            execution_token=req.execution_token,
            result_status=req.result_status,
            result_payload=req.result_payload,
            stdout=req.stdout,
            stderr=req.stderr,
            stdout_truncated=req.stdout_truncated,
            stderr_truncated=req.stderr_truncated,
            stdout_sha256=req.stdout_sha256,
            stderr_sha256=req.stderr_sha256,
            result_submission_hash_value=req.result_submission_hash,
        )
        return JSONResponse(status_code=result.status_code, content=result.payload)
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.get("/{run_id}/tools/{tool_call_id}/stream")
async def stream_tool(run_id: str, tool_call_id: str, user: dict = Depends(get_current_user)):
    del user

    async def _events():
        async for event in stream_tool_events(run_id=run_id, tool_call_id=tool_call_id):
            yield f"data: {json.dumps(event, separators=(',', ':'), ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
