"""Isolated HTTP prepare/confirm over real owned SQL, without public activation."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from server.routes import native_handoffs as routes
from tests.integration.test_native_handoff_service_postgres import (
    continuation_db as continuation_db,  # noqa: PLC0414
)
from tests.integration.test_native_handoff_service_postgres import (
    fresh_runtime_flags as fresh_runtime_flags,  # noqa: PLC0414
)
from tests.integration.test_native_handoff_service_postgres import (
    handoff_db as handoff_db,  # noqa: PLC0414
)
from tests.integration.test_native_handoff_store_postgres import destination
from tests.integration.test_openrouter_postgres import MODELS

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def client_for(c, monkeypatch, owner=None):
    monkeypatch.setattr(routes, "get_db", lambda: c.db)
    monkeypatch.setattr(routes, "get_fraud_engine", lambda: SimpleNamespace(check_inference=AsyncMock(return_value=True)))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_current_user] = lambda: {"user_id": owner or c.user}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://handoff-test") as client:
        yield client


def request_for(c):
    return {"source_scope": c.source_scope.model_dump(), "source_ref": c.source_ref.model_dump(),
            "selection": {"model": MODELS[1], "max_tokens": 100}, "idempotency_key": "http-preview"}


async def test_http_prepare_concurrent_confirmation_and_lost_ack_leave_one_commit(handoff_db, monkeypatch):
    c = handoff_db
    async with client_for(c, monkeypatch) as client:
        preview = await client.post("/agent/native-handoffs/prepare", json=request_for(c))
        assert preview.status_code == 200
        body = preview.json()
        assert body["destination_scope"] is None and body["task_count"] == 1
        assert "Keep the exact public task" not in preview.text
        dest = await destination(c)
        request = {key: body[key] for key in ("handoff_id", "handoff_sha256")}
        request.update(destination_scope=dest.model_dump(), idempotency_key="http-confirm")
        responses = await asyncio.wait_for(asyncio.gather(*(
            client.post("/agent/native-handoffs/confirm", json=request) for _ in range(3))), 5)
        assert all(response.status_code == 200 and response.json() == responses[0].json() for response in responses)
        assert responses[0].json()["destination_scope"] == dest.model_dump()
        # A repeated prepare after confirmation returns the same binding, not a
        # fresh preview or another destination. It never restarts a provider.
        replay = await client.post("/agent/native-handoffs/prepare", json=request_for(c))
        assert replay.status_code == 200 and replay.json() == responses[0].json()
    assert await c.db.fetchval("SELECT count(*) FROM native_task_handoffs") == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_requests") == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 1
    assert len(c.calls) == 1


@pytest.mark.parametrize("failure", ["stale", "foreign", "target", "gate"])
async def test_http_failed_confirmation_does_not_bind_destination(handoff_db, monkeypatch, failure):
    c = handoff_db
    async with client_for(c, monkeypatch) as client:
        body = (await client.post("/agent/native-handoffs/prepare", json=request_for(c))).json()
        dest = await destination(c)
        request = {key: body[key] for key in ("handoff_id", "handoff_sha256")}
        request.update(destination_scope=dest.model_dump(), idempotency_key="http-confirm")
        if failure == "stale":
            await c.db.execute("UPDATE agent_runs SET version=version+1 WHERE id=$1::uuid", c.source_scope.run_id)
        elif failure == "target":
            await c.db.execute("UPDATE provider_catalog SET enabled=false WHERE provider='openrouter' AND model_id=$1", MODELS[1])
        elif failure == "gate":
            monkeypatch.setenv("OPENVEGAS_NATIVE_TASK_HANDOFF", "0")
        if failure == "foreign":
            async with client_for(c, monkeypatch, owner=str(uuid4())) as foreign:
                response = await foreign.post("/agent/native-handoffs/confirm", json=request)
        else:
            response = await client.post("/agent/native-handoffs/confirm", json=request)
    assert response.status_code == 409
    assert response.headers["Cache-Control"] == "private, no-store"
    assert await c.db.fetchval("SELECT native_handoff_id FROM agent_runs WHERE id=$1::uuid", dest.run_id) is None
    assert await c.db.fetchval("SELECT count(*) FROM inference_requests") == 1
    assert len(c.calls) == 1


async def test_denied_shared_rate_gate_creates_no_preview_or_commit(handoff_db, monkeypatch):
    c = handoff_db
    async with client_for(c, monkeypatch) as client:
        limiter = AsyncMock(return_value=False)
        monkeypatch.setattr(routes, "get_fraud_engine", lambda: SimpleNamespace(check_inference=limiter))
        assert (await client.post("/agent/native-handoffs/prepare", json=request_for(c))).status_code == 429
        assert await c.db.fetchval("SELECT count(*) FROM native_task_handoffs") == 0
        limiter.return_value = True
        body = (await client.post("/agent/native-handoffs/prepare", json=request_for(c))).json()
        scope = await destination(c)
        limiter.return_value = False
        response = await client.post("/agent/native-handoffs/confirm", json={
            "handoff_id": body["handoff_id"], "handoff_sha256": body["handoff_sha256"],
            "destination_scope": scope.model_dump(), "idempotency_key": "rate-denied-confirm"})
        assert response.status_code == 429
    assert await c.db.fetchval("SELECT count(*) FROM native_task_handoffs") == 1
    assert await c.db.fetchval("SELECT destination_run_id FROM native_task_handoffs") is None
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 1
    assert len(c.calls) == 1
