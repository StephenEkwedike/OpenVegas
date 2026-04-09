from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.middleware.auth import get_current_user
from server.routes import mcp as mcp_routes


def _app_with_router() -> FastAPI:
    app = FastAPI()
    app.include_router(mcp_routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u-1"}
    return app


class _StubMCPService:
    async def list_servers(self, *, user_id: str):
        assert user_id == "u-1"
        return [{"id": "s1", "name": "local", "transport": "stdio", "target": "python3"}]

    async def register_server(self, *, user_id: str, name: str, transport: str, target: str, metadata: dict):
        assert user_id == "u-1"
        return type("Rec", (), {"id": "s1", "name": name, "transport": transport, "target": target, "metadata": metadata, "created_at": "now"})

    async def health(self, *, user_id: str, server_id: str):
        assert user_id == "u-1"
        assert server_id == "s1"
        return {"server_id": "s1", "status": "ok", "transport": "stdio", "detail": "binary_found"}

    async def call_tool(self, *, user_id: str, server_id: str, tool: str, arguments: dict, timeout_sec: int):
        assert user_id == "u-1"
        assert server_id == "s1"
        assert tool == "ping"
        assert arguments == {"x": 1}
        assert timeout_sec == 9
        return {"server_id": "s1", "transport": "stdio", "tool": "ping", "result": {"ok": True}}

    async def list_tools(self, *, user_id: str, server_id: str, timeout_sec: int):
        assert user_id == "u-1"
        assert server_id == "s1"
        assert timeout_sec == 7
        return {
            "server_id": "s1",
            "transport": "stdio",
            "tools": [{"name": "ping", "description": "health ping"}],
        }


def test_mcp_routes_success(monkeypatch):
    monkeypatch.setattr(mcp_routes, "_mcp_enabled", lambda: True)
    monkeypatch.setattr(mcp_routes, "get_mcp_registry_service", lambda: _StubMCPService())
    client = TestClient(_app_with_router())

    list_resp = client.get("/mcp/servers")
    assert list_resp.status_code == 200
    assert len(list_resp.json()["servers"]) == 1

    reg_resp = client.post(
        "/mcp/servers/register",
        json={"name": "local", "transport": "stdio", "target": "python3", "metadata": {}},
    )
    assert reg_resp.status_code == 200
    assert reg_resp.json()["server"]["id"] == "s1"

    health_resp = client.get("/mcp/servers/s1/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "ok"

    tools_resp = client.get("/mcp/servers/s1/tools?timeout_sec=7")
    assert tools_resp.status_code == 200
    assert tools_resp.json()["tools"][0]["name"] == "ping"

    call_resp = client.post("/mcp/servers/s1/tools/call", json={"tool": "ping", "arguments": {"x": 1}, "timeout_sec": 9})
    assert call_resp.status_code == 200
    assert call_resp.json()["result"]["ok"] is True


def test_mcp_feature_disabled(monkeypatch):
    monkeypatch.setattr(mcp_routes, "_mcp_enabled", lambda: False)
    client = TestClient(_app_with_router())
    resp = client.get("/mcp/servers")
    assert resp.status_code == 503
    assert resp.json()["error"] == "feature_disabled"


class _TimeoutMCPService(_StubMCPService):
    async def call_tool(self, *, user_id: str, server_id: str, tool: str, arguments: dict, timeout_sec: int):
        raise TimeoutError("mcp_timeout")

    async def list_tools(self, *, user_id: str, server_id: str, timeout_sec: int):
        raise TimeoutError("mcp_timeout")


class _AuthMCPService(_StubMCPService):
    async def call_tool(self, *, user_id: str, server_id: str, tool: str, arguments: dict, timeout_sec: int):
        raise PermissionError("mcp_auth_failed:403")

    async def list_tools(self, *, user_id: str, server_id: str, timeout_sec: int):
        raise PermissionError("mcp_auth_failed:403")


def test_mcp_call_timeout_maps_504(monkeypatch):
    monkeypatch.setattr(mcp_routes, "_mcp_enabled", lambda: True)
    monkeypatch.setattr(mcp_routes, "get_mcp_registry_service", lambda: _TimeoutMCPService())
    client = TestClient(_app_with_router())
    resp = client.post("/mcp/servers/s1/tools/call", json={"tool": "ping", "arguments": {}, "timeout_sec": 9})
    assert resp.status_code == 504
    assert resp.json()["error"] == "mcp_timeout"


def test_mcp_auth_error_maps_403(monkeypatch):
    monkeypatch.setattr(mcp_routes, "_mcp_enabled", lambda: True)
    monkeypatch.setattr(mcp_routes, "get_mcp_registry_service", lambda: _AuthMCPService())
    client = TestClient(_app_with_router())
    resp_tools = client.get("/mcp/servers/s1/tools?timeout_sec=7")
    assert resp_tools.status_code == 403
    assert resp_tools.json()["error"] == "mcp_auth_failed"
    resp_call = client.post("/mcp/servers/s1/tools/call", json={"tool": "ping", "arguments": {}, "timeout_sec": 9})
    assert resp_call.status_code == 403
    assert resp_call.json()["error"] == "mcp_auth_failed"
