"""MCP connector registry routes."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from openvegas.flags import features
from openvegas.telemetry import emit_metric
from server.middleware.auth import get_current_user
from server.services.dependencies import get_mcp_registry_service

router = APIRouter()


class MCPRegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    transport: Literal["stdio", "streamable-http", "websocket"]
    target: str = Field(min_length=1, max_length=4096)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


def _mcp_enabled() -> bool:
    return bool(features().get("mcp", False))


@router.get("/mcp/servers")
async def mcp_servers(user: dict = Depends(get_current_user)):
    if not _mcp_enabled():
        return JSONResponse(status_code=503, content={"error": "feature_disabled", "detail": "mcp disabled"})
    svc = get_mcp_registry_service()
    rows = await svc.list_servers(user_id=str(user["user_id"]))
    return {"servers": rows}


@router.post("/mcp/servers/register")
async def mcp_register(req: MCPRegisterRequest, user: dict = Depends(get_current_user)):
    if not _mcp_enabled():
        return JSONResponse(status_code=503, content={"error": "feature_disabled", "detail": "mcp disabled"})
    svc = get_mcp_registry_service()
    try:
        rec = await svc.register_server(
            user_id=str(user["user_id"]),
            name=req.name,
            transport=req.transport,
            target=req.target,
            metadata=req.metadata,
        )
    except PermissionError:
        emit_metric("mcp_register_denied_total", {"reason": "allowlist"})
        return JSONResponse(
            status_code=403,
            content={"error": "mcp_target_not_allowlisted", "detail": "MCP target not allowlisted"},
        )
    except ValueError:
        emit_metric("mcp_register_denied_total", {"reason": "transport"})
        return JSONResponse(
            status_code=400,
            content={"error": "unsupported_transport", "detail": "Unsupported MCP transport"},
        )
    emit_metric("mcp_register_total", {"transport": req.transport})
    return {
        "server": {
            "id": rec.id,
            "name": rec.name,
            "transport": rec.transport,
            "target": rec.target,
            "metadata": rec.metadata,
            "created_at": rec.created_at,
        }
    }


@router.get("/mcp/servers/{server_id}/health")
async def mcp_server_health(server_id: str, user: dict = Depends(get_current_user)):
    if not _mcp_enabled():
        return JSONResponse(status_code=503, content={"error": "feature_disabled", "detail": "mcp disabled"})
    svc = get_mcp_registry_service()
    try:
        result = await svc.health(user_id=str(user["user_id"]), server_id=server_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"error": "not_found", "detail": "MCP server not found"})
    emit_metric("mcp_health_check_total", {"status": str(result.get("status", "unknown"))})
    return result


class MCPCallToolRequest(BaseModel):
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_sec: int = Field(default=20, ge=1, le=120)

    model_config = ConfigDict(extra="forbid")


@router.get("/mcp/servers/{server_id}/tools")
async def mcp_list_tools(server_id: str, timeout_sec: int = 20, user: dict = Depends(get_current_user)):
    if not _mcp_enabled():
        return JSONResponse(status_code=503, content={"error": "feature_disabled", "detail": "mcp disabled"})
    svc = get_mcp_registry_service()
    try:
        result = await svc.list_tools(
            user_id=str(user["user_id"]),
            server_id=server_id,
            timeout_sec=timeout_sec,
        )
    except KeyError:
        emit_metric("mcp_tool_list_total", {"outcome": "failure", "reason": "not_found"})
        return JSONResponse(status_code=404, content={"error": "not_found", "detail": "MCP server not found"})
    except PermissionError as exc:
        emit_metric("mcp_tool_list_total", {"outcome": "failure", "reason": "auth"})
        return JSONResponse(status_code=403, content={"error": "mcp_auth_failed", "detail": str(exc)})
    except TimeoutError as exc:
        emit_metric("mcp_tool_list_total", {"outcome": "failure", "reason": "timeout"})
        return JSONResponse(status_code=504, content={"error": "mcp_timeout", "detail": str(exc)})
    except RuntimeError as exc:
        emit_metric("mcp_tool_list_total", {"outcome": "failure", "reason": "runtime"})
        return JSONResponse(status_code=502, content={"error": "mcp_tool_list_failed", "detail": str(exc)})
    emit_metric(
        "mcp_tool_list_total",
        {"outcome": "success", "transport": str(result.get("transport", "unknown"))},
    )
    return result


@router.post("/mcp/servers/{server_id}/tools/call")
async def mcp_call_tool(server_id: str, req: MCPCallToolRequest, user: dict = Depends(get_current_user)):
    if not _mcp_enabled():
        return JSONResponse(status_code=503, content={"error": "feature_disabled", "detail": "mcp disabled"})
    svc = get_mcp_registry_service()
    try:
        result = await svc.call_tool(
            user_id=str(user["user_id"]),
            server_id=server_id,
            tool=req.tool,
            arguments=req.arguments,
            timeout_sec=req.timeout_sec,
        )
    except KeyError:
        emit_metric("mcp_tool_call_total", {"outcome": "failure", "reason": "not_found"})
        return JSONResponse(status_code=404, content={"error": "not_found", "detail": "MCP server not found"})
    except NotImplementedError as exc:
        emit_metric("mcp_tool_call_total", {"outcome": "failure", "reason": "not_implemented"})
        return JSONResponse(status_code=501, content={"error": str(exc), "detail": str(exc)})
    except ValueError as exc:
        emit_metric("mcp_tool_call_total", {"outcome": "failure", "reason": "invalid_request"})
        return JSONResponse(status_code=400, content={"error": str(exc), "detail": str(exc)})
    except PermissionError as exc:
        emit_metric("mcp_tool_call_total", {"outcome": "failure", "reason": "auth"})
        return JSONResponse(status_code=403, content={"error": "mcp_auth_failed", "detail": str(exc)})
    except TimeoutError as exc:
        emit_metric("mcp_tool_call_total", {"outcome": "failure", "reason": "timeout"})
        return JSONResponse(status_code=504, content={"error": "mcp_timeout", "detail": str(exc)})
    except RuntimeError as exc:
        emit_metric("mcp_tool_call_total", {"outcome": "failure", "reason": "runtime"})
        return JSONResponse(status_code=502, content={"error": "mcp_tool_call_failed", "detail": str(exc)})
    emit_metric(
        "mcp_tool_call_total",
        {"outcome": "success", "transport": str(result.get("transport", "unknown"))},
    )
    return result
