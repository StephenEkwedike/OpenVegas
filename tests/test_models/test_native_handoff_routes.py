"""Handoff HTTP boundaries in an isolated app, never live inference or funds."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.contracts.native_handoff import NativeHandoffResponse
from openvegas.contracts.native_scope import NativeInferenceScope
from server.routes import native_handoffs as routes
from server.services.native_handoff_service import HandoffPreview, HandoffSelection

PRIVATE = "private-rejected-history-must-not-be-reflected"
FLAGS = ("OPENVEGAS_NATIVE_TASK_HANDOFF", "OPENVEGAS_NATIVE_GENERATION_SCOPE",
         "OPENVEGAS_NATIVE_GENERATION_HISTORY")


@pytest.fixture
def boundary(monkeypatch):
    for name in FLAGS:
        monkeypatch.setenv(name, "1")
    owner, handoff = str(uuid4()), str(uuid4())
    source = NativeInferenceScope(run_id=str(uuid4()), runtime_session_id=str(uuid4()),
        expected_run_version=7, expected_valid_actions_signature="sha256:" + "a" * 64)
    dest = source.model_copy(update={"run_id": str(uuid4()), "runtime_session_id": str(uuid4())})
    preview = HandoffPreview(handoff, "b" * 64, HandoffSelection("fixture/exact-v1", max_tokens=100),
        datetime.now(UTC) + timedelta(minutes=5), 2, 3, 2, 4)
    calls, database = [], object()

    class Service:
        prepared = preview
        confirmed = replace(preview, destination_scope=dest)

        def __init__(self, db):
            assert db is database

        async def prepare(self, **kwargs):
            calls.append(("prepare", kwargs))
            return self.prepared

        async def confirm(self, **kwargs):
            calls.append(("confirm", kwargs))
            return self.confirmed

    monkeypatch.setattr(routes, "NativeHandoffService", Service)
    monkeypatch.setattr(routes, "get_db", lambda: database)
    monkeypatch.setattr(routes, "get_fraud_engine", lambda: SimpleNamespace(check_inference=AsyncMock(return_value=True)))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_current_user] = lambda: {"user_id": owner}
    prepare = {"source_scope": source.model_dump(),
        "source_ref": {"previous_inference_request_id": str(uuid4()), "expected_history_revision": 2},
        "selection": {"model": preview.selection.model, "max_tokens": 100}, "idempotency_key": "prepare-once"}
    confirm = {"handoff_id": handoff, "handoff_sha256": preview.handoff_sha256,
               "destination_scope": dest.model_dump(), "idempotency_key": "confirm-once"}
    return app, TestClient(app), owner, prepare, confirm, calls, Service


@pytest.mark.parametrize("operation", ["prepare", "confirm"])
def test_owned_typed_summary_only_and_no_store_cache(boundary, operation):
    _, client, owner, prepare, confirm, calls, _ = boundary
    request = prepare if operation == "prepare" else confirm
    response = client.post("/agent/native-handoffs/" + operation, json=request)
    assert response.status_code == 200
    parsed = NativeHandoffResponse.model_validate(response.json())
    assert parsed.task_count == 2 and parsed.file_count == 3 and parsed.unique_file_count == 2
    assert response.headers["Cache-Control"] == "private, no-store"
    assert calls[0][0] == operation and calls[0][1]["user_id"] == owner
    assert calls[0][1]["idempotency_key"] == request["idempotency_key"]
    assert request["idempotency_key"] not in response.text
    assert owner not in response.text
    assert "document" not in response.text and "observations" not in response.text
    assert (parsed.destination_scope is None) == (operation == "prepare")


@pytest.mark.parametrize("operation", ["prepare", "confirm"])
@pytest.mark.parametrize("outcome", [False, None, 1, "true", RuntimeError(PRIVATE)])
def test_rate_gate_denial_is_private_and_precedes_storage(boundary, monkeypatch, operation, outcome):
    _, client, owner, prepare, confirm, calls, _ = boundary
    limiter = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
    monkeypatch.setattr(routes, "get_fraud_engine", lambda: SimpleNamespace(check_inference=limiter))
    monkeypatch.setattr(routes, "get_db", lambda: pytest.fail("Denied request accessed storage"))
    response = client.post("/agent/native-handoffs/" + operation, json=prepare if operation == "prepare" else confirm)
    limiter.assert_awaited_once_with(owner)
    assert response.status_code == 429 and not calls
    assert response.json()["error"] == "rate_limited"
    assert PRIVATE not in response.text
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize("operation", ["prepare", "confirm"])
def test_rate_gate_uses_shared_owned_inference_key(boundary, monkeypatch, operation):
    _, client, owner, prepare, confirm, _, _ = boundary
    limiter = AsyncMock(return_value=True)
    monkeypatch.setattr(routes, "get_fraud_engine", lambda: SimpleNamespace(check_inference=limiter))
    assert client.post("/agent/native-handoffs/" + operation,
                       json=prepare if operation == "prepare" else confirm).status_code == 200
    limiter.assert_awaited_once_with(owner)


def test_cancelled_rate_gate_is_not_converted_into_rate_denial(boundary, monkeypatch):
    _, _, owner, prepare, _, calls, _ = boundary
    limiter = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(routes, "get_fraud_engine", lambda: SimpleNamespace(check_inference=limiter))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(routes.prepare_native_handoff(routes.PrepareNativeHandoff.model_validate(prepare),
                                                 {"user_id": owner}))
    assert not calls


def test_expired_commit_replay_retains_original_identity_without_renewal(boundary):
    _, client, _, _, confirm, _, service = boundary
    service.confirmed = replace(service.confirmed, expires_at=datetime(2000, 1, 1, tzinfo=UTC))
    first = client.post("/agent/native-handoffs/confirm", json=confirm)
    second = client.post("/agent/native-handoffs/confirm", json=confirm)
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    assert first.json()["expires_at"].startswith("2000-01-01")


def test_prepare_replay_after_commit_returns_existing_destination(boundary):
    _, client, _, prepare, _, _, service = boundary
    service.prepared = service.confirmed
    response = client.post("/agent/native-handoffs/prepare", json=prepare)
    assert response.status_code == 200
    assert response.json()["destination_scope"] == service.confirmed.destination_scope.model_dump()


@pytest.mark.parametrize("operation", ["prepare", "confirm"])
@pytest.mark.parametrize("name", FLAGS)
def test_each_disabled_gate_prevents_database_access(boundary, monkeypatch, operation, name):
    _, client, _, prepare, confirm, calls, _ = boundary
    monkeypatch.delenv(name)
    monkeypatch.setattr(routes, "get_db", lambda: pytest.fail("disabled route touched database"))
    response = client.post("/agent/native-handoffs/" + operation, json=prepare if operation == "prepare" else confirm)
    assert response.status_code == 409 and not calls


@pytest.mark.parametrize("operation", ["prepare", "confirm"])
@pytest.mark.parametrize("extra", ["user_id", "document", "history", "claim", "target", PRIVATE])
def test_client_authority_and_history_rejected_without_reflection(boundary, operation, extra):
    _, client, _, prepare, confirm, calls, _ = boundary
    request = prepare if operation == "prepare" else confirm
    response = client.post("/agent/native-handoffs/" + operation, json={**request, extra: PRIVATE})
    assert response.status_code == 422 and not calls and PRIVATE not in response.text
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize("operation", ["prepare", "confirm"])
def test_invalid_json_never_reflected(boundary, operation):
    _, client, _, _, _, calls, _ = boundary
    response = client.post("/agent/native-handoffs/" + operation, content='{"bad":"' + PRIVATE,
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and not calls and PRIVATE not in response.text


@pytest.mark.parametrize("operation", ["prepare", "confirm"])
def test_authentication_required_before_service(boundary, operation):
    app, client, _, prepare, confirm, calls, _ = boundary

    def denied():
        raise HTTPException(401, "Sign in required")

    app.dependency_overrides[routes.get_current_user] = denied
    response = client.post("/agent/native-handoffs/" + operation, json=prepare if operation == "prepare" else confirm)
    assert response.status_code == 401 and not calls


@pytest.mark.parametrize("operation", ["prepare", "confirm"])
@pytest.mark.parametrize("error", [RuntimeError(PRIVATE), ContractError(APIErrorCode.HANDOFF_BLOCKED, PRIVATE),
                                  ContractError(APIErrorCode.STALE_PROJECTION, PRIVATE)])
def test_private_service_diagnostics_never_reflected(boundary, monkeypatch, operation, error):
    _, client, _, prepare, confirm, _, service = boundary

    async def fail(self, **_kwargs):
        raise error

    monkeypatch.setattr(service, operation, fail)
    response = client.post("/agent/native-handoffs/" + operation, json=prepare if operation == "prepare" else confirm)
    assert response.status_code == 409 and PRIVATE not in response.text
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize("field,value", [("handoff_id", str(uuid4())), ("handoff_sha256", "c" * 64),
    ("destination_scope", None), ("file_count", 15), ("observation_count", -1)])
def test_invalid_or_mismatched_confirmation_never_advertised(boundary, field, value):
    _, client, _, _, confirm, _, service = boundary
    service.confirmed = replace(service.confirmed, **{field: value})
    assert client.post("/agent/native-handoffs/confirm", json=confirm).status_code == 409


def test_cancellation_is_not_converted_into_recoverable_response(boundary, monkeypatch):
    _, _, owner, prepare, _, _, service = boundary

    async def cancel(self, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "prepare", cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(routes.prepare_native_handoff(routes.PrepareNativeHandoff.model_validate(prepare),
                                                 {"user_id": owner}))


@pytest.mark.parametrize("operation", ["prepare", "confirm"])
@pytest.mark.parametrize("declared", [True, False])
def test_body_limit_precedes_parsing_authentication_and_disabled_gate(boundary, monkeypatch, operation, declared):
    app, _, _, _, _, calls, _ = boundary
    monkeypatch.setenv(FLAGS[0], "0")
    app.dependency_overrides[routes.get_current_user] = lambda: pytest.fail("oversized body reached auth")
    raw = ('{"unknown":"' + PRIVATE * 1000 + '"}').encode()

    async def check():
        async def chunks():
            for index in range(0, len(raw), 4096):
                yield raw[index:index + 4096]

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture.invalid") as client:
            return await client.post("/agent/native-handoffs/" + operation,
                content=raw if declared else chunks(), headers={"Content-Type": "application/json"})

    response = asyncio.run(check())
    assert response.status_code == 413 and not calls and PRIVATE not in response.text
    assert response.headers["Cache-Control"] == "private, no-store"


def test_exact_body_limit_valid_json_is_accepted(boundary):
    _, client, _, prepare, _, _, _ = boundary
    raw = json.dumps(prepare).encode()
    raw += b" " * (routes.MAX_BODY_BYTES - len(raw))
    assert client.post("/agent/native-handoffs/prepare", content=raw,
                       headers={"Content-Type": "application/json"}).status_code == 200


@pytest.mark.parametrize("disabled", [None, *FLAGS])
@pytest.mark.parametrize("operation", ["prepare", "confirm"])
def test_registered_router_is_default_off_before_database_access(boundary, monkeypatch, disabled, operation):
    from server.main import app
    _, _, owner, prepare, confirm, calls, _ = boundary
    for flag in FLAGS:
        if disabled is None:
            monkeypatch.delenv(flag, raising=False)
        else:
            monkeypatch.setenv(flag, "0" if flag == disabled else "1")
    def forbidden_database():
        pytest.fail("Disabled handoff accessed the database")
    monkeypatch.setattr(routes, "get_db", forbidden_database)
    monkeypatch.setattr(routes, "get_fraud_engine", lambda: pytest.fail("Disabled handoff reached rate gate"))
    monkeypatch.setitem(app.dependency_overrides, routes.get_current_user, lambda: {"user_id": owner})
    response = TestClient(app).post("/agent/native-handoffs/" + operation,
                                   json=prepare if operation == "prepare" else confirm)
    assert response.status_code == 409 and not calls
    assert response.json()["error"] == APIErrorCode.HANDOFF_BLOCKED.value
    assert response.headers["Cache-Control"] == "private, no-store"
