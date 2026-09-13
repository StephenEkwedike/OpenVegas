"""ASGI-only host bridge gating tests; never launch an editor or read host files."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from server.routes import ide_bridge

ACTOR = "synthetic-actor"
BINDING = {"run_id": "synthetic-run", "runtime_session_id": "synthetic-session"}
EMPTY_CONTEXT = {"open_files": [], "active_file": None, "cursor": None, "selection": None, "diagnostics": [], "terminal_history": []}
OPERATIONS = (
    ("POST", "/ide/register", {**BINDING, "actor_id": ACTOR, "ide_type": "vscode", "workspace_root": "/synthetic/workspace", "workspace_fingerprint": "synthetic-fingerprint"}),
    ("POST", "/ide/open-file", {**BINDING, "path": "/synthetic/host-only.txt"}),
    ("POST", "/ide/run-command", {**BINDING, "command": "synthetic-command"}),
    ("POST", "/ide/show-diff", {**BINDING, "path": "/synthetic/host-only.txt", "new_contents": "synthetic"}),
    ("POST", "/ide/read-buffer", {**BINDING, "path": "/synthetic/host-only.txt"}),
    ("POST", "/ide/message", {"id": "synthetic-message", "method": "read_buffer", "params": {**BINDING, "path": "/synthetic/host-only.txt"}}),
    ("GET", "/ide/events/stream", BINDING),
)


@pytest.fixture
def app(monkeypatch):
    application = FastAPI()
    application.include_router(ide_bridge.router)
    application.dependency_overrides[ide_bridge.get_current_user] = lambda: {"user_id": ACTOR, "role": "authenticated"}

    def forbidden(*args, **kwargs):
        pytest.fail("Disabled host bridge reached backend resources")

    monkeypatch.setattr(ide_bridge, "create_bridge", forbidden)
    monkeypatch.setattr(ide_bridge, "get_db", forbidden)
    monkeypatch.setattr(ide_bridge, "get_bridge_registry", forbidden)
    return application


async def request(app, method, path, payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://asgi-only") as client:
        return await client.request(method, path, **({"params": payload} if method == "GET" else {"json": payload}))


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["prod", "production", "PRODUCTION"])
@pytest.mark.parametrize("method,path,payload", OPERATIONS)
async def test_production_cannot_reenable_host_operations_with_flag(app, monkeypatch, runtime, method, path, payload):
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", runtime)
    monkeypatch.setenv("OPENVEGAS_ENABLE_HOST_IDE_BRIDGE", "1")
    response = await request(app, method, path, payload)
    assert response.status_code == 503
    assert "local CLI" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,payload", OPERATIONS)
async def test_local_explicit_disable_blocks_host_operations(app, monkeypatch, method, path, payload):
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", "development")
    monkeypatch.setenv("OPENVEGAS_ENABLE_HOST_IDE_BRIDGE", "0")
    response = await request(app, method, path, payload)
    assert response.status_code == 503


@pytest.mark.parametrize("name,value", [("OPENVEGAS_RUNTIME_ENV", " production "), ("OPENVEGAS_RUNTIME_ENV", " PROD "), ("ENV", "production"), ("ENV", " production ")])
def test_production_detection_matches_runtime_name_normalization(monkeypatch, name, value):
    monkeypatch.delenv("OPENVEGAS_RUNTIME_ENV", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv(name, value)
    monkeypatch.setenv("OPENVEGAS_ENABLE_HOST_IDE_BRIDGE", "1")
    assert ide_bridge._host_bridge_enabled() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime,flag", [("production", "1"), ("prod", "1"), ("development", "0")])
async def test_disabled_context_is_empty_without_database_or_bridge_access(app, monkeypatch, runtime, flag):
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", runtime)
    monkeypatch.setenv("OPENVEGAS_ENABLE_HOST_IDE_BRIDGE", flag)
    response = await request(app, "POST", "/ide/context", BINDING)
    assert response.status_code == 200
    assert response.json() == EMPTY_CONTEXT


@pytest.mark.asyncio
async def test_disabled_context_still_requires_authentication(app, monkeypatch):
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", "production")
    application = FastAPI()
    application.include_router(ide_bridge.router)
    response = await request(application, "POST", "/ide/context", BINDING)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_local_enabled_context_retains_existing_adapter_contract(app, monkeypatch):
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", "development")
    monkeypatch.setenv("OPENVEGAS_ENABLE_HOST_IDE_BRIDGE", "1")
    binding = AsyncMock()
    context = {**EMPTY_CONTEXT, "open_files": ["synthetic-buffer"]}
    bridge = SimpleNamespace(get_context=AsyncMock(return_value=context))
    registry = SimpleNamespace(get_for_actor=lambda **kwargs: SimpleNamespace(bridge=bridge))
    monkeypatch.setattr(ide_bridge, "_assert_run_binding", binding)
    monkeypatch.setattr(ide_bridge, "get_bridge_registry", lambda: registry)
    response = await request(app, "POST", "/ide/context", BINDING)
    assert response.status_code == 200
    assert response.json() == context
    binding.assert_awaited_once_with(**BINDING, actor_id=ACTOR)
    bridge.get_context.assert_awaited_once()
