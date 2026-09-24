"""Strict public handoff transport with synthetic HTTP, never real credentials."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from openvegas import client as client_module
from openvegas.client import APIError, OpenVegasClient
from openvegas.contracts.native_handoff import (
    ConfirmNativeHandoff,
    NativeHandoffRef,
    NativeHandoffResponse,
    PrepareNativeHandoff,
)

CANARY = "private-input-must-not-be-reflected"


def scope():
    return {"run_id": str(uuid4()), "runtime_session_id": str(uuid4()),
            "expected_run_version": 1, "expected_valid_actions_signature": "sha256:" + "a" * 64}


@pytest.fixture
def case():
    selection = {"provider": "openrouter", "model": "fixture/exact-v1", "enable_tools": True,
                 "enable_web_search": False, "reasoning_effort": None, "max_tokens": 1024}
    ref = {"handoff_id": str(uuid4()), "handoff_sha256": "b" * 64}
    destination = scope()
    return {
        "prepare": {"source_scope": scope(), "source_ref": {
            "previous_inference_request_id": str(uuid4()), "expected_history_revision": 3},
            "selection": selection, "idempotency_key": "prepare-fixed"},
        "confirm": {**ref, "destination_scope": destination, "idempotency_key": "confirm-fixed"},
        "response": {**ref, "selection": deepcopy(selection), "expires_at": "2026-01-01T00:00:00+00:00",
                     "task_count": 2, "file_count": 2, "unique_file_count": 1,
                     "observation_count": 2, "destination_scope": destination},
    }


@asynccontextmanager
async def client_for(monkeypatch, handler):
    monkeypatch.setattr(client_module, "token_expires_soon", lambda *_args, **_kwargs: False)
    client = object.__new__(OpenVegasClient)
    client.base_url, client.token = "https://offline.invalid", "synthetic-token"
    client._session_snapshot = {}
    client._refresh_single_flight = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client._http_client = http
        yield client


async def send(client, stream, **kwargs):
    provider = kwargs.pop("provider", "openrouter")
    if stream:
        return [item async for item in client.ask_stream("Current prompt", provider, "fixture/exact-v1", **kwargs)]
    return await client.ask("Current prompt", provider, "fixture/exact-v1", **kwargs)


def inference_options(case, *, continuing=False):
    options = {"idempotency_key": "inference-fixed", "native_scope": deepcopy(case["confirm"]["destination_scope"]),
               "native_history": True, "native_handoff": {name: case["confirm"][name]
                   for name in ("handoff_id", "handoff_sha256")}, "max_tokens": 1024,
               "enable_tools": True, "enable_web_search": False, "persist_context": False,
               "conversation_mode": "ephemeral", "attachments": [str(uuid4())]}
    if continuing:
        options["native_continuation"] = {"previous_inference_request_id": str(uuid4()), "expected_history_revision": 2}
    else:
        options["native_user_text"] = "Original current input.\r\nExact bytes."
    return options


def answer(stream):
    if stream:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text='event: response.completed\ndata: {"payload":{"status":"ok","text":"fixture"}}\n\n')
    return httpx.Response(200, json={"text": "fixture"})


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["prepare", "confirm"])
@pytest.mark.parametrize("typed", [False, True])
async def test_typed_methods_snapshot_before_auth_await_and_return_only_public_dto(case, monkeypatch, operation, typed):
    model = PrepareNativeHandoff if operation == "prepare" else ConfirmNativeHandoff
    original = model.model_validate(case[operation]) if typed else deepcopy(case[operation])
    expected = model.model_validate(original).model_dump(mode="json")
    seen = []

    def handle(request):
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert request.url.path == "/agent/native-handoffs/" + operation
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=case["response"])

    async def mutate(**_kwargs):
        if typed:
            field = original.source_scope if operation == "prepare" else original.destination_scope
            object.__setattr__(field, "run_id", str(uuid4()))
            if operation == "prepare":
                object.__setattr__(original.selection, "max_tokens", 99)
        else:
            original.clear()
            original[CANARY] = CANARY

    async with client_for(monkeypatch, handle) as client:
        monkeypatch.setattr(client_module, "token_expires_soon", lambda *_args, **_kwargs: True)
        client._refresh_single_flight.side_effect = mutate
        result = await getattr(client, "native_handoff_" + operation)(original)
        client._refresh_single_flight.assert_awaited_once()
    assert seen == [expected]
    assert type(result) is NativeHandoffResponse
    assert result == NativeHandoffResponse.model_validate(case["response"])
    # Expired replay is a valid summary, not renewed dispatch authorization.
    assert result.expires_at.year == 2026


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["prepare", "confirm"])
async def test_auth_retry_preserves_identical_handoff_operation(case, monkeypatch, operation):
    seen = []
    original = deepcopy(case[operation])

    def handle(request):
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            original["idempotency_key"] = "do-not-rekey"
            return httpx.Response(401, json={"detail": CANARY})
        return httpx.Response(200, json=case["response"])

    async with client_for(monkeypatch, handle) as client:
        await getattr(client, "native_handoff_" + operation)(original)
        client._refresh_single_flight.assert_awaited_once()
    assert len(seen) == 2 and seen[0] == seen[1]
    assert seen[0]["idempotency_key"] == case[operation]["idempotency_key"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["selection", "handoff_id", "handoff_sha256", "destination", "projection", "missing_destination"])
async def test_response_binding_mismatch_is_rejected(case, monkeypatch, change):
    response = deepcopy(case["response"])
    operation = "prepare" if change == "selection" else "confirm"
    if change == "selection":
        response["selection"]["max_tokens"] = 99
    elif change == "handoff_id":
        response[change] = str(uuid4())
    elif change == "handoff_sha256":
        response[change] = "c" * 64
    elif change == "missing_destination":
        response["destination_scope"] = None
    elif change == "projection":
        response["destination_scope"]["expected_run_version"] += 1
    else:
        response["destination_scope"]["run_id"] = str(uuid4())
    async with client_for(monkeypatch, lambda _r: httpx.Response(200, json=response)) as client:
        with pytest.raises(APIError) as error:
            await getattr(client, "native_handoff_" + operation)(case[operation])
    assert error.value.status == 502 and error.value.data == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["prepare", "confirm"])
@pytest.mark.parametrize("field", ["history", "system", "tool", "signature", "binding", CANARY])
async def test_private_request_fields_fail_sanitized_before_http(case, monkeypatch, operation, field):
    seen = []
    raw = {**case[operation], field: CANARY}
    async with client_for(monkeypatch, lambda r: seen.append(r)) as client:
        with pytest.raises(ValueError) as error:
            await getattr(client, "native_handoff_" + operation)(raw)
    assert not seen and CANARY not in str(error.value)
    assert CANARY not in repr(error.value.errors()) and CANARY not in error.value.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["prepare", "confirm"])
@pytest.mark.parametrize("fault", ["private", "nested", "malformed", "http", "timeout"])
async def test_response_failures_never_reflect_private_data_or_fallback(case, monkeypatch, operation, fault):
    seen = []

    def handle(request):
        seen.append(request.url.path)
        if fault == "timeout":
            raise httpx.ReadTimeout(CANARY)
        if fault == "http":
            return httpx.Response(409, json={"detail": CANARY, "history": CANARY})
        if fault == "malformed":
            return httpx.Response(200, text=CANARY)
        response = deepcopy(case["response"])
        if fault == "nested":
            response["selection"]["binding"] = CANARY
        else:
            response["history"] = CANARY
        return httpx.Response(200, json=response)

    async with client_for(monkeypatch, handle) as client:
        with pytest.raises(APIError) as error:
            await getattr(client, "native_handoff_" + operation)(case[operation])
    assert seen == ["/agent/native-handoffs/" + operation]
    assert error.value.status == {"timeout": 503, "http": 409}.get(fault, 502)
    assert CANARY not in str(error.value) and error.value.data == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("field", ["enable_tools", "enable_web_search", "persist_context"])
@pytest.mark.parametrize("value", [0, 1, "false", "true"])
async def test_handoff_flags_cannot_be_coerced(case, monkeypatch, stream, field, value):
    options = {**inference_options(case), field: value}
    async with client_for(monkeypatch, lambda _r: pytest.fail("invalid flags reached HTTP")) as client:
        with pytest.raises(ValueError, match="Invalid native handoff"):
            await send(client, stream, **options)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    'data: {"type":"response.error","payload":{"detail":"' + CANARY + '"}}\n\n',
    'event: response.completed\ndata: {"type":"response.error","payload":{"detail":"' + CANARY + '"}}\n\n',
    'event: response.delta\ndata: {"type":"response.completed"}\n\n',
    'data: ["' + CANARY + '"]\n\n',
    '', 'data: {"type":"response.started"}\n\n',
])
async def test_error_envelopes_and_truncated_streams_are_uncertain_not_success(case, monkeypatch, body):
    yielded = []
    async with client_for(monkeypatch, lambda _r: httpx.Response(200,
        headers={"content-type": "text/event-stream"}, text=body)) as client:
        with pytest.raises(APIError) as error:
            async for item in client.ask_stream("Current prompt", "openrouter", "fixture/exact-v1",
                                                **inference_options(case)):
                yielded.append(item)
    assert CANARY not in str(error.value) and CANARY not in json.dumps(yielded)
    assert not any(item["data"].get("type") == "response.completed" for item in yielded)


@pytest.mark.asyncio
async def test_bound_stream_resets_sse_event_name_between_data_only_frames(case, monkeypatch):
    body = ('event: response.started\ndata: {"type":"response.started"}\n\n'
            'data: {"type":"response.completed","payload":{"status":"ok","text":"complete"}}\n\n')
    async with client_for(monkeypatch, lambda _r: httpx.Response(200,
        headers={"content-type": "text/event-stream"}, text=body)) as client:
        result = await send(client, True, **inference_options(case))
    assert len(result) == 2 and result[-1]["data"]["type"] == "response.completed"
    assert result[-1]["event"] == "response.completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [{}, {"payload": None}, {"payload": []}, {"payload": {}},
                                   {"payload": {"status": "unknown"}}, {"payload": {"status": True}}])
async def test_completion_label_without_valid_success_payload_is_rejected(case, monkeypatch, value):
    body = "data: " + json.dumps({"type": "response.completed", **value}) + "\n\n"
    yielded = []
    async with client_for(monkeypatch, lambda _r: httpx.Response(200,
        headers={"content-type": "text/event-stream"}, text=body)) as client:
        with pytest.raises(APIError):
            async for item in client.ask_stream("Current prompt", "openrouter", "fixture/exact-v1",
                                                **inference_options(case)):
                yielded.append(item)
    assert not yielded


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("continuing", [False, True])
@pytest.mark.parametrize("tokens", [1, 1_000_000])
async def test_ask_and_stream_bind_exact_public_snapshot_across_auth_retry(case, monkeypatch, stream, continuing, tokens):
    options = inference_options(case, continuing=continuing)
    options["max_tokens"] = tokens
    expected = deepcopy(options)
    seen = []

    def handle(request):
        assert request.headers["authorization"] == "Bearer synthetic-token"
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            options["native_handoff"]["handoff_id"] = str(uuid4())
            options["native_scope"]["expected_run_version"] += 99
            options["attachments"].append(str(uuid4()))
            if continuing:
                options["native_continuation"]["expected_history_revision"] += 99
            return httpx.Response(401, json={"detail": "synthetic auth retry"})
        return answer(stream)

    async with client_for(monkeypatch, handle) as client:
        assert await send(client, stream, **options)
        client._refresh_single_flight.assert_awaited_once()
    assert len(seen) == 2 and seen[0] == seen[1]
    assert seen[0] == {"prompt": "Current prompt", "provider": "openrouter", "model": "fixture/exact-v1", **expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("change", ["missing_handoff", "missing_tokens", "no_scope", "no_history", "nonbool_history",
    "no_key", "other_provider", "missing_user", "continuation_user", "false_tokens", "true_tokens", "zero_tokens",
    "negative_tokens", "large_tokens", "float_tokens", "string_tokens", "private_ref", "zero_id", "bad_hash",
    "private_scope", "private_continuation"])
async def test_bound_request_invalid_options_fail_before_http(case, monkeypatch, stream, change):
    options = inference_options(case)
    if change == "missing_handoff":
        options["native_handoff"] = None
    elif change == "missing_tokens":
        options["max_tokens"] = None
    elif change == "no_scope":
        options["native_scope"] = None
    elif change == "no_history":
        options["native_history"] = False
    elif change == "nonbool_history":
        options["native_history"] = 1
    elif change == "no_key":
        options["idempotency_key"] = None
    elif change == "other_provider":
        options["provider"] = "openai"
    elif change == "missing_user":
        options.pop("native_user_text")
    elif change == "continuation_user":
        options["native_continuation"] = {"previous_inference_request_id": str(uuid4()), "expected_history_revision": 0}
    elif change.endswith("_tokens"):
        options["max_tokens"] = {"false_tokens": False, "true_tokens": True, "zero_tokens": 0,
            "negative_tokens": -1, "large_tokens": 1_000_001, "float_tokens": 1024.0, "string_tokens": "1024"}[change]
    elif change == "zero_id":
        options["native_handoff"]["handoff_id"] = "00000000-0000-0000-0000-000000000000"
    elif change == "bad_hash":
        options["native_handoff"]["handoff_sha256"] = "B" * 64
    elif change == "private_scope":
        options["native_scope"][CANARY] = CANARY
    elif change == "private_continuation":
        options.pop("native_user_text")
        options["native_continuation"] = {CANARY: CANARY}
    else:
        options["native_handoff"][CANARY] = CANARY
    seen = []
    async with client_for(monkeypatch, lambda r: seen.append(r)) as client:
        with pytest.raises(ValueError) as error:
            await send(client, stream, **options)
    assert not seen and CANARY not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("history", [False, True])
async def test_legacy_and_existing_native_requests_omit_new_fields(case, monkeypatch, stream, history):
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return answer(stream)

    options = {"idempotency_key": "legacy-fixed", "native_handoff": None, "max_tokens": None}
    if history:
        options.update(native_history=True, native_scope=case["confirm"]["destination_scope"])
    async with client_for(monkeypatch, handle) as client:
        await send(client, stream, **options)
    assert "native_handoff" not in seen[0] and "max_tokens" not in seen[0]
    assert seen[0] == {"prompt": "Current prompt", "provider": "openrouter", "model": "fixture/exact-v1",
                      **{key: value for key, value in options.items() if value is not None}}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("timeout", [False, True])
async def test_bound_inference_http_failure_is_sanitized_without_fallback(case, monkeypatch, stream, timeout):
    seen = []

    def handle(request):
        seen.append(request.url.path)
        if timeout:
            raise httpx.ReadTimeout(CANARY)
        return httpx.Response(409, json={"detail": CANARY, "binding": CANARY})

    async with client_for(monkeypatch, handle) as client:
        with pytest.raises(APIError) as error:
            await send(client, stream, **inference_options(case))
    assert len(seen) == 1 and error.value.status == (503 if timeout else 409)
    assert CANARY not in str(error.value) and error.value.data == {}


@pytest.mark.asyncio
async def test_typed_reference_is_supported(case, monkeypatch):
    options = inference_options(case)
    options["native_handoff"] = NativeHandoffRef.model_validate(options["native_handoff"])
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return answer(False)

    async with client_for(monkeypatch, handle) as client:
        await send(client, False, **options)
    assert seen[0]["native_handoff"] == options["native_handoff"].model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["error_event", "failed_completion", "malformed"])
async def test_bound_stream_error_does_not_yield_reflected_diagnostics(case, monkeypatch, fault):
    if fault == "malformed":
        body = "event: response.completed\ndata: " + CANARY + "\n\n"
    else:
        event = "response.error" if fault == "error_event" else "response.completed"
        body = "event: " + event + "\ndata: " + json.dumps({
            "payload": {"status": "error", "detail": CANARY, "history": CANARY}}) + "\n\n"
    yielded = []
    async with client_for(monkeypatch, lambda _r: httpx.Response(200,
        headers={"content-type": "text/event-stream"}, text=body)) as client:
        with pytest.raises(APIError) as error:
            async for item in client.ask_stream("Current prompt", "openrouter", "fixture/exact-v1",
                                                **inference_options(case)):
                yielded.append(item)
    assert not yielded and error.value.status == 502
    assert CANARY not in str(error.value) and error.value.data == {}
