"""Reject ambiguous supplier envelopes before tool parsing or billing."""

import json
from unittest.mock import Mock

import httpx
import pytest

import test_openrouter as transport
import test_openrouter_versioned_tools as native
from openvegas.gateway import openrouter


@pytest.mark.asyncio
@pytest.mark.parametrize("original,replacement", [
    ('"id": "synthetic-receipt"', '"id":"gen-other", "id":"synthetic-receipt"'),
    ('"id": "synthetic-receipt"', r'"i\u0064":"gen-other", "id":"synthetic-receipt"'),
    ('"model":', '"model":"fixture/wrong-model", "model":'),
    ('"usage":', '"usage":{"cost":"1"}, "usage":'),
    ('"cost": "0.000020"', '"cost":"1", "cost":"0.000020"'),
    ('"prompt_tokens": 11', '"prompt_tokens":999999, "prompt_tokens":11'),
    ('"finish_reason": "stop"', '"finish_reason":"error", "finish_reason":"stop"'),
    ('"content": "Answer"', '"content":"private-conflicting-answer", "content":"Answer"'),
])
async def test_duplicate_response_members_fail_closed(monkeypatch, original, replacement):
    raw = json.dumps(transport.response())
    assert raw.count(original) == 1
    raw = raw.replace(original, replacement)
    sent, events = [], []
    monkeypatch.setattr(openrouter, "emit_metric", lambda name, tags: events.append((name, tags)))

    def supplier(req):
        sent.append(req)
        return httpx.Response(200, text=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(supplier)) as client:
        with pytest.raises(openrouter.OpenRouterFailure) as error:
            await transport.complete(transport.request(), client)
    assert len(sent) == 1
    assert error.value.diagnostic_reason == "ambiguous_response"
    assert error.value.provider_request_id is None
    assert events == [("openrouter_failure_total", {"reason": "ambiguous_response"})]
    assert "private-conflicting-answer" not in str(error.value) + str(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("id", "call-other"), ("name", "Bash")])
async def test_duplicate_native_tool_fields_never_parse_or_capture(field, value):
    req = native.request(native=True)
    raw = json.dumps(native.response(req, native.assistant(native.FLAT_V2)))
    original = '"id": "call-original"' if field == "id" else '"name": "Read"'
    raw = raw.replace(original, json.dumps(field) + ":" + json.dumps(value) + "," + original)
    parse_tool = Mock(side_effect=AssertionError("Ambiguous tool must not be parsed"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=raw)),
    ) as client:
        with pytest.raises(openrouter.OpenRouterFailure) as error:
            await openrouter.complete(req, native.KEY, model_config=native.CONFIG,
                                      capabilities=native.CAPS, parse_tool=parse_tool, client=client)
    assert error.value.diagnostic_reason == "ambiguous_response"
    assert req._native_envelope_capture is None
    parse_tool.assert_not_called()
    assert native.PRIVATE not in str(error.value)


@pytest.mark.asyncio
async def test_identical_keys_in_separate_objects_and_quoted_text_are_not_duplicates():
    body = transport.response()
    body["choices"][0]["message"]["content"] = '{"id":1,"id":2} is example source text'
    body["metadata"] = [{"id": "one"}, {"id": "two"}]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    ) as client:
        result = await transport.complete(transport.request(), client)
    assert result["text"] == body["choices"][0]["message"]["content"]
    assert result["provider_request_id"] == "synthetic-receipt"
