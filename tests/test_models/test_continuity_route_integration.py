from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import test_provider_continuity_transactions as contracts
from fastapi import FastAPI
from test_provider_continuity_transactions import KEY, OTHER, USER

from openvegas.gateway.inference import InferenceResult
from server.routes import models
from server.services import dependencies


@pytest.fixture
def setup(monkeypatch):
    return contracts.setup.__wrapped__(monkeypatch)


@pytest.fixture
def app_setup(setup, monkeypatch):
    db, service, catalog = setup
    gateway = SimpleNamespace(infer=AsyncMock(return_value=InferenceResult("answer", 3, 2, completion_status="complete")))
    fraud = SimpleNamespace(check_inference=AsyncMock())
    mode = SimpleNamespace(resolve_for_user=AsyncMock(return_value={"effective_mode": "wrapper", "conversation_mode": "persistent"}))
    monkeypatch.setattr(models, "get_catalog", lambda: catalog)
    monkeypatch.setattr(dependencies, "get_provider_thread_service", lambda: service)
    monkeypatch.setattr(dependencies, "get_gateway", lambda: gateway)
    monkeypatch.setattr(dependencies, "get_fraud_engine", lambda: fraud)
    monkeypatch.setattr(dependencies, "get_llm_mode_service", lambda: mode)
    app = FastAPI()
    app.include_router(models.router)
    app.dependency_overrides[models.get_current_user] = lambda: {"user_id": USER}
    return app, db, gateway, fraud, mode


async def create(client):
    response = await client.post(
        "/models/conversations", json={"provider": "openai", "model": "reviewed-test"}
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["mistral", "gemini"])
async def test_route_create_ask_switch_ask_one_gateway_call_per_turn(app_setup, destination):
    app, _db, gateway, _, _ = app_setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await create(client)
        payload = {
            "provider": "openai",
            "model": "reviewed-test",
            "thread_id": created["thread_id"],
            "expected_revision": created["revision"],
            "prompt": "first",
            "idempotency_key": KEY,
        }
        response = await client.post("/models/conversations/ask", json=payload)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert gateway.infer.await_count == 1
        assert (await client.post("/models/conversations/ask", json=payload)).status_code == 409
        assert gateway.infer.await_count == 1
        choice = {
            "provider": destination,
            "model": "reviewed-test",
            "thread_id": created["thread_id"],
        }
        proposal = (await client.post("/models/switch", json=choice)).json()
        assert proposal["messages"] == []
        fork = await client.post(
            "/models/switch",
            json={**choice, "commit": True, "expected_revision": proposal["revision"]},
        )
        assert fork.status_code == 200
        fork = fork.json()
        assert fork["context_transferred"] and fork["thread_id"] != created["thread_id"]
        assert gateway.infer.await_count == 1
        second = await client.post(
            "/models/conversations/ask",
            json={
                **payload,
                "thread_id": fork["thread_id"],
                "expected_revision": fork["revision"],
                "provider": destination,
                "prompt": "second",
            },
        )
        assert second.status_code == 200 and gateway.infer.await_count == 2
        assert gateway.infer.call_args.args[0].messages == [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"api_key": "synthetic-client-key"},
        {"user_id": OTHER},
        {"messages": []},
        {"attachments": []},
        {"thoughtSignature": "synthetic"},
        {"thought_signature": "synthetic"},
        {"enable_tools": True},
        {"max_tokens": True},
    ],
)
async def test_route_rejects_byok_client_history_tool_fields_and_coercion(app_setup, extra):
    app, db, gateway, _, _ = app_setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/conversations", json={"provider": "openai", "model": "reviewed-test", **extra}
        )
        assert response.status_code == 422
        assert "synthetic-client-key" not in response.text
        assert response.headers["cache-control"] == "private, no-store"
    assert not db.writes
    gateway.infer.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_routes_require_auth(app_setup):
    app, _db, gateway, _, _ = app_setup
    app.dependency_overrides.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in ["/models/conversations", "/models/switch", "/models/conversations/ask"]:
            assert (await client.post(path, json={})).status_code == 401
    gateway.infer.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_source_and_no_transcript_in_plan_response(app_setup):
    app, _db, gateway, _, _ = app_setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await create(client)
        app.dependency_overrides[models.get_current_user] = lambda: {"user_id": OTHER}
        response = await client.post(
            "/models/switch",
            json={
                "provider": "mistral",
                "model": "reviewed-test",
                "thread_id": created["thread_id"],
            },
        )
        assert response.status_code == 409
        assert created["thread_id"] not in response.text
        assert response.headers["cache-control"] == "private, no-store"
    gateway.infer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["fraud", "byok", "secret", "tools", "attachments", "stale", "overbudget", "ephemeral"],
)
async def test_invalid_turn_before_paid_call(app_setup, failure):
    app, _db, gateway, fraud, mode = app_setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await create(client)
        payload = {
            "provider": "openai",
            "model": "reviewed-test",
            "thread_id": created["thread_id"],
            "expected_revision": created["revision"],
            "prompt": "hello",
            "idempotency_key": KEY,
        }
        if failure == "fraud":
            fraud.check_inference.side_effect = RuntimeError("synthetic rate limit")
        elif failure == "byok":
            mode.resolve_for_user.return_value = {"effective_mode": "byok"}
        elif failure == "ephemeral":
            mode.resolve_for_user.return_value = {
                "effective_mode": "wrapper",
                "conversation_mode": "ephemeral",
            }
        elif failure == "secret":
            payload["prompt"] = "sk-" + "a" * 30
        elif failure == "tools":
            payload["enable_tools"] = True
        elif failure == "attachments":
            payload["attachments"] = ["file-id"]
        elif failure == "stale":
            payload["expected_revision"] = "0" * 64
        else:
            payload["max_tokens"] = 1025
        response = await client.post("/models/conversations/ask", json=payload)
        assert response.status_code in {400, 409, 422, 429}
    gateway.infer.assert_not_awaited()
