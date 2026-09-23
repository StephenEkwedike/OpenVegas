"""Offline, synthetic private envelope contracts. No credentials/network."""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace
from uuid import uuid4

import httpx
import pytest

from openvegas.agent import native_envelope as envelopes
from openvegas.contracts.errors import ContractError
from openvegas.gateway import openrouter
from openvegas.gateway.inference import AIGateway, InferenceResult
from tests.test_models.test_native_generation_ownership import rows
from tests.test_models.test_openrouter import (
    MODEL,
    SYNTHETIC_CREDENTIAL,
    capabilities,
    catalog_row,
    install_review,
    request,
    response,
)

PRIVATE = "opaque-private-signature-unchanged"


def history_request(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_NATIVE_GENERATION_HISTORY", "1")
    req = request(enable_tools=True)
    _, claim, _ = rows()
    req._native_generation_claim = replace(claim, history_revision=0)
    req._native_history_inputs = envelopes.history_inputs(attachment_refs=[], settings={"prompt": "Read"})
    assert envelopes.prepare_history_request(req)
    return req


def original_message():
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-" + str(n), "type": "function", "vendor": {"signature": PRIVATE + str(n)},
         "function": {"name": "call_local_tool", "arguments": '{  "tool_name": "Read", "arguments": {"path": "a.txt"}  }'}}
        for n in range(3)
    ], "reasoning_details": [
        {"type": "reasoning.encrypted", "data": PRIVATE, "format": "vendor-x", "index": 2},
        {"type": "reasoning.text", "text": "private reason", "signature": PRIVATE, "index": 0},
    ], "vendor_extra": {"unknown": [None, False, "\u2603"]}}


def provider_response():
    body = response()
    body["choices"][0] = {"finish_reason": "tool_calls", "message": original_message()}
    return body


