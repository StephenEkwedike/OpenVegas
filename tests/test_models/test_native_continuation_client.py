"""CLI identity guards and actual ask/stream transport, with no supplier calls."""
import json
from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from openvegas.agent.native_scope_client import NativeGenerationSession
from openvegas.client import OpenVegasClient


@pytest.fixture
def scope():
    return {"run_id": str(uuid4()), "runtime_session_id": str(uuid4()),
            "expected_run_version": 1, "expected_valid_actions_signature": "sha256:" + "a" * 64}


@pytest.fixture
def options():
    return {"provider": "openrouter", "model": "fixture/model", "enable_tools": True,
            "enable_web_search": False, "attachments": [], "reasoning_effort": None}


def reply(scope, revision=0, calls=True):
    request_id = str(uuid4())
    return {"text": "fixture", "completion_status": "incomplete" if calls else "complete",
            "native_generation": {"scope_version": 1, "run_id": scope["run_id"],
                "runtime_session_id": scope["runtime_session_id"], "original_turn_scope_verified": True,
                "continuation_supported": calls, "inference_request_id": request_id,
                "history_revision": revision},
            "tool_calls": [{"provider_call_id": "call1", "native_inference_request_id": request_id}]
            if calls else []}


def prepare(session, scope, options, key="first"):
    return session.prepare(key=key, scope=scope, options=options, history=True)


def test_only_acknowledged_native_receipt_opens_the_next_revision(scope, options):
    s = NativeGenerationSession()
    first = prepare(s, scope, options)
    assert first == {"native_scope": scope, "native_history": True}
    with pytest.raises(ValueError, match="unconfirmed"):
        prepare(s, scope, options, "second")
    answer = reply(scope)
    s.validate_result(answer)
    next_scope = {**scope, "expected_run_version": 5}
    second = prepare(s, next_scope, options, "second")
    assert second["native_continuation"] == {"previous_inference_request_id": answer["native_generation"]["inference_request_id"],
                                              "expected_history_revision": 0}
    assert second["native_scope"] == next_scope
    with pytest.raises(ValueError, match="unconfirmed"):
        prepare(s, next_scope, options, "third")
    s.validate_result(reply(scope, revision=1, calls=False))
    with pytest.raises(ValueError, match="final"):
        prepare(s, next_scope, options, "third")


def test_payloads_are_snapshots_and_same_key_recovery_never_refreshes_projection(scope, options):
    s = NativeGenerationSession()
    first = prepare(s, scope, options)
    first["native_scope"]["run_id"] = str(uuid4())
    assert prepare(s, {**scope, "expected_run_version": 3}, options)["native_scope"] == scope
    result = reply(scope)
    expected = deepcopy(result)
    s.validate_result(result)
    result["native_generation"]["inference_request_id"] = str(uuid4())
    followup = prepare(s, scope, options, "next")
    assert followup["native_continuation"]["previous_inference_request_id"] == expected["native_generation"]["inference_request_id"]
    followup["native_continuation"]["expected_history_revision"] = 999
    assert prepare(s, scope, options, "next")["native_continuation"]["expected_history_revision"] == 0


@pytest.mark.parametrize("name,value", [("model", "different/model"), ("provider", "gemini"),
    ("enable_tools", False), ("enable_web_search", True), ("attachments", [str(uuid4())]),
    ("reasoning_effort", "high")])
def test_no_silent_model_feature_or_attachment_change(scope, options, name, value):
    s = NativeGenerationSession()
    prepare(s, scope, options)
    s.validate_result(reply(scope))
    with pytest.raises(ValueError, match="cannot change model"):
        prepare(s, scope, {**options, name: value}, "next")


@pytest.mark.parametrize("field,value", [("history_revision", True), ("history_revision", 1),
    ("history_revision", "0"), ("history_revision", None), ("continuation_supported", False)])
def test_bad_history_receipt_cannot_unlock_a_paid_continuation(scope, options, field, value):
    s = NativeGenerationSession()
    prepare(s, scope, options)
    result = reply(scope)
    result["native_generation"][field] = value
    with pytest.raises(ValueError):
        s.validate_result(result)
    with pytest.raises(ValueError, match="unconfirmed"):
        prepare(s, scope, options, "next")


def test_duplicate_native_calls_and_rebound_replay_are_rejected(scope, options):
    s = NativeGenerationSession()
    prepare(s, scope, options)
    result = reply(scope)
    result["tool_calls"] *= 2
    with pytest.raises(ValueError):
        s.validate_result(result)
    result = reply(scope)
    s.validate_result(result)
    with pytest.raises(ValueError, match="replay changed"):
        s.validate_result(reply(scope))
    prepare(s, scope, options, "next")
    with pytest.raises(ValueError, match="older native command"):
        prepare(s, scope, options)
    with pytest.raises(ValueError, match="fall back"):
        s.prepare(key="other", scope=scope, options=options)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_client_copies_continuation_identity_and_keeps_legacy_shape(scope, stream):
    continuation = {"previous_inference_request_id": str(uuid4()), "expected_history_revision": 0}
    original = deepcopy(continuation)
    client = object.__new__(OpenVegasClient)
    client.base_url, client.token = "https://offline.invalid", "fixture"
    client._request = AsyncMock(return_value={"text": "fixture"})
    client._refresh_single_flight = AsyncMock()
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            continuation["expected_history_revision"] = 999
            scope["expected_run_version"] = 999
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text='event: response.completed\ndata: {"text":"fixture"}\n\n')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client._http_client = http
        kwargs = {"idempotency_key": "same", "native_scope": scope,
                  "native_history": True, "native_continuation": continuation}
        if stream:
            assert [x async for x in client.ask_stream("", "openrouter", "fixture/model", **kwargs)]
            assert len(payloads) == 2 and payloads[0] == payloads[1]
            body = payloads[0]
        else:
            await client.ask("", "openrouter", "fixture/model", **kwargs)
            body = client._request.call_args.kwargs["json"]
        assert body["native_continuation"] == original and body["native_history"] is True
    await client.ask("legacy", "openrouter", "fixture/model")
    assert not ({"native_scope", "native_history", "native_continuation"} & client._request.call_args.kwargs["json"].keys())


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"native_history": True}, {"native_history": "true"},
    {"native_continuation": {"previous_inference_request_id": str(uuid4()), "expected_history_revision": 0}},
])
async def test_unscoped_history_is_rejected_before_network(kwargs):
    client = object.__new__(OpenVegasClient)
    client._request = AsyncMock()
    with pytest.raises(ValueError):
        await client.ask("", "openrouter", "fixture/model", idempotency_key="key", **kwargs)
    client._request.assert_not_awaited()


def test_unrepresentable_vendor_state_is_explicit_not_silently_rounded(scope, options):
    session = NativeGenerationSession()
    prepare(session, scope, options)
    result = reply(scope)
    result["native_generation"].update(continuation_supported=False,
                                        continuation_block_reason="native_history_numeric_precision")
    with pytest.raises(ValueError, match="precision loss"):
        session.validate_result(result)
    with pytest.raises(ValueError, match="unconfirmed"):
        prepare(session, scope, options, "next")
