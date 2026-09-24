"""Integrated envelope/transport guard checks with synthetic retained history."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from openvegas.agent.native_envelope import capture_dispatch, prepare_history_request
from openvegas.contracts.errors import ContractError
from openvegas.gateway import openrouter
from openvegas.gateway.inference import AIGateway
from openvegas.gateway.providers import model_capabilities
from server.services import native_handoff_dispatch as first
from server.services import native_handoff_guard as guard
from server.services.native_handoff_continuation import prepare_continuation
from tests.test_models.test_native_handoff_continuation import (
    make_case as make_case,  # noqa: PLC0414 - pytest fixture registration
)


@pytest.mark.asyncio
@pytest.mark.parametrize("media,web,repeated", [(False, False, False), (True, False, False),
                                              (True, False, True), (False, True, False), (True, True, False)])
async def test_continuation_capture_keeps_full_history_and_both_file_groups(make_case, media, web, repeated):
    case = await make_case(media=media, web=web, repeated=repeated)
    prepared = await prepare_continuation(case.db, request=case.request)
    req = prepared.request
    assert prepare_history_request(req) is True
    payload = openrouter.build_payload(req, req._managed_model_config, model_capabilities(req.provider, req.model))
    snapshot = capture_dispatch(req, payload)
    assert json.loads(snapshot.payload_json) == case.payload
    assert snapshot.inputs_json == case.inputs._json
    assert guard.binding_for(req) is prepared.binding
    assert guard.attachment_file_ids(req) == prepared.binding.file_ids
    assert await guard.verify_dispatch_tx(case.tx, req, expected=prepared.binding) is prepared.binding


@pytest.mark.asyncio
async def test_continuation_does_not_consume_first_commit_again(make_case, monkeypatch):
    case = await make_case()
    prepared = await prepare_continuation(case.db, request=case.request)
    consume = AsyncMock(side_effect=AssertionError("first commitment consumed twice"))
    monkeypatch.setattr(first, "consume_first_dispatch_tx", consume)
    await guard.consume_dispatch_tx(case.tx, prepared.request, "unused", expected=prepared.binding)
    consume.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_snapshot_retains_binding_and_attachment_identity_without_sharing(make_case):
    case = await make_case(media=True)
    prepared = await prepare_continuation(case.db, request=case.request)
    copied = AIGateway._snapshot_handoff_request(prepared.request)
    assert copied is not prepared.request
    binding = guard.binding_for(copied)
    assert binding == prepared.binding and binding is not prepared.binding
    assert copied._managed_attachment_context is binding.attachments
    prepared.request.messages.clear()
    guard.validate_bound_request(copied, expected=binding)
    with pytest.raises(ContractError):
        guard.validate_bound_request(copied, expected=prepared.binding)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["drop", "swap", "both", "payload", "inputs", "deadline"])
async def test_tampering_stops_transport_before_any_request(make_case, change):
    case = await make_case()
    prepared = await prepare_continuation(case.db, request=case.request)
    req, binding = prepared.request, prepared.binding
    if change == "drop":
        req._native_handoff_continuation_binding = None
    elif change == "swap":
        req._native_handoff_continuation_binding = replace(binding)
    elif change == "both":
        req._native_handoff_binding = object()
    elif change == "payload":
        req.messages[0]["content"] = "private-tamper-marker"
    elif change == "inputs":
        req._native_history_inputs = None
    else:
        binding = replace(binding, expires_at=datetime.now(UTC) - timedelta(seconds=1))
        req._native_handoff_continuation_binding = binding
    calls = []

    async def transport(request):
        calls.append(request)
        raise AssertionError("unverified request reached supplier")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        with pytest.raises(ContractError):
            await openrouter.complete(req, "synthetic-no-real-credential", model_config=req._managed_model_config,
                capabilities=model_capabilities(req.provider, req.model), parse_tool=lambda _: None,
                client=http, handoff_binding=binding)
    assert not calls


@pytest.mark.asyncio
async def test_incoming_marker_alone_is_never_authority(make_case):
    case = await make_case()
    prepared = await prepare_continuation(case.db, request=case.request)
    prepared.request._native_handoff_continuation_binding = None
    with pytest.raises(ContractError):
        prepare_history_request(prepared.request)