@pytest.mark.asyncio
async def test_capture_real_adapter_preserves_all_calls_private_fields_and_exact_json(monkeypatch):
    install_review(monkeypatch)
    req = history_request(monkeypatch)
    original = original_message()
    message_json = json.dumps(original, ensure_ascii=True, indent=2)
    message_json = message_json[:-1] + ', "vendor_number": 0.125 }'
    raw = ('{"id":"synthetic-receipt","model":' + json.dumps(MODEL)
           + ',"choices":[{"message":' + message_json + ',"finish_reason":"tool_calls"}],'
           + '"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18,"cost":"0.000020"}}').encode()
    sent = []

    def upstream(http_req):
        sent.append(json.loads(http_req.content))
        return httpx.Response(200, content=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        values = await openrouter.complete(req, SYNTHETIC_CREDENTIAL, model_config=catalog_row(),
            capabilities=capabilities(), parse_tool=AIGateway._parse_local_tool_call, client=client)
    result = InferenceResult(**values)
    envelope = req._native_envelope_capture
    assert envelope.assistant_message_json == message_json
    assert envelope.request_payload() == sent[0]
    assert envelope.assistant_message()["tool_calls"] == original["tool_calls"]
    assert envelope.assistant_message()["reasoning_details"] == original["reasoning_details"]
    assert [c["provider_call_id"] for c in result.tool_calls] == ["call-0", "call-1", "call-2"]
    for public in (repr(result), json.dumps(asdict(result), default=str),
                   AIGateway._serialize_success_body(result), repr(req), repr(envelope)):
        assert PRIVATE not in public
        assert "private reason" not in public
    assert SYNTHETIC_CREDENTIAL not in envelope.request_payload_json
    assert "Authorization" not in envelope.request_payload_json
    assert envelopes.validate_capture(req, result, request_hash=AIGateway._payload_hash(req)) is envelope
    envelope.assistant_message()["reasoning_details"].clear()
    envelope.request_payload()["tools"].clear()
    assert envelope.assistant_message()["reasoning_details"] == original["reasoning_details"]
    assert envelope.request_payload()["tools"] == sent[0]["tools"]


@pytest.mark.parametrize("mutation", ["request", "normalized_call", "settings", "identity", "missing"])
def test_mutated_request_or_result_cannot_settle_private_history(monkeypatch, mutation):
    req = history_request(monkeypatch)
    body = provider_response()
    public = openrouter.parse_response(body, req, catalog_row(), AIGateway._parse_local_tool_call)
    dispatch = envelopes.capture_dispatch(req, {"model": req.model, "messages": req.messages,
                                               "tools": openrouter.local_tool_definitions(req.model)})
    envelopes.capture_response(req, dispatch, json.dumps(body).encode(), public)
    original_hash = AIGateway._payload_hash(req)
    result = InferenceResult(**public)
    if mutation == "request":
        req.messages[0]["content"] = "changed"
    elif mutation == "normalized_call":
        result.tool_calls[0]["arguments"]["path"] = "changed"
    elif mutation == "identity":
        result.provider_request_id = "other"
    elif mutation == "settings":
        req._native_history_inputs = envelopes.history_inputs(attachment_refs=[], settings={"prompt": "changed"})
    else:
        req._native_envelope_capture = None
    with pytest.raises(ContractError):
        envelopes.validate_capture(req, result, request_hash=original_hash)


@pytest.mark.parametrize("bad", [
    '{"role":"assistant","role":"user"}',
    '{"role":"assistant","vendor":{"x":1,"x":2}}',
    '{"role":"assistant","vendor":NaN}',
    '{"role":"assistant","vendor":Infinity}',
    '{"role":"assistant","vendor":' + '[' * 65 + '0' + ']' * 65 + '}',
])
def test_opaque_source_ambiguity_and_bounds_reject_explicitly(monkeypatch, bad):
    req = history_request(monkeypatch)
    dispatch = envelopes.capture_dispatch(req, {"model": MODEL, "messages": req.messages})
    raw = ('{"id":"synthetic","model":' + json.dumps(MODEL)
           + ',"choices":[{"message":' + bad + ',"finish_reason":"stop"}]}').encode()
    with pytest.raises(ContractError) as err:
        envelopes.capture_response(req, dispatch, raw, {})
    assert "vendor" not in str(err.value)
    assert req._native_envelope_capture is None


@pytest.mark.parametrize("kind", ["input", "payload", "response"])
def test_size_bounds_never_truncate(monkeypatch, kind):
    req = history_request(monkeypatch)
    with pytest.raises(ContractError):
        if kind == "input":
            envelopes.history_inputs(attachment_refs=[], settings={"prompt": "x" * envelopes.MAX_INPUTS_BYTES})
        elif kind == "payload":
            envelopes.capture_dispatch(req, {"messages": "x" * envelopes.MAX_PAYLOAD_BYTES})
        else:
            dispatch = envelopes.capture_dispatch(req, {"model": MODEL, "messages": req.messages})
            envelopes.capture_response(req, dispatch, b" " * (envelopes.MAX_ASSISTANT_BYTES + 1), {})


def test_legacy_scope_compatible_even_when_history_flag_on(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_NATIVE_GENERATION_HISTORY", "1")
    req = request(enable_tools=True)
    _, req._native_generation_claim, _ = rows()
    assert not envelopes.prepare_history_request(req)
    assert envelopes.capture_dispatch(req, {"unused": PRIVATE}) is None
    req._native_generation_claim = None
    assert not envelopes.prepare_history_request(req)


@pytest.mark.parametrize("change", ["server_off", "missing_inputs", "raw_inputs", "missing_claim", "foreign_provider"])
def test_optin_history_requires_explicit_trusted_scope_and_inputs(monkeypatch, change):
    req = history_request(monkeypatch)
    if change == "server_off":
        monkeypatch.delenv("OPENVEGAS_NATIVE_GENERATION_HISTORY")
    elif change == "missing_inputs":
        req._native_history_inputs = None
    elif change == "raw_inputs":
        req._native_history_inputs = {"attachment_refs": [], "settings": {}}
    elif change == "missing_claim":
        req._native_generation_claim = None
    else:
        req.provider = "openai"
    with pytest.raises(ContractError):
        envelopes.prepare_history_request(req)


def test_history_inputs_freeze_owned_refs_and_settings():
    refs = [{"file_id": "11111111-1111-4111-8111-111111111111", "sha256": "a" * 64}]
    settings = {"prompt": PRIVATE, "enable_web_search": True}
    expected = {"attachment_refs": copy.deepcopy(refs), "settings": copy.deepcopy(settings)}
    snapshot = envelopes.history_inputs(attachment_refs=refs, settings=settings)
    refs[0]["sha256"] = "b" * 64
    settings["prompt"] = "changed"
    assert snapshot.values() == expected
    assert PRIVATE not in repr(snapshot)
    with pytest.raises(ContractError):
        envelopes.history_inputs(attachment_refs=[dict(refs[0], mime_type="image/png")], settings={})
    with pytest.raises(ContractError):
        envelopes.history_inputs(attachment_refs=[refs[0], refs[0]], settings={})


@pytest.mark.asyncio
async def test_private_insert_errors_never_echo_sql_failing_row():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    tx = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("Failing row contains " + PRIVATE)))
    with pytest.raises(ContractError) as error:
        await envelopes._insert_private_tx(tx, "insert", PRIVATE)
    assert PRIVATE not in str(error.value)
    assert error.value.__suppress_context__


