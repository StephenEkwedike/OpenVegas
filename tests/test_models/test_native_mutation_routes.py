"""Strict authenticated transport only; no filesystem writes or paid providers."""
from __future__ import annotations

import sys
from types import ModuleType
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from openvegas.contracts.errors import APIErrorCode, ContractError
from server.routes import native_mutations as routes


@pytest.fixture
def boundary(monkeypatch):
    owner, run, preparation, tool = [str(uuid4()) for _ in range(4)]
    calls = []
    database = object()

    class Service:
        def __init__(self, db):
            assert db is database

        async def prepare(self, **kwargs):
            calls.append(("prepare", kwargs))
            return {"preparation_id": preparation, "observation_kind": "runtime_observed_file_v1"}

        async def approve(self, **kwargs):
            calls.append(("approve", kwargs))
            return {"approval_id": str(uuid4())}

    module = ModuleType("openvegas.agent.native_mutation_service")
    module.NativeMutationService = Service
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(routes, "get_db", lambda: database)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_current_user] = lambda: {"user_id": owner}
    payload = {
        "runtime_session_id": str(uuid4()), "expected_run_version": 3,
        "expected_valid_actions_signature": "sha256:" + "a" * 64,
        "idempotency_key": "prepare-" + str(uuid4()),
        "native_inference_request_id": str(uuid4()), "native_provider_call_id": "call-one",
        "observed_source": {"exists": True, "content_utf8": "first\r\nlast"},
    }
    return app, TestClient(app), owner, run, preparation, tool, payload, calls, Service


def test_prepare_preserves_literal_source_and_uses_authenticated_owner(boundary):
    _, client, owner, run, _, _, payload, calls, _ = boundary
    response = client.post(f"/agent/runs/{run}/native-mutations/prepare", json=payload)
    assert response.status_code == 200
    assert calls == [("prepare", {"user_id": owner, "run_id": run, **payload, "plan_mode": False})]
    assert "content_utf8" not in response.text


@pytest.mark.parametrize("extra", ["user_id", "patch", "original_call", "approval_required", "native_history", "target_path"])
def test_prepare_rejects_client_supplied_authority(boundary, extra):
    _, client, _, run, _, _, payload, calls, _ = boundary
    response = client.post(f"/agent/runs/{run}/native-mutations/prepare", json={**payload, extra: "injected"})
    assert response.status_code == 422 and not calls


@pytest.mark.parametrize("field,value", [
    ("expected_run_version", True), ("expected_run_version", "3"), ("expected_run_version", -1),
    ("runtime_session_id", "00000000-0000-0000-0000-000000000000"),
    ("native_inference_request_id", "not-a-uuid"), ("native_provider_call_id", ""),
    ("expected_valid_actions_signature", "sha256:"), ("idempotency_key", "bad\nkey"),
    ("plan_mode", "false"),
])
def test_prepare_does_not_coerce_invalid_fences(boundary, field, value):
    _, client, _, run, _, _, payload, calls, _ = boundary
    response = client.post(f"/agent/runs/{run}/native-mutations/prepare", json={**payload, field: value})
    assert response.status_code == 422 and not calls


@pytest.mark.parametrize("source", [
    {"exists": False, "content_utf8": ""}, {"exists": True, "content_utf8": None},
    {"exists": "true", "content_utf8": ""}, {"exists": True, "content_utf8": "x" * 32769},
    {"exists": True, "content_utf8": "x", "sha256": "a" * 64},
])
def test_missing_and_empty_sources_cannot_be_confused(boundary, source):
    _, client, _, run, _, _, payload, calls, _ = boundary
    response = client.post(f"/agent/runs/{run}/native-mutations/prepare", json={**payload, "observed_source": source})
    assert response.status_code == 422 and not calls


@pytest.mark.parametrize("source", [{"exists": False, "content_utf8": None}, {"exists": True, "content_utf8": ""}])
def test_valid_empty_and_missing_source_remain_distinct(boundary, source):
    _, client, _, run, _, _, payload, calls, _ = boundary
    response = client.post(f"/agent/runs/{run}/native-mutations/prepare", json={**payload, "observed_source": source})
    assert response.status_code == 200
    assert calls[0][1]["observed_source"] == source


def test_approval_only_forwards_explicit_fence_and_commitment(boundary):
    _, client, owner, run, preparation, tool, payload, calls, _ = boundary
    approval = {key: payload[key] for key in ("runtime_session_id", "expected_run_version", "expected_valid_actions_signature", "idempotency_key")}
    approval.update(tool_call_id=tool, contract_sha256="b" * 64)
    response = client.post(f"/agent/runs/{run}/native-mutations/{preparation}/approve", json=approval)
    assert response.status_code == 200
    assert calls == [("approve", {"user_id": owner, "run_id": run, "preparation_id": preparation, **approval})]
    response = client.post(f"/agent/runs/{run}/native-mutations/{preparation}/approve", json={**approval, "execute": True})
    assert response.status_code == 422 and len(calls) == 1


def test_unauthenticated_prepare_never_reaches_service(boundary):
    app, client, _, run, _, _, payload, calls, _ = boundary

    def reject():
        raise HTTPException(401, "Sign in required")

    app.dependency_overrides[routes.get_current_user] = reject
    response = client.post(f"/agent/runs/{run}/native-mutations/prepare", json=payload)
    assert response.status_code == 401 and not calls


def test_fixed_service_failure_does_not_echo_private_source(boundary, monkeypatch):
    _, client, _, run, _, _, payload, calls, service = boundary

    async def disabled(self, **_kwargs):
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Native mutations are disabled.")

    monkeypatch.setattr(service, "prepare", disabled)
    payload["observed_source"]["content_utf8"] = "private source fixture"
    response = client.post(f"/agent/runs/{run}/native-mutations/prepare", json=payload)
    assert response.status_code == 409 and response.json()["error"] == "invalid_transition"
    assert "private source fixture" not in response.text and not calls
    assert "private source fixture" not in repr(routes.PrepareNativeMutation.model_validate(payload))


@pytest.mark.parametrize("invalid", ["source_shape", "source_size", "extra", "invalid_json"])
def test_validation_errors_never_echo_rejected_private_source(boundary, invalid):
    _, client, _, run, _, _, payload, calls, _ = boundary
    private = "private_source_validation_marker"
    payload["observed_source"] = {"exists": False, "content_utf8": private}
    if invalid == "source_size":
        payload["observed_source"] = {"exists": True, "content_utf8": private * 2000}
    elif invalid == "extra":
        payload["observed_source"] = {"exists": True, "content_utf8": private, "unknown": private}
    if invalid == "invalid_json":
        response = client.post(f"/agent/runs/{run}/native-mutations/prepare",
                               content='{"observed_source": "' + private,
                               headers={"Content-Type": "application/json"})
    else:
        response = client.post(f"/agent/runs/{run}/native-mutations/prepare", json=payload)
    assert response.status_code == 422 and not calls
    assert response.json() == {"detail": "Invalid native mutation request."}
    assert private not in response.text
