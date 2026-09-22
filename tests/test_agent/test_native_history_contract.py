from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openvegas.agent.native_history import (
    KIND,
    accepted_native_receipts_tx,
    bind_native_call_tx,
    object_value,
    require_call_id,
    require_uuid,
    safe_receipt_content,
)
from openvegas.agent.orchestration_service import AgentOrchestrationService
from openvegas.agent.runtime_contracts import result_submission_hash, tool_payload_hash
from openvegas.contracts.errors import ContractError
from openvegas.gateway.inference import AIGateway, InferenceResult
from server.routes import agent_orchestration as agent_routes
from server.routes.inference import _native_tool_references

REQUEST, RUN, USER, SESSION, TOOL = [str(uuid.uuid4()) for _ in range(5)]
MODEL = "openai/gpt-4.1-nano"
NORMALIZE = AgentOrchestrationService._normalize_tool_arguments
RUN_ROW = {"id": RUN, "user_id": USER, "runtime_session_id": SESSION, "state": "running"}
CALL = {"tool_name": "Read", "arguments": {"filepath": "notes.txt"},
        "shell_mode": "read_only", "timeout_sec": 30, "provider_call_id": "call-1"}


class Tx:
    def __init__(self):
        self.body = {"provider_request_id": "gen-1", "tool_calls": [copy.deepcopy(CALL)]}
        self.source = {"status": "succeeded", "response_status": 200,
                       "response_body_text": self.body, "provider_request_id": "gen-1"}
        self.preauth = {"provider": "openrouter", "model_id": MODEL, "status": "settled"}
        self.previous = None
        self.count = 0
        self.binding = None
        self.tool = None
        self.callback = True
        self.queries = []

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if "inference_requests" in query:
            assert "user_id=$2" in query and "FOR UPDATE" in query
            return self.source
        if "inference_preauthorizations" in query:
            assert "request_id=$1::uuid" not in query
            return self.preauth
        assert "agent_run_tool_calls" in query and "run_id=$2" in query
        return self.tool

    async def fetchval(self, query, *args):
        self.queries.append((query, args))
        if "count(*)" in query:
            return self.count
        if "agent_run_events" in query:
            assert "runtime_callback" in query
            assert "payload->'redaction_checked'='true'::jsonb" in query
            assert "payload->'redaction_required'='false'::jsonb" in query
            return "event" if self.callback else None
        return self.previous

    async def execute(self, query, *args):
        self.queries.append((query, args))
        assert "INSERT INTO agent_chat_turns" in query
        self.binding = json.loads(args[1])
        return "INSERT 0 1"

    async def fetch(self, query, *args):
        assert "ORDER BY turn_no LIMIT" in query
        return [{"content_json": self.binding, "tool_call_id": TOOL}]


async def bind(tx, **overrides):
    args = {"tx": tx, "run": RUN_ROW, "inference_request_id": REQUEST, "provider_call_id": "call-1",
            "tool_call_id": TOOL, "tool_name": "fs_read",
            "arguments": {"filepath": "notes.txt", "path": "notes.txt"}, "shell_mode": "read_only",
            "timeout_sec": 5, "normalize": NORMALIZE}
    args.update(overrides)
    await bind_native_call_tx(**args)


@pytest.mark.asyncio
async def test_server_binding_and_accepted_reload_use_stored_ids_and_results():
    tx = Tx()
    await bind(tx)
    assert tx.binding["native_call"] == CALL
    assert tx.binding["kind"] == KIND
    assert tx.binding["runtime_timeout_sec"] == 5
    empty = hashlib.sha256(b"").hexdigest()
    digest = result_submission_hash(result_status="succeeded", result_payload={"content": "Hello"},
                                    stdout_sha256=empty, stderr_sha256=empty)
    tx.tool = {"payload_hash": tool_payload_hash("fs_read", {"filepath": "notes.txt", "path": "notes.txt"}, "read_only"),
               "request_payload_json": {"timeout_sec": 5, "tool_name": "fs_read", "shell_mode": "read_only",
                                        "arguments": {"filepath": "notes.txt", "path": "notes.txt"}},
               "result_payload": {"content": "Hello"},
               "result_submission_hash": digest, "status": "succeeded", "finished_at": datetime.now(UTC),
               "commit_state": "not_applicable", "stdout": "", "stderr": "", "stdout_sha256": empty,
               "stderr_sha256": empty, "stdout_truncated": False, "stderr_truncated": False,
               "execution_token": "never-export-this"}
    rows = await accepted_native_receipts_tx(tx, run=RUN_ROW, provider="openrouter", model=MODEL)
    assert rows[0]["result"]["payload"] == {"content": "Hello"}
    assert rows[0]["provider_call_id"] == "call-1"
    assert "never-export-this" not in json.dumps(rows)
    request = copy.deepcopy(tx.tool["request_payload_json"])
    tx.tool["request_payload_json"]["arguments"]["path"] = "changed.txt"
    with pytest.raises(ContractError):
        await accepted_native_receipts_tx(tx, run=RUN_ROW, provider="openrouter", model=MODEL)
    tx.tool["request_payload_json"] = request
    tx.binding["normalizer_version"] = True
    with pytest.raises(ContractError):
        await accepted_native_receipts_tx(tx, run=RUN_ROW, provider="openrouter", model=MODEL)
    tx.binding["normalizer_version"] = 1
    for key, bad in [("stdout_truncated", True), ("result_submission_hash", "f" * 64),
                     ("status", "cancelled"), ("commit_state", "commit_unknown"),
                     ("stdout", "modified")]:
        original = tx.tool[key]
        tx.tool[key] = bad
        with pytest.raises(ContractError):
            await accepted_native_receipts_tx(tx, run=RUN_ROW, provider="openrouter", model=MODEL)
        tx.tool[key] = original
    tx.callback = False
    with pytest.raises(ContractError, match="runtime callback"):
        await accepted_native_receipts_tx(tx, run=RUN_ROW, provider="openrouter", model=MODEL)


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"arguments": {"path": "other.txt"}}, {"tool_name": "fs_apply_patch"}, {"timeout_sec": 1},
    {"shell_mode": "mutating"}, {"provider_call_id": "missing"},
    {"run": dict(RUN_ROW, state="completed")},
])
async def test_changed_effective_request_is_not_bound(override):
    tx = Tx()
    with pytest.raises(ContractError):
        await bind(tx, **override)
    assert tx.binding is None


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["absent", "failed", "unsettled", "provider", "duplicate", "used", "limit"])
async def test_source_must_be_unique_owned_settled_and_within_bound(condition):
    tx = Tx()
    if condition == "absent":
        tx.source = None
    elif condition == "failed":
        tx.source["status"] = "failed"
    elif condition == "unsettled":
        tx.preauth["status"] = "reserved"
    elif condition == "provider":
        tx.preauth["provider"] = "anthropic"
    elif condition == "duplicate":
        tx.body["tool_calls"].append(copy.deepcopy(CALL))
    elif condition == "used":
        tx.previous = "already-bound"
    elif condition == "limit":
        tx.count = 128
    with pytest.raises(ContractError):
        await bind(tx)
    assert tx.binding is None