def continue_request(req, first_payload):
    native = copy.deepcopy(first_payload)
    original = original_message()
    native["messages"] += [original, *[{"role": "tool", "tool_call_id": c["id"], "content": "accepted result"}
                                       for c in original["tool_calls"]]]
    req.messages = native["messages"]
    req._managed_web_context = None  # New generation gateway preflight replaces the previous turn's review.
    req._native_generation_claim = replace(req._native_generation_claim, history_revision=1,
        previous_request_id=str(uuid4()), continuation_payload_json=json.dumps(native))
    return native


def test_native_continuation_payload_keeps_exact_tools_and_opaque_messages(monkeypatch):
    install_review(monkeypatch)
    req = history_request(monkeypatch)
    first = openrouter.build_payload(req, catalog_row(), capabilities())
    native = continue_request(req, first)
    actual = openrouter.build_payload(req, catalog_row(), capabilities())
    assert actual == native
    assert actual["messages"][-4]["reasoning_details"] == original_message()["reasoning_details"]
    assert actual["messages"][-1]["role"] == "tool"
    assert actual["tools"] == first["tools"]
    assert openrouter.input_token_bound(req) == req._managed_openrouter_dispatch.input_tokens
    assert openrouter.input_token_bound(req) >= len(json.dumps(actual, ensure_ascii=False, separators=(",", ":")).encode())


@pytest.mark.parametrize("change", ["price", "tools", "reasoning", "plugin", "unclaimed", "messages", "bound"])
def test_native_continuation_rejects_substitution_without_flattening(monkeypatch, change):
    install_review(monkeypatch)
    req = history_request(monkeypatch)
    native = continue_request(req, openrouter.build_payload(req, catalog_row(), capabilities()))
    config = catalog_row()
    caps = capabilities()
    if change == "price":
        config["cost_input_per_1m"] = "3"
    elif change == "tools":
        native["tools"] = []
    elif change == "reasoning":
        native["reasoning"] = {"effort": "high"}
    elif change == "plugin":
        native["plugins"] = [{"id": "unreviewed"}]
    elif change == "unclaimed":
        req._native_generation_claim = None
    elif change == "messages":
        req.messages = copy.deepcopy(req.messages)
        req.messages[-1]["content"] = "forged result"
    else:
        caps["context_window_tokens"] = 512
    if change in {"tools", "reasoning", "plugin"}:
        req._native_generation_claim = replace(req._native_generation_claim, continuation_payload_json=json.dumps(native))
    with pytest.raises(ContractError):
        openrouter.build_payload(req, config, caps)


