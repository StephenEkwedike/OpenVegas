"""Offline ownership boundaries; real transaction coverage lives in integration."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from openvegas.agent.native_generation import (
    NativeGenerationClaim,
    lock_dispatch_claim_tx,
    scope_document,
    verify_source_scope_tx,
)
from openvegas.agent.orchestration_contracts import valid_actions_signature
from openvegas.contracts.errors import ContractError
from openvegas.contracts.native_scope import NativeInferenceScope
from openvegas.gateway.inference import AIGateway, InferenceRequest
from server.routes.inference import AskRequest
from server.services.inference_replay import command_fingerprint

USER, RUN, SESSION, ROUTE, GATEWAY = [str(uuid4()) for _ in range(5)]
SCOPE = {"run_id": RUN, "runtime_session_id": SESSION, "expected_run_version": 0,
         "expected_valid_actions_signature": valid_actions_signature(0, [{"action": "resume"}, {"action": "cancel"}])}


def incoming(**changes):
    return {"prompt": "Read notes", "provider": "openrouter", "model": "fixture/model",
            "enable_tools": True, "persist_context": False, "conversation_mode": "ephemeral",
            "thread_id": None, "idempotency_key": "native-test", "native_scope": dict(SCOPE), **changes}


@pytest.mark.parametrize("field,bad", [
    ("run_id", "00000000-0000-0000-0000-000000000000"), ("run_id", RUN.upper()),
    ("run_id", RUN.replace("-", "")), ("run_id", " " + RUN), ("run_id", 1),
    ("runtime_session_id", "not-a-uuid"), ("runtime_session_id", None),
    ("expected_run_version", True), ("expected_run_version", -1),
    ("expected_run_version", 2**63), ("expected_run_version", 0.0),
    ("expected_run_version", "0"), ("expected_valid_actions_signature", "sha256:" + "A" * 64),
    ("expected_valid_actions_signature", "sha256:" + "a" * 64 + "\n"),
    ("expected_valid_actions_signature", "a" * 64), ("expected_valid_actions_signature", None),
])
def test_scope_rejects_coercion_and_noncanonical_values(field, bad):
    with pytest.raises(ValidationError):
        NativeInferenceScope.model_validate({**SCOPE, field: bad})


@pytest.mark.parametrize("extra", ["user_id", "owner_token", "gateway_request_id", "claim"])
def test_scope_accepts_no_authority_fields(extra):
    with pytest.raises(ValidationError):
        NativeInferenceScope.model_validate({**SCOPE, extra: USER})


def test_dto_exact_dump_and_frozen_snapshot():
    value = dict(SCOPE)
    scope = NativeInferenceScope.model_validate(value)
    value["run_id"] = SESSION
    assert scope.model_dump(mode="json") == SCOPE
    with pytest.raises(ValidationError):
        scope.run_id = SESSION


@pytest.mark.parametrize("change", [
    {"idempotency_key": None}, {"idempotency_key": ""}, {"idempotency_key": "bad\nkey"},
    {"provider": "openai"}, {"enable_tools": False}, {"persist_context": True},
    {"thread_id": RUN}, {"conversation_mode": None}, {"conversation_mode": "persistent"},
])
def test_scoped_request_contract_rejects_unsupported_mode(change):
    with pytest.raises(ValidationError):
        AskRequest.model_validate(incoming(**change))


def test_first_generation_preserves_optional_web_and_attachments():
    req = AskRequest.model_validate(incoming(enable_web_search=True, attachments=[GATEWAY]))
    assert req.enable_web_search and req.attachments == [GATEWAY]
    assert req.native_scope.model_dump(mode="json") == SCOPE
    # The private claim is neither a constructor parameter nor HTTP authority.
    req = AskRequest.model_validate(incoming(_native_generation_claim={"owner_token": "forged"}))
    assert "_native_generation_claim" not in req.model_dump()
    with pytest.raises(TypeError):
        InferenceRequest("user:" + USER, "openrouter", "fixture/model", [], _native_generation_claim={})


def test_legacy_fingerprint_byte_contract_and_scoped_version():
    old = {"prompt": "x", "provider": "openrouter", "model": "fixture/model",
           "thread_id": None, "conversation_mode": None, "persist_context": True,
           "enable_tools": False, "enable_web_search": False, "attachments": [], "reasoning_effort": None}
    legacy_preimage = json.dumps({"schema": "inference_route_replay_v1", "command": old},
                                sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    expected = hashlib.sha256(legacy_preimage.encode()).hexdigest()
    assert command_fingerprint(old) == command_fingerprint(dict(old, native_scope=None)) == expected
    assert command_fingerprint(dict(old, native_scope=SCOPE)) != expected
    assert command_fingerprint(dict(old, native_scope={**SCOPE, "run_id": SESSION})) != command_fingerprint(dict(old, native_scope=SCOPE))


def rows():
    run = {"id": RUN, "user_id": USER, "runtime_session_id": SESSION, "state": "created", "version": 0,
           "workspace_root": "/fixture", "workspace_fingerprint": "sha256:" + "a" * 64,
           "git_root": None, "native_generation_claim_id": ROUTE}
    claim = NativeGenerationClaim(USER, ROUTE, NativeInferenceScope(**SCOPE),
                                  scope_document(NativeInferenceScope(**SCOPE), run),
                                  str(uuid4()), "a" * 64, "server-derived-key")
    row = {"id": ROUTE, "user_id": USER, "native_run_id": RUN, "gateway_request_id": None,
           "payload_hash": claim.command_hash, "status": "processing", "native_scope": claim.scope_json,
           "response_body_text": json.dumps({"owner_token": claim.owner_token, "gateway_key": claim.gateway_key})}
    return run, claim, row


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["account", "session", "root", "key", "linked", "token", "cancelled", "stale", "expired"])
async def test_gateway_rechecks_claim_before_gateway_row_or_provider(change):
    from datetime import UTC, datetime, timedelta

    run, claim, row = rows()
    req = InferenceRequest("user:" + USER, "openrouter", "fixture/model", [{"role": "user", "content": "x"}],
                           enable_tools=True, idempotency_key=claim.gateway_key)
    if change == "account":
        req.account_id = "agent:" + USER
    elif change == "session":
        run["runtime_session_id"] = USER
    elif change == "root":
        run["workspace_root"] = "/changed"
    elif change == "key":
        req.idempotency_key = "different"
    elif change == "linked":
        row["gateway_request_id"] = GATEWAY
    elif change == "token":
        claim = replace(claim, owner_token=str(uuid4()))
    elif change == "cancelled":
        run["cancel_requested_at"] = datetime.now(UTC)
    elif change == "stale":
        run["version"] = 1
    else:
        run["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)

    async def fetchrow(sql, *args):
        if "FROM agent_runs" in sql:
            return run
        if "FROM inference_route_commands" in sql:
            return row
        return None

    tx = SimpleNamespace(fetchrow=fetchrow, fetch=AsyncMock(return_value=[]))
    with pytest.raises(ContractError):
        await lock_dispatch_claim_tx(tx, claim, req)


@pytest.mark.asyncio
async def test_linkage_is_required_both_directions_and_legacy_never_gets_provenance():
    run, _claim, row = rows()
    row["gateway_request_id"] = GATEWAY
    tx = SimpleNamespace(fetchrow=AsyncMock(return_value=row))
    scoped = {"native_route_command_id": ROUTE}
    verified = await verify_source_scope_tx(tx, run=run, source=scoped, request_id=GATEWAY)
    assert verified == {"scope_version": 1, **SCOPE}
    for changed_run, source in [(run, {}), ({**run, "native_generation_claim_id": None}, scoped),
                                 ({**run, "id": USER}, scoped), ({**run, "runtime_session_id": USER}, scoped)]:
        with pytest.raises(ContractError):
            await verify_source_scope_tx(tx, run=changed_run, source=source, request_id=GATEWAY)
    assert await verify_source_scope_tx(tx, run={}, source={}, request_id=GATEWAY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "processing", "succeeded"])
async def test_unscoped_gateway_cannot_retry_or_replay_native_row(status):
    from datetime import UTC, datetime, timedelta

    row = {"id": GATEWAY, "payload_hash": "hash", "status": status,
        "updated_at": datetime.now(UTC) - timedelta(days=7), "native_route_command_id": ROUTE,
        "response_status": 200, "response_body_text": '{"text":"cached"}'}
    tx = SimpleNamespace(fetchrow=AsyncMock(side_effect=[None, row]))
    with pytest.raises(ContractError, match="authenticated route"):
        await AIGateway(None, None, None)._begin_inference_request(user_id=USER, idempotency_key="key", payload_hash="hash", tx=tx)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "usage", "charge", "settled", "account", "source", "failed", "voided"])
async def test_refunded_hold_requires_successful_zero_charge_usage_to_bind(bad):
    from tests.test_agent.test_native_history_contract import USER as OWNER
    from tests.test_agent.test_native_history_contract import Tx, bind

    class RefundTx(Tx):
        async def fetchrow(self, query, *args):
            if "FROM inference_usage" in query:
                assert "v_cost=0" in query and args[1] == OWNER
                return None if bad == "usage" else {"id": "usage-receipt"}
            return await super().fetchrow(query, *args)

    tx = RefundTx()
    tx.preauth.update(status="refunded", settled_v=0, account_id="user:" + OWNER)
    tx.source.update(inference_source="wrapper", final_charge_v=0)
    if bad == "charge":
        tx.source["final_charge_v"] = 1
    elif bad == "settled":
        tx.preauth["settled_v"] = 1
    elif bad == "account":
        tx.preauth["account_id"] = "agent:" + OWNER
    elif bad == "source":
        tx.source["inference_source"] = "byok"
    elif bad == "failed":
        tx.source["status"] = "failed"
    elif bad == "voided":
        tx.preauth["status"] = "voided"
    if bad is None:
        await bind(tx)
        assert tx.binding is not None and "generation_scope" not in tx.binding
    else:
        with pytest.raises(ContractError):
            await bind(tx)
        assert tx.binding is None
