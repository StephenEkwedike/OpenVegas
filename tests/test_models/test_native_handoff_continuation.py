"""Offline continuation preflight; SQL/provenance/accepted-receipt doubles only.

Payload construction, exact history comparison, review/capability validation,
file parsing and occurrence accounting are real. No gateway or provider runs.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from openvegas.agent.native_envelope import NativeEnvelope, NativeHistoryInputs, history_inputs
from openvegas.agent.native_generation import NativeGenerationClaim, scope_document
from openvegas.agent.native_handoff_document import PortableTaskDocument
from openvegas.contracts.errors import ContractError
from openvegas.contracts.native_scope import NativeInferenceScope
from openvegas.gateway.inference import InferenceRequest
from openvegas.gateway.openrouter import build_payload
from openvegas.gateway.providers import model_capabilities
from server.services import native_handoff_continuation as continuation
from server.services.attachment_history import references
from server.services.inference_replay import _KIND, _gateway_key
from server.services.native_handoff_context import _combine
from server.services.native_handoff_provenance import ConsumedHandoff
from server.services.openrouter_attachments import prepare_owned_attachment_blocks
from tests.test_models import test_openrouter_web_gateway as web_fixture
from tests.test_models.test_openrouter_attachments import (
    IDS,
    MODEL,
    OWNER,
    config,
    picture,
    review,
    row,
)


@pytest.fixture
def make_case(monkeypatch):
    async def make(*, media=False, web=False, repeated=False):
        for flag in ("OPENVEGAS_NATIVE_TASK_HANDOFF", "OPENVEGAS_NATIVE_GENERATION_SCOPE",
                     "OPENVEGAS_NATIVE_GENERATION_HISTORY", "OPENVEGAS_FEATURES_ENABLED",
                     "OPENVEGAS_MODEL_SWITCH_ENABLED", "OPENVEGAS_ENABLE_REASONING_CONTROLS",
                     "OPENVEGAS_ENABLE_WEB_SEARCH", "OPENVEGAS_ENABLE_FILE_UPLOAD",
                     "OPENVEGAS_ENABLE_IMAGE_INPUT"):
            monkeypatch.setenv(flag, "1")
        inspected = review()
        if web:
            inspected["web_search"] = web_fixture.review()["web_search"]
            inspected["context_window_tokens"] = 16384
            inspected["web_search"]["execution"].update(context_window_tokens=16384, max_output_tokens=1000)
            for section in ("execution", "prices"):
                inspected["web_search"][section]["expires_at"] = inspected["expires_at"]
            inspected["attachments"]["provider"] = "fixture/endpoint"
            inspected["capabilities"] = {"web_search": True}
        inspected.setdefault("capabilities", {}).update(tools=True, reasoning_efforts=["low", "high"])
        inspected["supported_parameters"] = ["tools", "reasoning"]
        monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({"openrouter:" + MODEL: inspected}))
        from server.services import dependencies
        monkeypatch.setattr(dependencies, "current_flags", lambda: SimpleNamespace(files_enabled=True))
        catalog = config()
        uploads = [row(), row(picture(), "image/png", "current.png", index=1)] if media else []
        inherited = references(uploads[:1]) if media else []
        current = references(uploads[:1] if repeated else uploads[1:]) if media else []
        document = PortableTaskDocument.from_tasks([{
            "user_text": "Earlier task", "attachment_refs": inherited,
            "generations": [{"assistant_text": "Earlier answer", "observations": []}],
        }])
        handoff_id, previous_id, prior_route_id, route_id = (str(uuid4()) for _ in range(4))
        incoming = ConsumedHandoff(handoff_id, "b" * 64, document, previous_id, (handoff_id,))
        scope = NativeInferenceScope(run_id=str(uuid4()), runtime_session_id=str(uuid4()),
            expected_run_version=2, expected_valid_actions_signature="sha256:" + "a" * 64)
        run = {"id": scope.run_id, "user_id": OWNER, "runtime_session_id": scope.runtime_session_id,
            "workspace_root": "/synthetic", "workspace_fingerprint": "sha256:" + "c" * 64,
            "git_root": None, "native_handoff_id": handoff_id, "native_generation_claim_id": route_id,
            "native_history_revision": 1}
        settings = {"provider": "openrouter", "model": MODEL, "enable_tools": True, "enable_web_search": web,
            "reasoning_effort": "high", "max_tokens": 100, "prompt": "Current task", "native_user_text": "Current task",
            "attachments": [ref["file_id"] for ref in current]}
        inputs = history_inputs(attachment_refs=current, settings=settings, incoming_handoff=incoming.provenance())
        messages = [{"role": "system", "content": "Server instructions"}]
        batches = []
        rows = {item["file_id"]: item for item in uploads}

        async def resolve(*, user_id, file_ids):
            assert user_id == OWNER
            return [deepcopy(rows[ident]) for ident in file_ids]

        for prompt, refs in (("Earlier task", inherited), ("Current task", current)):
            content = prompt
            if refs:
                prepared = await prepare_owned_attachment_blocks(user_id=OWNER,
                    file_ids=[ref["file_id"] for ref in refs], model_id=MODEL, model_config=catalog,
                    model_review=inspected, upload_service=SimpleNamespace(resolve_uploaded_for_inference=resolve))
                batches.append(prepared)
                content = [{"type": "text", "text": prompt}, *prepared.blocks]
            messages.append({"role": "user", "content": content})
        old = InferenceRequest("user:" + OWNER, "openrouter", MODEL, messages, max_tokens=100,
            enable_tools=True, enable_web_search=web, reasoning_effort="high", idempotency_key=_gateway_key(OWNER, "previous"))
        old._managed_attachment_context = _combine(batches)
        prior_payload = build_payload(old, catalog, model_capabilities("openrouter", MODEL))
        assistant = {"role": "assistant", "content": None,
            "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque-private-canary", "signature": "exact=="}],
            "tool_calls": [{"id": "call-1", "type": "function", "function": {
                "name": "call_local_tool", "arguments": '{"tool_name":"Read","arguments":{"path":"a.txt"}}'}}]}
        results = [{"role": "tool", "tool_call_id": "call-1", "content": '{"complete":"accepted result"}'}]
        payload = {**deepcopy(prior_payload), "messages": [*deepcopy(prior_payload["messages"]), assistant, *results]}
        owner = str(uuid4())
        claim = NativeGenerationClaim(OWNER, route_id, scope, scope_document(scope, run), owner,
            "d" * 64, _gateway_key(OWNER, "next"), 1, previous_id, continuation._wire(payload), inputs._json)
        request = InferenceRequest("user:" + OWNER, "openrouter", MODEL, deepcopy(payload["messages"]),
            max_tokens=100, enable_tools=True, enable_web_search=web, reasoning_effort="high", idempotency_key=claim.gateway_key)
        request._native_generation_claim, request._native_history_inputs = claim, inputs
        previous = {"id": prior_route_id, "user_id": OWNER, "native_run_id": scope.run_id,
            "native_history_revision": 0, "previous_native_request_id": None, "gateway_request_id": previous_id,
            "native_scope": claim.scope_json, "payload_hash": "e" * 64, "idempotency_key": "previous",
            "status": "succeeded", "response_status": 200,
            "response_body_text": json.dumps({"kind": _KIND, "state": "completed", "owner_token": str(uuid4()),
                "gateway_key": _gateway_key(OWNER, "previous"), "gateway_request_id": previous_id, "response": {}})}
        route = {"id": route_id, "user_id": OWNER, "native_run_id": scope.run_id,
            "native_history_revision": 1, "previous_native_request_id": previous_id, "gateway_request_id": None,
            "native_scope": claim.scope_json, "payload_hash": claim.command_hash, "idempotency_key": "next",
            "status": "processing", "response_status": None,
            "response_body_text": json.dumps({"kind": _KIND, "state": "processing", "owner_token": owner,
                "gateway_key": claim.gateway_key})}
        envelope = NativeEnvelope(continuation._wire(assistant), continuation._wire(prior_payload), inputs._json,
            "provider-call", MODEL, "tool_calls", "f" * 64, "a" * 64)
        case = SimpleNamespace(request=request, claim=claim, incoming=incoming, run=run, route=route,
            previous=previous, rows=rows, catalog=catalog, review=inspected, inputs=inputs,
            payload=payload, results=results, envelope=envelope, order=[], loads=[],
            file_expiry=datetime.now(UTC) + timedelta(minutes=4))

        async def fetchrow(sql, *args):
            case.order.append(sql)
            if "FROM agent_runs" in sql:
                return deepcopy(run)
            if "FROM provider_catalog" in sql:
                return deepcopy(catalog)
            if "FROM inference_requests" in sql:
                return {"id": previous_id, "user_id": OWNER, "status": "succeeded", "response_status": 200}
            raise AssertionError(sql)

        async def fetch(sql, *args):
            case.order.append(sql)
            assert "FROM inference_route_commands" in sql
            return deepcopy([previous, route])

        async def fetchval(sql, *args):
            assert "min(expires_at)" in sql
            return case.file_expiry

        tx = SimpleNamespace(fetchrow=fetchrow, fetch=fetch, fetchval=fetchval)
        case.tx = tx

        class Database:
            @asynccontextmanager
            async def transaction(self):
                yield tx

        case.db = Database()

        async def owned_files(self, *, user_id, file_ids, tx=None):
            assert tx is case.tx and user_id == OWNER
            case.loads.append(list(file_ids))
            return [deepcopy(rows[ident]) for ident in file_ids]

        async def provenance(*args, **kwargs):
            case.order.append("provenance")
            assert args[0] is tx and kwargs == {"user_id": OWNER, "scope": scope}
            return incoming

        monkeypatch.setattr(continuation, "verify_consumed_handoff_tx", AsyncMock(side_effect=provenance))
        monkeypatch.setattr(continuation, "require_fresh_projection_tx", AsyncMock())
        monkeypatch.setattr(continuation, "load_native_envelope_tx", AsyncMock(return_value=envelope))
        monkeypatch.setattr(continuation, "load_native_tool_results_tx", AsyncMock(return_value=results))
        monkeypatch.setattr(continuation.FileUploadService, "resolve_uploaded_for_inference", owned_files)
        return case
    return make


async def prepare(case):
    return await continuation.prepare_continuation(case.db, request=case.request)


@pytest.mark.asyncio
@pytest.mark.parametrize("media,web,repeated", [(False,False,False),(True,False,False),
                                              (True,False,True),(False,True,False),(True,True,False)])
async def test_exact_retained_payload_and_new_authorization(make_case, media, web, repeated):
    case = await make_case(media=media, web=web, repeated=repeated)
    before = deepcopy(case.request.messages)
    prepared = await prepare(case)
    req, binding = prepared.request, prepared.binding
    assert req is not case.request and case.request.messages == before
    assert req.messages == case.payload["messages"] and req._native_history_inputs._json == case.inputs._json
    assert req._native_handoff_binding is None and binding.provenance() == case.incoming.provenance()
    assert "opaque-private-canary" not in repr(binding) + repr(prepared)
    assert "opaque-private-canary" in binding.payload_json
    assert case.order[0] == "provenance"
    assert continuation.load_native_envelope_tx.call_args.kwargs["expected_route_command_id"] == case.previous["id"]
    assert binding.expires_at > datetime.now(UTC)
    if media:
        assert binding.expires_at == case.file_expiry
        assert binding.file_ids == ((IDS[0], IDS[0]) if repeated else (IDS[0], IDS[1]))
        expected_loads = [[ident] for ident in sorted(set(binding.file_ids))]
        assert case.loads == expected_loads * 2
    assert await continuation.verify_continuation_tx(case.tx, req, expected=binding) is binding
    continuation.validate_continuation(req, expected=binding, payload=case.payload,
                                       wire_bytes=binding.payload_json.encode())
    with pytest.raises(FrozenInstanceError):
        binding.expires_at = datetime.now(UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("owner_token", str(uuid4())), ("command_hash", "0"*64),
    ("previous_request_id", str(uuid4())), ("history_revision", 0), ("history_revision", True),
    ("history_revision", 2), ("route_command_id", str(uuid4())), ("gateway_key", "wrong")])
async def test_forged_claim_is_rejected(make_case, field, value):
    case = await make_case()
    case.request._native_generation_claim = replace(case.claim, **{field:value})
    with pytest.raises(ContractError):
        await prepare(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["messages", "payload", "inputs", "provenance", "model", "reasoning", "web"])
async def test_payload_or_input_substitution_is_not_authority(make_case, change):
    case = await make_case()
    req = case.request
    if change in {"messages", "payload"}:
        req.messages[0]["content"] = "forged-private-canary"
        if change == "payload":
            fake = {**case.payload, "messages": req.messages}
            req._native_generation_claim = replace(case.claim, continuation_payload_json=continuation._wire(fake))
    elif change in {"inputs", "provenance"}:
        values = case.inputs.values()
        if change == "inputs": values["settings"]["prompt"] = "forged-private-canary"
        else: values["incoming_handoff"]["handoff_sha256"] = "f" * 64
        raw = continuation._wire(values)
        req._native_history_inputs = NativeHistoryInputs(raw)
        req._native_generation_claim = replace(case.claim, history_inputs_json=raw)
    elif change == "model": req.model = "fixture/other-model"
    elif change == "reasoning": req.reasoning_effort = "low"
    else: req.enable_web_search = True
    with pytest.raises(ContractError) as error:
        await prepare(case)
    assert "forged-private-canary" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["proof", "settlement", "results", "final", "route", "parent", "scope"])
async def test_missing_proof_settlement_receipts_or_links_block(make_case, monkeypatch, change):
    case = await make_case()
    if change == "proof": continuation.verify_consumed_handoff_tx.side_effect = RuntimeError("private-proof-error")
    elif change == "settlement": continuation.load_native_envelope_tx.side_effect = RuntimeError("private-billing-error")
    elif change == "results": continuation.load_native_tool_results_tx.return_value = []
    elif change == "final": continuation.load_native_envelope_tx.return_value = replace(case.envelope, finish_reason="stop")
    elif change == "route": case.route["gateway_request_id"] = str(uuid4())
    elif change == "parent": case.previous["gateway_request_id"] = str(uuid4())
    else: case.run["workspace_root"] = "/different-private-workspace"
    with pytest.raises(ContractError) as error:
        await prepare(case)
    assert "private-" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["bytes", "name", "owner", "id", "missing", "expired"])
async def test_current_and_inherited_files_reauthorized(make_case, change):
    case = await make_case(media=True)
    if change == "bytes": case.rows[IDS[0]]["content_bytes"] = b"different"
    elif change == "name": case.rows[IDS[0]]["filename"] = "renamed.txt"
    elif change == "owner": case.rows[IDS[1]]["user_id"] = str(uuid4())
    elif change == "id": case.rows[IDS[1]]["file_id"] = IDS[2]
    elif change == "missing": case.rows.pop(IDS[1])
    else: case.file_expiry = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ContractError):
        await prepare(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["OPENVEGAS_NATIVE_TASK_HANDOFF", "OPENVEGAS_NATIVE_GENERATION_HISTORY",
    "OPENVEGAS_FEATURES_ENABLED", "OPENVEGAS_MODEL_SWITCH_ENABLED", "OPENVEGAS_ENABLE_REASONING_CONTROLS",
    "OPENVEGAS_ENABLE_FILE_UPLOAD", "OPENVEGAS_ENABLE_IMAGE_INPUT"])
async def test_gate_flip_after_prepare_cannot_reuse_binding(make_case, monkeypatch, flag):
    case = await make_case(media=True)
    prepared = await prepare(case)
    monkeypatch.setenv(flag, "0")
    with pytest.raises(ContractError):
        continuation.validate_continuation_deadline(prepared.request, expected=prepared.binding)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["marker", "copy", "claim", "inputs", "messages", "media", "wire"])
async def test_latched_identity_and_transport_reject_mutation(make_case, change):
    case = await make_case(media=True)
    prepared = await prepare(case)
    req, binding = prepared.request, prepared.binding
    kwargs = {}
    if change == "marker": req._native_handoff_continuation_binding = None
    elif change == "copy": req._native_handoff_continuation_binding = replace(binding)
    elif change == "claim": req._native_generation_claim = replace(case.claim, owner_token=str(uuid4()))
    elif change == "inputs": req._native_history_inputs = NativeHistoryInputs("{}")
    elif change == "messages": req.messages.pop()
    elif change == "media": req._managed_attachment_context = None
    else: kwargs["wire_bytes"] = binding.payload_json.encode() + b" "
    with pytest.raises(ContractError):
        continuation.validate_continuation(req, expected=binding, **kwargs)


@pytest.mark.asyncio
async def test_caller_mutation_during_await_does_not_change_snapshot(make_case, monkeypatch):
    case = await make_case()
    original = continuation.verify_consumed_handoff_tx.side_effect
    async def mutate(*args, **kwargs):
        case.request.messages[0]["content"] = "mutated after entry"
        case.request._native_history_inputs = NativeHistoryInputs("{}")
        return await original(*args, **kwargs)
    continuation.verify_consumed_handoff_tx.side_effect = mutate
    prepared = await prepare(case)
    assert prepared.request.messages == case.payload["messages"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["live_request", "gate", "expiry"])
async def test_transaction_verification_rechecks_after_await(make_case, monkeypatch, change):
    case = await make_case()
    prepared = await prepare(case)
    original = continuation.verify_consumed_handoff_tx.side_effect
    async def mutate(*args, **kwargs):
        if change == "live_request": prepared.request.messages[0]["content"] = "changed while waiting"
        elif change == "gate": monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "0")
        else:
            class Clock:
                fromisoformat = staticmethod(datetime.fromisoformat)
                @staticmethod
                def now(tz): return prepared.binding.expires_at + timedelta(seconds=1)
            monkeypatch.setattr(continuation, "datetime", Clock)
        return await original(*args, **kwargs)
    continuation.verify_consumed_handoff_tx.side_effect = mutate
    with pytest.raises(ContractError):
        await continuation.verify_continuation_tx(case.tx, prepared.request, expected=prepared.binding)


@pytest.mark.asyncio
async def test_new_tool_definitions_do_not_rewrite_historical_payload(make_case, monkeypatch):
    case = await make_case()
    from openvegas.gateway import openrouter
    original = openrouter.local_tool_definitions
    monkeypatch.setattr(openrouter, "local_tool_definitions", lambda model: [*original(model), {"changed":True}])
    with pytest.raises(ContractError):
        await prepare(case)


@pytest.mark.asyncio
async def test_occurrences_count_bytes_before_loading_next_file(make_case, monkeypatch):
    case = await make_case(media=True, repeated=True)
    case.rows[IDS[0]]["content_bytes"] = b"x" * (262144 + 1)
    case.rows[IDS[0]]["size_bytes"] = 262144 + 1
    parser = AsyncMock()
    monkeypatch.setattr(continuation, "prepare_owned_attachment_blocks", parser)
    with pytest.raises(ContractError):
        await prepare(case)
    assert case.loads == [[IDS[0]]]
    parser.assert_not_called()


@pytest.mark.asyncio
async def test_wall_clock_file_expiry_during_parser_await(make_case, monkeypatch):
    case = await make_case(media=True)
    real = continuation.prepare_owned_attachment_blocks
    async def delayed(**kwargs):
        result = await real(**kwargs)
        case.file_expiry = datetime.now(UTC) + timedelta(milliseconds=5)
        await asyncio.sleep(0.015)
        return result
    monkeypatch.setattr(continuation, "prepare_owned_attachment_blocks", delayed)
    with pytest.raises(ContractError):
        await prepare(case)


@pytest.mark.asyncio
async def test_file_reread_detects_post_parser_substitution(make_case, monkeypatch):
    case = await make_case(media=True)
    real = continuation.prepare_owned_attachment_blocks
    async def changed(**kwargs):
        result = await real(**kwargs)
        if kwargs["file_ids"] == [IDS[1]]:
            case.rows[IDS[0]]["filename"] = "post-await-rename.txt"
        return result
    monkeypatch.setattr(continuation, "prepare_owned_attachment_blocks", changed)
    with pytest.raises(ContractError):
        await prepare(case)


@pytest.mark.asyncio
async def test_expired_first_dispatch_deadline_is_not_reused(make_case, monkeypatch):
    case = await make_case()
    # The consumed-proof verifier is the historical authority. Its result has
    # no first-dispatch deadline; continuation must not reload one as new auth.
    forbidden = AsyncMock(side_effect=AssertionError("old deadline must not be used"))
    monkeypatch.setattr(continuation.store, "_now_unexpired", forbidden)
    prepared = await prepare(case)
    assert prepared.binding.expires_at > datetime.now(UTC)
    forbidden.assert_not_called()


@pytest.mark.asyncio
async def test_revision_two_preserves_full_prior_payload_and_original_inputs(make_case):
    case = await make_case()
    next_id, middle_id = str(uuid4()), str(uuid4())
    middle = deepcopy(case.previous)
    middle.update(id=middle_id, native_history_revision=1,
        previous_native_request_id=case.incoming.first_request_id, gateway_request_id=next_id)
    body = json.loads(middle["response_body_text"])
    body["gateway_request_id"] = next_id
    middle["response_body_text"] = json.dumps(body)
    async def routes(*args): return deepcopy([case.previous, middle, case.route])
    case.tx.fetch = routes
    case.run["native_history_revision"] = case.route["native_history_revision"] = 2
    case.route["previous_native_request_id"] = next_id
    next_envelope = replace(case.envelope, request_payload_json=case.claim.continuation_payload_json)
    continuation.load_native_envelope_tx.side_effect = [next_envelope, case.envelope]
    payload = {**case.payload, "messages": [*case.payload["messages"], case.envelope.assistant_message(), *case.results]}
    case.request._native_generation_claim = replace(case.claim, history_revision=2,
        previous_request_id=next_id, continuation_payload_json=continuation._wire(payload))
    case.request.messages = deepcopy(payload["messages"])
    prepared = await prepare(case)
    assert prepared.binding.payload_json == continuation._wire(payload)
    assert prepared.request._native_history_inputs._json == case.inputs._json
    assert [call.kwargs["expected_route_command_id"] for call in continuation.load_native_envelope_tx.call_args_list] == [middle_id, case.previous["id"]]


@pytest.mark.asyncio
async def test_web_gate_flip_is_rechecked_without_media(make_case, monkeypatch):
    case = await make_case(web=True)
    prepared = await prepare(case)
    monkeypatch.setenv("OPENVEGAS_ENABLE_WEB_SEARCH", "0")
    with pytest.raises(ContractError):
        continuation.validate_continuation_deadline(prepared.request, expected=prepared.binding)


@pytest.mark.asyncio
async def test_transaction_exit_is_not_an_authorization_gap(make_case, monkeypatch):
    case = await make_case()
    class Database:
        @asynccontextmanager
        async def transaction(self):
            yield case.tx
            monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "0")
    case.db = Database()
    with pytest.raises(ContractError):
        await prepare(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["review", "catalog", "context"])
async def test_current_configuration_changes_do_not_reuse_prepared_auth(make_case, monkeypatch, change):
    case = await make_case()
    prepared = await prepare(case)
    if change == "review":
        monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    elif change == "catalog":
        case.catalog["v_price_input_per_1m"] = "999"
    else:
        case.review["context_window_tokens"] = 100
        monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({"openrouter:"+MODEL:case.review}))
    with pytest.raises(ContractError):
        await continuation.verify_continuation_tx(case.tx, prepared.request, expected=prepared.binding)


@pytest.mark.asyncio
async def test_payload_hash_matches_first_dispatch_capture_without_weakening_wire_check(make_case):
    case = await make_case()
    prepared = await prepare(case)
    assert prepared.binding.payload_sha256 == continuation._hash(case.payload)
    reordered = dict(reversed(list(case.payload.items())))
    assert continuation._hash(reordered) == prepared.binding.payload_sha256
    with pytest.raises(ContractError):
        continuation.validate_continuation(prepared.request, expected=prepared.binding,
                                           wire_bytes=continuation._wire(reordered).encode())