@pytest.mark.asyncio
@pytest.mark.parametrize("web", [False, True])
async def test_native_continuation_revalidates_owned_media_and_counts_private_metadata(monkeypatch, web):
    import hashlib

    from tests.test_models.test_openrouter_attachment_request import (
        Setup,
        UploadDB,
        catalog_config,
        image_bytes,
        model_review,
    )
    from tests.test_models.test_openrouter_web_gateway import attachment_web_request

    if web:
        req = await attachment_web_request(monkeypatch)
        config = catalog_row()
        caps = capabilities(context_window_tokens=16384, web_search=True)
    else:
        setup = Setup(UploadDB(), catalog_config(), model_review(), monkeypatch)
        setup.install_review()
        req, _ = await setup.request(tools=True)
        config = setup.config
        caps = capabilities(context_window_tokens=500_000)
    _, claim, _ = rows()
    req._native_generation_claim = replace(claim, history_revision=0)
    refs = [{"file_id": req._managed_attachment_context.prepared.file_ids[0],
             "sha256": hashlib.sha256(image_bytes()).hexdigest()}]
    req._native_history_inputs = envelopes.history_inputs(attachment_refs=refs, settings={"prompt": "test"})
    monkeypatch.setenv("OPENVEGAS_NATIVE_GENERATION_HISTORY", "1")
    assert envelopes.prepare_history_request(req)
    first = openrouter.build_payload(req, config, caps)
    native = continue_request(req, first)
    actual = openrouter.build_payload(req, config, caps)
    assert actual == native
    assert actual["messages"][-4]["reasoning_details"] == original_message()["reasoning_details"]
    assert openrouter.input_token_bound(req) > req._managed_attachment_context.prepared.media_tokens
    if web:
        snapshot = req._managed_web_context
        assert snapshot.payload(req, config) == native
        assert openrouter.build_payload(req, config, caps) == native
        assert req._managed_web_context is snapshot
    # A private claim cannot authorize substituted media: the live owned block validator wins.
    native["messages"][0]["content"][1]["image_url"]["url"] = "data:image/png;base64,AA=="
    req._native_generation_claim = replace(req._native_generation_claim, continuation_payload_json=json.dumps(native))
    from server.services.openrouter_attachments import AttachmentError
    with pytest.raises((AttachmentError, ContractError)):
        openrouter.build_payload(req, config, caps)


@pytest.mark.asyncio
async def test_escaped_credential_in_private_provider_metadata_rejects_not_scrubs(monkeypatch):
    install_review(monkeypatch)
    req = history_request(monkeypatch)
    body = provider_response()
    body["choices"][0]["message"]["reasoning_details"][0]["data"] = SYNTHETIC_CREDENTIAL
    raw = json.dumps(body).replace(SYNTHETIC_CREDENTIAL, ''.join(f'\\u{ord(c):04x}' for c in SYNTHETIC_CREDENTIAL)).encode()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw))) as client:
        with pytest.raises(openrouter.OpenRouterFailure) as error:
            await openrouter.complete(req, SYNTHETIC_CREDENTIAL, model_config=catalog_row(), capabilities=capabilities(),
                parse_tool=AIGateway._parse_local_tool_call, client=client)
    assert error.value.diagnostic_reason == "reflected_credential"
    assert req._native_envelope_capture is None


@pytest.mark.parametrize("number,supported", [("0.123456789012345678901234567890", False), ("1e309", False),
                                             ("0.125", True), ("1e-6", True), ("12345678901234567890", True)])
def test_opaque_numeric_source_is_retained_but_never_silently_rounded(monkeypatch, number, supported):
    req = history_request(monkeypatch)
    body = provider_response()
    raw = json.dumps(body).replace('"role": "assistant"', '"role": "assistant", "vendor_number":' + number).encode()
    public = openrouter.parse_response(body, req, catalog_row(), AIGateway._parse_local_tool_call)
    dispatch = envelopes.capture_dispatch(req, {"model": MODEL, "messages": req.messages})
    envelopes.capture_response(req, dispatch, raw, public)
    envelope = req._native_envelope_capture
    assert '"vendor_number":' + number in envelope.assistant_message_json
    assert envelope.continuation_safe is supported
    assert envelope.continuation_block_reason == (None if supported else "native_history_numeric_precision")
    if not supported:
        with pytest.raises(ContractError):
            envelope.assistant_message()
        # Even a forged in-memory claim must be rejected before the wire encoder can round it.
        req._native_generation_claim = replace(req._native_generation_claim, history_revision=1,
            previous_request_id=str(uuid4()), continuation_payload_json='{"model":' + json.dumps(MODEL)
            + ',"messages":[],"opaque":' + number + '}')
        req.messages = []
        with pytest.raises(ContractError):
            openrouter.build_payload(req, catalog_row(), capabilities())


@pytest.mark.asyncio
async def test_private_dispatch_bound_rejects_before_any_http_request(monkeypatch):
    req = history_request(monkeypatch)
    monkeypatch.setattr(openrouter, "build_payload", lambda *_: {"messages": "x" * envelopes.MAX_PAYLOAD_BYTES})
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
        with pytest.raises(ContractError):
            await openrouter.complete(req, SYNTHETIC_CREDENTIAL, model_config=catalog_row(), capabilities=capabilities(),
                parse_tool=AIGateway._parse_local_tool_call, client=client)
    assert calls == []
