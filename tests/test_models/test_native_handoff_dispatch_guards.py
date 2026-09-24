"""Offline dispatch guard regressions, not SQL/provider certification.

Preparation, intent hashes, claim checks, payloads and gateway control flow are
real. Source SQL, wallet effects, transport and settlement use explicit doubles.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from openvegas.agent import native_generation
from openvegas.agent.native_continuation import frozen_settings
from openvegas.agent.native_envelope import history_inputs, prepare_history_request
from openvegas.agent.native_generation import NativeGenerationClaim, scope_document
from openvegas.agent.native_handoff_document import PortableTaskDocument
from openvegas.capabilities import resolve_capability
from openvegas.contracts.errors import ContractError
from openvegas.contracts.native_scope import NativeInferenceScope
from openvegas.gateway import openrouter
from openvegas.gateway.inference import AIGateway, InferenceRequest, InferenceResult
from openvegas.gateway.providers import model_capabilities
from server.services import native_handoff_dispatch as dispatch
from server.services import native_handoff_service as service
from server.services.inference_replay import _gateway_key, command_fingerprint
from server.services.openrouter_attachment_request import prepare_attachment_request
from tests.test_models import test_openrouter_web_gateway as web_fixture
from tests.test_models.test_openrouter_attachments import MODEL, OWNER, config, review, row


@pytest.fixture
def make_case(monkeypatch):
    async def make(*, web=False, media=False):
        for name in (
            "OPENVEGAS_NATIVE_TASK_HANDOFF", "OPENVEGAS_NATIVE_GENERATION_SCOPE",
            "OPENVEGAS_NATIVE_GENERATION_HISTORY", "OPENVEGAS_FEATURES_ENABLED",
            "OPENVEGAS_MODEL_SWITCH_ENABLED", "OPENVEGAS_ENABLE_REASONING_CONTROLS",
            "OPENVEGAS_ENABLE_WEB_SEARCH", "OPENVEGAS_ENABLE_FILE_UPLOAD",
        ):
            monkeypatch.setenv(name, "1")
        catalog = web_fixture.catalog_row() if web else config()
        inspected = web_fixture.review() if web else review()
        inspected.setdefault("capabilities", {}).update(tools=True, reasoning_efforts=["low", "high"])
        inspected["supported_parameters"] = ["tools", "reasoning"]
        monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({"openrouter:" + MODEL: inspected}))
        monkeypatch.setattr(service, "resolve_provider_api_key", AsyncMock(return_value="synthetic-test-key"))
        from server.services import dependencies
        monkeypatch.setattr(dependencies, "current_flags", lambda: SimpleNamespace(files_enabled=True))

        document = PortableTaskDocument.from_tasks([{
            "user_text": "Previous completed task.", "attachment_refs": [],
            "generations": [{"assistant_text": "Previous public answer.", "observations": []}],
        }])
        selection = service.HandoffSelection(MODEL, enable_web_search=web,
                                             reasoning_effort="high", max_tokens=100)
        scope = NativeInferenceScope(run_id=str(uuid4()), runtime_session_id=str(uuid4()),
            expected_run_version=0, expected_valid_actions_signature="sha256:" + "a" * 64)
        source_scope = scope.model_copy(update={"run_id": str(uuid4()), "runtime_session_id": str(uuid4())})
        route_id, handoff_id, request_id = (str(uuid4()) for _ in range(3))
        gateway_key = _gateway_key(OWNER, "offline-dispatch-guards")
        upload = row()
        command = {
            "provider": "openrouter", "model": MODEL, "prompt": "Current protocol prompt.",
            "native_user_text": "Current original user input.", "enable_tools": True,
            "enable_web_search": web, "reasoning_effort": "high",
            "attachments": [upload["file_id"]] if media else [],
            "native_scope": scope.model_dump(), "native_history": True,
            "persist_context": False, "thread_id": None, "conversation_mode": "ephemeral",
        }
        run = {"id": scope.run_id, "user_id": OWNER, "runtime_session_id": scope.runtime_session_id,
            "workspace_root": "/synthetic-workspace", "workspace_fingerprint": "sha256:" + "c" * 64,
            "git_root": None, "state": "created", "version": 0,
            "native_handoff_id": handoff_id, "native_generation_claim_id": route_id,
            "native_history_revision": 0}
        claim = NativeGenerationClaim(OWNER, route_id, scope, scope_document(scope, run),
            str(uuid4()), command_fingerprint(command), gateway_key, 0)
        route = {"id": route_id, "user_id": OWNER, "native_run_id": scope.run_id,
            "payload_hash": claim.command_hash, "status": "processing", "gateway_request_id": None,
            "native_scope": claim.scope_json, "native_history_revision": 0,
            "previous_native_request_id": None,
            "response_body_text": json.dumps({"owner_token": claim.owner_token, "gateway_key": gateway_key})}

        async def fetchrow(sql, *args):
            if "provider_catalog" in sql:
                return dict(catalog)
            if "UPDATE inference_route_commands SET gateway_request_id" in sql:
                route["gateway_request_id"] = args[1]
                return {"id": route_id}
            if "FROM inference_route_commands" in sql:
                return dict(route)
            if "FROM agent_runs" in sql:
                return dict(run)
            return None

        async def fetchval(sql, *args):
            if "min(expires_at)" in sql:
                return datetime.now(UTC) + timedelta(hours=1)
            return None

        tx = SimpleNamespace(fetchrow=fetchrow, fetchval=fetchval, execute=AsyncMock())

        class Database:
            @asynccontextmanager
            async def transaction(self):
                yield tx

        async def resolve(*, user_id, file_ids, tx=None):
            assert user_id == OWNER and file_ids == [upload["file_id"]]
            return [dict(upload)]

        monkeypatch.setattr(dispatch.FileUploadService, "resolve_uploaded_for_inference",
                            lambda self, **kwargs: resolve(**kwargs))
        target = await service._review_target(tx, user_id=OWNER, document=document,
            selection=selection, upload_service=SimpleNamespace(resolve_uploaded_for_inference=resolve))
        record = SimpleNamespace(handoff_id=handoff_id, handoff_sha256="b" * 64,
            destination_scope=scope, source_scope=source_scope, first_request_id=None,
            target=target, document=document, expires_at=datetime.now(UTC) + timedelta(minutes=5))
        monkeypatch.setattr(dispatch.store, "lock_handoff_runs_tx", AsyncMock(return_value=record))
        monkeypatch.setattr(dispatch.store, "_runs", AsyncMock(return_value={scope.run_id: run}))
        monkeypatch.setattr(dispatch.store, "_fresh_source", AsyncMock())
        monkeypatch.setattr(dispatch.store, "_now_unexpired", AsyncMock())
        monkeypatch.setattr(native_generation, "require_fresh_projection_tx", AsyncMock())

        messages = [{"role": "system", "content": "Server-owned current instructions."},
                    {"role": "user", "content": command["prompt"]}]
        refs, context = [], None
        if media:
            messages, context, refs = await prepare_attachment_request(history=messages[:-1],
                prompt=command["prompt"], file_ids=command["attachments"], user_id=OWNER,
                model_id=MODEL, model_config=catalog,
                upload_service=SimpleNamespace(resolve_uploaded_for_inference=resolve))
        request = InferenceRequest("user:" + OWNER, "openrouter", MODEL, messages,
            max_tokens=100, idempotency_key=gateway_key, enable_tools=True,
            enable_web_search=web, reasoning_effort="high")
        request._native_generation_claim = claim
        request._managed_attachment_context = context
        request._native_history_inputs = history_inputs(attachment_refs=refs,
            settings=frozen_settings(SimpleNamespace(**command), max_tokens=100))
        db = Database()

        async def prepare():
            return await dispatch.prepare_first_dispatch(db, request=request,
                handoff_id=handoff_id, handoff_sha256=record.handoff_sha256)

        wallet = SimpleNamespace(get_balance=AsyncMock(return_value=Decimal(100)), reserve=AsyncMock())
        gateway = AIGateway(db, wallet, SimpleNamespace(get_model=AsyncMock(return_value=catalog)))
        monkeypatch.setattr(gateway, "_resolve_provider_api_key", AsyncMock(return_value="synthetic-test-key"))
        monkeypatch.setattr(gateway, "_estimate_grant_cover_v", AsyncMock(return_value=Decimal(0)))
        monkeypatch.setattr(gateway, "_begin_inference_request", AsyncMock(return_value=(request_id, None)))
        provider = AsyncMock(return_value=InferenceResult("Synthetic answer", 1, 1))
        monkeypatch.setattr(gateway, "_route_to_provider", provider)
        monkeypatch.setattr(gateway, "_finalize_inference_execution", AsyncMock(return_value=provider.return_value))
        monkeypatch.setattr(gateway, "_cleanup_inference_after_failure", AsyncMock())
        consume = AsyncMock()
        monkeypatch.setattr(dispatch, "consume_first_dispatch_tx", consume)

        async def transport(req, api_key, **kwargs):
            assert consume.await_count == 1 and wallet.reserve.await_count == 1
            return provider.return_value

        provider.side_effect = transport
        return SimpleNamespace(raw=request, prepare=prepare, gateway=gateway, wallet=wallet,
            provider=provider, consume=consume, route=route, command=command, catalog=catalog)

    return make


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{}, {"media": True}, {"web": True}])
async def test_valid_intent_reaches_consumption_before_provider(make_case, options):
    case = await make_case(**options)
    prepared = await case.prepare()
    await case.gateway.infer(prepared)
    case.consume.assert_awaited_once()
    case.wallet.reserve.assert_awaited_once()
    case.provider.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["user_message", "native_user_text", "prompt_and_message", "file_ids", "route_hash"])
async def test_unbound_current_intent_is_rejected(make_case, change):
    case = await make_case()
    inputs = case.raw._native_history_inputs.values()
    if change == "user_message":
        case.raw.messages[-1]["content"] = "Unreserved current message."
    elif change == "native_user_text":
        inputs["settings"]["native_user_text"] = "Unreserved original input."
    elif change == "prompt_and_message":
        inputs["settings"]["prompt"] = case.raw.messages[-1]["content"] = "Unreserved prompt."
    elif change == "file_ids":
        inputs["settings"]["attachments"] = [str(uuid4())]
    else:
        case.route["payload_hash"] = "d" * 64
    case.raw._native_history_inputs = history_inputs(**inputs)
    with pytest.raises(ContractError):
        prepared = await case.prepare()
        await case.gateway.infer(prepared)
    case.consume.assert_not_awaited()
    case.wallet.reserve.assert_not_awaited()
    case.provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_media_first_text_must_match_reserved_prompt(make_case):
    case = await make_case(media=True)
    case.raw.messages[-1]["content"][0]["text"] = "Substituted media prompt."
    with pytest.raises(ContractError):
        await case.prepare()


@pytest.mark.asyncio
async def test_reserved_current_file_cannot_be_silently_omitted(make_case):
    case = await make_case(media=True)
    inputs = case.raw._native_history_inputs.values()
    assert inputs["settings"]["attachments"]
    inputs["attachment_refs"] = []
    case.raw._native_history_inputs = history_inputs(**inputs)
    case.raw._managed_attachment_context = None
    case.raw.messages[-1]["content"] = inputs["settings"]["prompt"]
    with pytest.raises(ContractError):
        prepared = await case.prepare()
        await case.gateway.infer(prepared)
    case.consume.assert_not_awaited()
    case.provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_caller_mutation_during_catalog_await_cannot_change_snapshot(make_case, monkeypatch):
    case = await make_case()
    prepared = await case.prepare()
    original_messages = deepcopy(prepared.messages)
    original_binding = prepared._native_handoff_binding

    async def catalog(*args):
        prepared.messages[-1]["content"] = "Caller changed shared request."
        prepared._native_handoff_binding = None
        prepared._native_history_inputs = case.raw._native_history_inputs
        return dict(case.catalog)

    monkeypatch.setattr(case.gateway.catalog, "get_model", catalog)
    await case.gateway.infer(prepared)
    sent = case.provider.await_args.args[0]
    assert sent.messages == original_messages
    assert sent._native_handoff_binding == original_binding
    case.consume.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["link", "wallet"])
@pytest.mark.parametrize("change", ["remove_markers", "extend_deadline"])
async def test_execution_binding_cannot_change_across_await(make_case, monkeypatch, stage, change):
    case = await make_case()
    prepared = await case.prepare()
    seen = {}
    verify = dispatch.verify_first_dispatch_tx

    async def observe(tx, req, **kwargs):
        seen["request"] = req
        return await verify(tx, req, **kwargs)

    def mutate():
        req = seen["request"]
        if change == "remove_markers":
            req._native_handoff_binding = None
            req._native_history_inputs = case.raw._native_history_inputs
        else:
            binding = req._native_handoff_binding
            req._native_handoff_binding = replace(binding, expires_at=binding.expires_at + timedelta(minutes=1))

    monkeypatch.setattr(dispatch, "verify_first_dispatch_tx", observe)
    if stage == "link":
        original = native_generation.link_gateway_tx

        async def link(*args):
            await original(*args)
            mutate()

        monkeypatch.setattr(native_generation, "link_gateway_tx", link)
    else:
        async def reserve(**kwargs):
            mutate()

        monkeypatch.setattr(case.wallet, "reserve", AsyncMock(side_effect=reserve))
    with pytest.raises(ContractError):
        await case.gateway.infer(prepared)
    case.provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_is_part_of_immutable_dispatch_proof(make_case):
    case = await make_case()
    binding = (await case.prepare())._native_handoff_binding
    changed = replace(binding, expires_at=binding.expires_at + timedelta(seconds=1))
    assert binding.proof() != changed.proof()


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["reasoning_controls", "web_search"])
async def test_account_capability_revocation_during_wallet_wait_blocks_dispatch(make_case, monkeypatch, capability):
    case = await make_case(web=capability == "web_search")
    prepared = await case.prepare()
    assert resolve_capability("openrouter", MODEL, capability, user_id=OWNER)

    async def revoke(**kwargs):
        monkeypatch.setenv("OPENVEGAS_ENABLE_" + capability.upper(), "0")
        assert not resolve_capability("openrouter", MODEL, capability, user_id=OWNER)

    monkeypatch.setattr(case.wallet, "reserve", AsyncMock(side_effect=revoke))
    with pytest.raises(ContractError):
        await case.gateway.infer(prepared)
    case.wallet.reserve.assert_awaited_once()
    case.provider.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["remove_markers", "extend_deadline", "expired", "reasoning_controls", "web_search"])
async def test_transport_uses_latched_binding_and_rechecks_authorization(make_case, monkeypatch, change):
    case = await make_case(web=change == "web_search")
    prepared = await case.prepare()
    prepared._managed_model_config = dict(case.catalog)
    prepare_history_request(prepared)
    binding = prepared._native_handoff_binding
    if change == "remove_markers":
        prepared._native_handoff_binding = None
        prepared._native_history_inputs = case.raw._native_history_inputs
    elif change == "extend_deadline":
        prepared._native_handoff_binding = replace(binding, expires_at=binding.expires_at + timedelta(minutes=1))
    elif change == "expired":
        class Clock:
            @staticmethod
            def now(tz):
                return binding.expires_at + timedelta(seconds=1)

        monkeypatch.setattr(dispatch, "datetime", Clock)
    else:
        monkeypatch.setenv("OPENVEGAS_ENABLE_" + change.upper(), "0")
        assert not resolve_capability("openrouter", MODEL, change, user_id=OWNER)
    client = SimpleNamespace(stream=Mock(side_effect=AssertionError("Transport must not be entered")))
    with pytest.raises(ContractError):
        await openrouter.complete(prepared, "synthetic-test-key", model_config=case.catalog,
            capabilities=model_capabilities("openrouter", MODEL),
            parse_tool=case.gateway._parse_local_tool_call, client=client, handoff_binding=binding)
    client.stream.assert_not_called()