@pytest.mark.parametrize("value", [None, "no", REQUEST.upper(), " " + REQUEST])
def test_bad_internal_ids_are_rejected(value):
    with pytest.raises(ContractError):
        require_uuid(value)


@pytest.mark.parametrize("value", [None, "sk-" + "a" * 30, "call\n", "x" * 257])
def test_bad_provider_ids_are_rejected(value):
    with pytest.raises(ContractError):
        require_call_id(value)


@pytest.mark.parametrize("value", [{"nested": ["sk-or-v1-" + "a" * 64]},
                                  {"nested": "\x1b[2J"}, {"password": "private-value"}])
def test_sensitive_or_terminal_control_receipts_cannot_transfer(value):
    with pytest.raises(ContractError):
        safe_receipt_content(value)


@pytest.mark.parametrize("value", [None, [], "{" , {"value": float("nan")}, {"value": "\ud800"}, "\ud800"])
def test_invalid_storage_is_rejected(value):
    with pytest.raises(ContractError):
        object_value(value)


@pytest.mark.parametrize("value", [{"items": [{"content": "internal-fixture-data"}]},
                                  {"internal-fixture-key": "value"},
                                  {"value": "line one\ninternal-fixture-data"}])
def test_current_configured_redaction_policy_also_blocks_receipt_transfer(monkeypatch, value):
    monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", "internal-fixture")
    with pytest.raises(ContractError, match="Sensitive"):
        safe_receipt_content(value)


def test_reference_annotations_are_server_owned_and_do_not_mutate_result():
    result = InferenceResult("", 1, 1, tool_calls=[dict(CALL, native_inference_request_id="forged")])
    assert "native_inference_request_id" not in _native_tool_references(result, provider="openrouter")[0]
    result.inference_request_id = REQUEST
    assert _native_tool_references(result, provider="openrouter")[0]["native_inference_request_id"] == REQUEST
    assert "native_inference_request_id" not in _native_tool_references(result, provider="openai")[0]
    assert result.tool_calls[0]["native_inference_request_id"] == "forged"


def test_inference_replay_uses_database_id_not_response_body_claim():
    result = AIGateway._deserialize_result({"id": REQUEST, "response_body_text": json.dumps({
        "text": "OK", "inference_request_id": "forged", "input_tokens": 1, "output_tokens": 1})})
    assert result.inference_request_id == REQUEST
    with pytest.raises(TypeError):
        InferenceResult("", 0, 0, inference_request_id="forged")


@pytest.mark.parametrize("denied", [False, True])
def test_receipt_route_uses_authenticated_owner_and_contract_error(monkeypatch, denied):
    app = FastAPI()
    app.include_router(agent_routes.router)
    seen = []

    class Service:
        async def native_tool_receipts(self, **kwargs):
            seen.append(kwargs)
            if denied:
                require_uuid("bad")
            return {"receipts": [], "conversation_replay_supported": False}

    monkeypatch.setattr(agent_routes, "get_agent_orchestration_service", Service)
    app.dependency_overrides[agent_routes.get_current_user] = lambda: {"user_id": USER}
    response = TestClient(app).get(f"/agent/runs/{RUN}/native-tool-receipts", params={
        "runtime_session_id": SESSION, "provider": "openrouter", "model": MODEL,
        "user_id": "forged-owner",
    })
    assert response.status_code == (409 if denied else 200)
    assert seen == [{"user_id": USER, "run_id": RUN, "runtime_session_id": SESSION,
                     "provider": "openrouter", "model": MODEL}]
    assert "Traceback" not in response.text


def test_receipt_route_rejects_anonymous_before_service(monkeypatch):
    app = FastAPI()
    app.include_router(agent_routes.router)

    def unexpected_service():
        raise AssertionError("Anonymous receipt request reached service")

    monkeypatch.setattr(agent_routes, "get_agent_orchestration_service", unexpected_service)
    response = TestClient(app).get(f"/agent/runs/{RUN}/native-tool-receipts", params={
        "runtime_session_id": SESSION, "provider": "openrouter", "model": MODEL,
    })
    assert response.status_code == 401
