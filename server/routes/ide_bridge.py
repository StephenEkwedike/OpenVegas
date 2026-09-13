"""IDE bridge routes (session-bound, actor-bound)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.ide.bridge_registry import BridgeSession, get_bridge_registry
from openvegas.ide.jetbrains_bridge import JetBrainsBridge
from openvegas.ide.vscode_bridge import VSCodeBridge
from server.middleware.auth import get_current_user
from server.services.dependencies import get_db

router = APIRouter(prefix="/ide", tags=["ide-bridge"])


def _host_bridge_enabled() -> bool:
    runtime = os.getenv("OPENVEGAS_RUNTIME_ENV", os.getenv("ENV", "local")).strip().lower()
    # These adapters operate on the API host, not on a remote user's computer.
    return runtime not in {"prod", "production"} and os.getenv("OPENVEGAS_ENABLE_HOST_IDE_BRIDGE", "1") == "1"


def _require_host_bridge() -> None:
    if not _host_bridge_enabled():
        raise HTTPException(503, "Host IDE bridge is unavailable on this backend; use the local CLI tools")


class RegisterBridgeRequest(BaseModel):
    run_id: str
    runtime_session_id: str
    actor_id: str
    ide_type: str
    workspace_root: str
    workspace_fingerprint: str


class OpenFileRequest(BaseModel):
    run_id: str
    runtime_session_id: str
    path: str
    line: int | None = None
    col: int | None = None


class RunCommandRequest(BaseModel):
    run_id: str
    runtime_session_id: str
    command: str
    terminal_name: str | None = None


class ShowDiffRequest(BaseModel):
    run_id: str
    runtime_session_id: str
    path: str
    new_contents: str
    allow_partial_accept: bool = True


class ReadBufferRequest(BaseModel):
    run_id: str
    runtime_session_id: str
    path: str


class ContextRequest(BaseModel):
    run_id: str
    runtime_session_id: str


class IDEEnvelopeRequest(BaseModel):
    id: str
    type: str = "request"
    method: str
    params: dict


def _status_for_error(code: APIErrorCode) -> int:
    if code in {
        APIErrorCode.INVALID_TRANSITION,
        APIErrorCode.HANDOFF_BLOCKED,
    }:
        return 409
    return 400


def create_bridge(ide_type: str, workspace_root: str):
    kind = str(ide_type).strip().lower()
    if kind == "vscode":
        return VSCodeBridge(workspace_root=workspace_root)
    if kind == "jetbrains":
        return JetBrainsBridge(workspace_root=workspace_root)
    raise ContractError(APIErrorCode.INVALID_TRANSITION, f"Unsupported ide_type: {ide_type}")


async def _assert_run_binding(
    *,
    run_id: str,
    actor_id: str,
    runtime_session_id: str,
) -> None:
    db = get_db()
    row = await db.fetchrow(
        """
        SELECT 1
        FROM agent_runs
        WHERE id=$1::uuid AND user_id=$2::uuid AND runtime_session_id=$3::uuid
        """,
        run_id,
        actor_id,
        runtime_session_id,
    )
    if not row:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Run/session/actor binding mismatch.")


@router.post("/register", dependencies=[Depends(_require_host_bridge)])
async def register_bridge(req: RegisterBridgeRequest, user: dict = Depends(get_current_user)):
    try:
        actor_id = str(user["user_id"])
        if actor_id != str(req.actor_id):
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "actor_id mismatch.")
        await _assert_run_binding(
            run_id=req.run_id,
            actor_id=actor_id,
            runtime_session_id=req.runtime_session_id,
        )
        bridge = create_bridge(req.ide_type, req.workspace_root)
        resumed = get_bridge_registry().register(
            BridgeSession(
                run_id=req.run_id,
                runtime_session_id=req.runtime_session_id,
                actor_id=actor_id,
                ide_type=req.ide_type,
                workspace_root=str(Path(req.workspace_root).resolve()),
                workspace_fingerprint=req.workspace_fingerprint,
                bridge=bridge,
            )
        )
        await get_bridge_registry().publish_event(
            run_id=req.run_id,
            runtime_session_id=req.runtime_session_id,
            event={"type": "bridge_registered", "resumed": bool(resumed)},
        )
        return {"ok": True, "resumed": bool(resumed)}
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.post("/open-file", dependencies=[Depends(_require_host_bridge)])
async def ide_open_file(req: OpenFileRequest, user: dict = Depends(get_current_user)):
    try:
        actor_id = str(user["user_id"])
        await _assert_run_binding(
            run_id=req.run_id,
            actor_id=actor_id,
            runtime_session_id=req.runtime_session_id,
        )
        session = get_bridge_registry().get_for_actor(
            run_id=req.run_id,
            runtime_session_id=req.runtime_session_id,
            actor_id=actor_id,
        )
        if not session:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "No registered IDE bridge for session.")
        await session.bridge.open_file(req.path, req.line, req.col)
        await get_bridge_registry().publish_event(
            run_id=req.run_id,
            runtime_session_id=req.runtime_session_id,
            event={"type": "open_file", "path": req.path, "line": req.line, "col": req.col},
        )
        return {"ok": True}
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})
    except Exception as e:
        return JSONResponse(
            status_code=409,
            content={"error": APIErrorCode.EDITOR_OPEN_FAILED.value, "detail": str(e)},
        )


@router.post("/run-command", dependencies=[Depends(_require_host_bridge)])
async def ide_run_command(req: RunCommandRequest, user: dict = Depends(get_current_user)):
    try:
        actor_id = str(user["user_id"])
        await _assert_run_binding(
            run_id=req.run_id,
            actor_id=actor_id,
            runtime_session_id=req.runtime_session_id,
        )
        session = get_bridge_registry().get_for_actor(
            run_id=req.run_id,
            runtime_session_id=req.runtime_session_id,
            actor_id=actor_id,
        )
        if not session:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "No registered IDE bridge for session.")
        await session.bridge.run_command(req.command, req.terminal_name)
        await get_bridge_registry().publish_event(
            run_id=req.run_id,
            runtime_session_id=req.runtime_session_id,
            event={"type": "run_command", "command": req.command, "terminal_name": req.terminal_name},
        )
        return {"ok": True}
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})
    except Exception as e:
        return JSONResponse(
            status_code=409,
            content={"error": APIErrorCode.TOOL_EXECUTION_FAILED.value, "detail": str(e)},
        )


@router.post("/show-diff", dependencies=[Depends(_require_host_bridge)])
async def ide_show_diff(req: ShowDiffRequest, user: dict = Depends(get_current_user)):
    try:
        actor_id = str(user["user_id"])
        await _assert_run_binding(
            run_id=req.run_id,
            actor_id=actor_id,
            runtime_session_id=req.runtime_session_id,
        )
        session = get_bridge_registry().get_for_actor(
            run_id=req.run_id,
            runtime_session_id=req.runtime_session_id,
            actor_id=actor_id,
        )
        if not session:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "No registered IDE bridge for session.")
        result = await session.bridge.show_diff(
            req.path,
            req.new_contents,
            allow_partial_accept=req.allow_partial_accept,
        )
        await get_bridge_registry().publish_event(
            run_id=req.run_id,
            runtime_session_id=req.runtime_session_id,
            event={"type": "show_diff", "path": req.path, "hunks_total": int(result.get("hunks_total", 0))},
        )
        return result
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.post("/read-buffer", dependencies=[Depends(_require_host_bridge)])
async def ide_read_buffer(req: ReadBufferRequest, user: dict = Depends(get_current_user)):
    try:
        actor_id = str(user["user_id"])
        await _assert_run_binding(
            run_id=req.run_id,
            actor_id=actor_id,
            runtime_session_id=req.runtime_session_id,
        )
        session = get_bridge_registry().get_for_actor(
            run_id=req.run_id,
            runtime_session_id=req.runtime_session_id,
            actor_id=actor_id,
        )
        if not session:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "No registered IDE bridge for session.")
        return {"content": await session.bridge.read_buffer(req.path)}
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.post("/context")
async def ide_context(req: ContextRequest, user: dict = Depends(get_current_user)):
    if not _host_bridge_enabled():
        return {"open_files": [], "active_file": None, "cursor": None, "selection": None,
                "diagnostics": [], "terminal_history": []}
    try:
        actor_id = str(user["user_id"])
        await _assert_run_binding(
            run_id=req.run_id,
            actor_id=actor_id,
            runtime_session_id=req.runtime_session_id,
        )
        session = get_bridge_registry().get_for_actor(
            run_id=req.run_id,
            runtime_session_id=req.runtime_session_id,
            actor_id=actor_id,
        )
        if not session:
            return {
                "open_files": [],
                "active_file": None,
                "cursor": None,
                "selection": None,
                "diagnostics": [],
                "terminal_history": [],
            }
        return await session.bridge.get_context()
    except ContractError as e:
        return JSONResponse(status_code=_status_for_error(e.code), content={"error": e.code.value, "detail": e.detail})


@router.post("/message", dependencies=[Depends(_require_host_bridge)])
async def ide_message(req: IDEEnvelopeRequest, user: dict = Depends(get_current_user)):
    try:
        actor_id = str(user["user_id"])
        params = dict(req.params or {})
        run_id = str(params.get("run_id", ""))
        runtime_session_id = str(params.get("runtime_session_id", ""))
        await _assert_run_binding(
            run_id=run_id,
            actor_id=actor_id,
            runtime_session_id=runtime_session_id,
        )
        session = get_bridge_registry().get_for_actor(
            run_id=run_id,
            runtime_session_id=runtime_session_id,
            actor_id=actor_id,
        )
        if not session:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "No registered IDE bridge for session.")

        method = str(req.method or "").strip().lower()
        if method == "open_file":
            await session.bridge.open_file(params.get("path", ""), params.get("line"), params.get("col"))
            result: dict = {"ok": True}
        elif method == "run_command":
            await session.bridge.run_command(params.get("command", ""), params.get("terminal_name"))
            result = {"ok": True}
        elif method == "show_diff":
            result = await session.bridge.show_diff(
                params.get("path", ""),
                params.get("new_contents", ""),
                allow_partial_accept=bool(params.get("allow_partial_accept", True)),
            )
        elif method == "show_diff_interactive":
            interactive = getattr(session.bridge, "show_diff_interactive", None)
            if callable(interactive):
                result = await interactive(
                    params.get("path", ""),
                    params.get("new_contents", ""),
                    allow_partial_accept=bool(params.get("allow_partial_accept", True)),
                )
            else:
                result = await session.bridge.show_diff(
                    params.get("path", ""),
                    params.get("new_contents", ""),
                    allow_partial_accept=bool(params.get("allow_partial_accept", True)),
                )
        elif method == "read_buffer":
            result = {"content": await session.bridge.read_buffer(params.get("path", ""))}
        elif method == "get_context":
            result = await session.bridge.get_context()
        elif method == "get_open_files":
            result = {"open_files": await session.bridge.get_open_files()}
        else:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, f"Unsupported IDE method: {req.method}")

        await get_bridge_registry().publish_event(
            run_id=run_id,
            runtime_session_id=runtime_session_id,
            event={"type": "message", "method": method, "request_id": req.id},
        )
        return {"id": req.id, "type": "response", "result": result, "error": None}
    except ContractError as e:
        return JSONResponse(
            status_code=_status_for_error(e.code),
            content={"id": req.id, "type": "response", "result": None, "error": {"code": e.code.value, "detail": e.detail}},
        )


@router.get("/events/stream", dependencies=[Depends(_require_host_bridge)])
async def ide_events_stream(run_id: str, runtime_session_id: str, user: dict = Depends(get_current_user)):
    actor_id = str(user["user_id"])
    await _assert_run_binding(run_id=run_id, actor_id=actor_id, runtime_session_id=runtime_session_id)
    session = get_bridge_registry().get_for_actor(
        run_id=run_id,
        runtime_session_id=runtime_session_id,
        actor_id=actor_id,
    )
    if not session:
        return JSONResponse(
            status_code=409,
            content={"error": APIErrorCode.INVALID_TRANSITION.value, "detail": "No registered IDE bridge for session."},
        )

    async def _gen():
        async for event in get_bridge_registry().stream_events(
            run_id=run_id,
            runtime_session_id=runtime_session_id,
        ):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")
