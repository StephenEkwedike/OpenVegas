from __future__ import annotations  # noqa: I001 - Partial staging trees change Ruff's first-party import discovery.

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.inference import AIGateway, InferenceRequest, InferenceResult
from openvegas.gateway.mistral import discovery_candidates


def request(provider="mistral", **kwargs):
    return InferenceRequest(
        account_id="user:u1",
        provider=provider,
        model="reviewed-test-model",
        messages=[{"role": "user", "content": "hello"}],
        **kwargs,
    )


def mistral_body():
    return {
        "id": "response-1",
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": "answer"}}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "mistral"])
async def test_four_provider_text_and_buffered_stream_contract(provider, monkeypatch):
    calls = []
    chat_create = AsyncMock(
        return_value=SimpleNamespace(
            id="response-1",
            choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
        )
    )
    anthropic_create = AsyncMock(
        return_value=SimpleNamespace(
            id="response-1",
            content=[SimpleNamespace(text="answer")],
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )
    )
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(
            AsyncOpenAI=lambda **kw: SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create))
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(
            AsyncAnthropic=lambda **kw: SimpleNamespace(
                messages=SimpleNamespace(create=anthropic_create)
            )
        ),
    )
    def handler(req):
        if provider == "gemini":
            assert req.url == "https://generativelanguage.googleapis.com/v1beta/models/reviewed-test-model:generateContent"
            assert req.headers["x-goog-api-key"] == "fake-server-key"
            calls.append(json.loads(req.content))
            return httpx.Response(200, json={
                "responseId": "response-1",
                "candidates": [{"finishReason": "STOP", "content": {
                    "role": "model", "parts": [{"text": "answer"}],
                }}],
                "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7},
            })
        assert req.url == "https://api.mistral.ai/v1/chat/completions"
        assert req.headers["Authorization"] == "Bearer fake-server-key"
        calls.append(json.loads(req.content))
        return httpx.Response(200, json=mistral_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = AIGateway(None, None, None, http_client=client)
        ctx = SimpleNamespace(provider_api_key="fake-server-key")
        gateway._prepare_inference_execution = AsyncMock(return_value=(ctx, None))
        gateway._finalize_inference_execution = AsyncMock(
            side_effect=lambda ctx, req, result: result
        )
        gateway._cleanup_inference_after_failure = AsyncMock()
        req = request(provider, max_tokens=64)
        events = [e async for e in gateway.stream_infer(req)]
        assert [e["type"] for e in events] == ["text_delta", "completed"]
        assert events[0]["text"] == "answer"
        result = events[-1]["result"]
        assert (result.input_tokens, result.output_tokens, result.provider_request_id) == (
            11,
            7,
            "response-1",
        )
        gateway._finalize_inference_execution.assert_awaited_once()
        gateway._cleanup_inference_after_failure.assert_not_awaited()
        if provider == "openai":
            assert chat_create.call_args.kwargs["messages"] == req.messages
            assert chat_create.call_args.kwargs["max_completion_tokens"] == 64
        elif provider == "anthropic":
            assert anthropic_create.call_args.kwargs == {
                "model": req.model,
                "messages": req.messages,
                "max_tokens": 64,
            }
        elif provider == "gemini":
            assert len(calls) == 1
            assert calls[0]["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
            assert calls[0]["generationConfig"]["maxOutputTokens"] == 64
        else:
            assert calls == [
                {"model": req.model, "messages": req.messages, "max_tokens": 64, "stream": False}
            ]
        assert not client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [
        (400, APIErrorCode.INVALID_TRANSITION),
        (404, APIErrorCode.INVALID_TRANSITION),
        (401, APIErrorCode.PROVIDER_UNAVAILABLE),
        (429, APIErrorCode.PROVIDER_UNAVAILABLE),
        (500, APIErrorCode.PROVIDER_UNAVAILABLE),
        (302, APIErrorCode.PROVIDER_UNAVAILABLE),
    ],
)
async def test_mistral_errors_do_not_retry_or_expose_body(status, code):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            status, text="secret-provider-body", headers={"Location": "https://evil.invalid"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = AIGateway(None, None, None, client)
        with pytest.raises(ContractError) as exc:
            await gateway._route_to_provider(request(), "fake-key")
    assert exc.value.code == code
    assert "secret-provider-body" not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["usage", "negative", "float", "boolean", "tool", "content", "finish"]
)
async def test_mistral_never_invents_usage_or_tool_success(mutation):
    body = mistral_body()
    if mutation == "usage":
        body.pop("usage")
    elif mutation in {"negative", "float", "boolean"}:
        body["usage"]["prompt_tokens"] = {"negative": -1, "float": 1.2, "boolean": True}[mutation]
    elif mutation == "tool":
        body["choices"][0]["message"]["tool_calls"] = [{"function": "Bash"}]
    elif mutation == "content":
        body["choices"][0]["message"]["content"] = [{"type": "thinking", "text": "private"}]
    else:
        body["choices"][0]["finish_reason"] = "tool_calls"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))
    ) as client:
        gateway = AIGateway(None, None, None, client)
        with pytest.raises(ContractError, match="usage payload"):
            await gateway._route_to_provider(request(), "fake-key")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tools", "web", "image", "tool_history", "empty", "budget"])
async def test_mistral_unsupported_inputs_rejected_before_network(change):
    req = request()
    if change == "tools":
        req.enable_tools = True
    if change == "web":
        req.enable_web_search = True
    if change == "image":
        req.messages[0]["content"] = [{"type": "image_url"}]
    if change == "tool_history":
        req.messages[0]["role"] = "tool"
    if change == "empty":
        req.messages = []
    if change == "budget":
        req.max_tokens = 0

    def forbidden(req):
        raise AssertionError("Unexpected provider call")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ContractError):
            await AIGateway(None, None, None, client)._route_to_provider(req, "fake-key")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.CancelledError, httpx.ReadTimeout])
async def test_mistral_failure_releases_gateway_hold_without_settlement(failure):
    async def fail(req):
        raise failure("test failure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        gateway = AIGateway(None, None, None, client)
        ctx = SimpleNamespace(provider_api_key="fake-key")
        gateway._prepare_inference_execution = AsyncMock(return_value=(ctx, None))
        gateway._finalize_inference_execution = AsyncMock()
        gateway._cleanup_inference_after_failure = AsyncMock()
        with pytest.raises(
            asyncio.CancelledError if failure is asyncio.CancelledError else ContractError
        ):
            _ = [event async for event in gateway.stream_infer(request())]
        gateway._cleanup_inference_after_failure.assert_awaited_once_with(ctx)
        gateway._finalize_inference_execution.assert_not_awaited()


@pytest.mark.asyncio
async def test_mistral_stream_close_and_idempotent_replay_do_not_double_settle():
    gateway = AIGateway(None, None, None)
    ctx = SimpleNamespace(provider_api_key="fake-key")
    result = InferenceResult("answer", 11, 7, v_cost=Decimal("0.1"))
    gateway._prepare_inference_execution = AsyncMock(return_value=(ctx, None))
    gateway._route_to_provider = AsyncMock(return_value=result)
    gateway._finalize_inference_execution = AsyncMock(return_value=result)
    gateway._cleanup_inference_after_failure = AsyncMock()
    stream = gateway.stream_infer(request())
    assert await anext(stream) == {"type": "text_delta", "text": "answer"}
    await stream.aclose()
    gateway._cleanup_inference_after_failure.assert_not_awaited()
    gateway._finalize_inference_execution.assert_awaited_once()
    gateway._route_to_provider.reset_mock()
    gateway._prepare_inference_execution = AsyncMock(return_value=(ctx, result))
    events = [event async for event in gateway.stream_infer(request())]
    assert events[-1]["result"].v_cost == Decimal("0.1")
    gateway._route_to_provider.assert_not_awaited()
    gateway._finalize_inference_execution.assert_awaited_once()


def test_discovery_does_not_enable_future_models_or_fabricate_prices():
    cards = {
        "data": [
            {
                "id": "future-model",
                "capabilities": {"completion_chat": True},
                "max_context_length": 9999,
            },
            {"id": "fim-only", "capabilities": {"completion_fim": True}},
        ]
    }
    candidates = discovery_candidates(cards)
    assert len(candidates) == 1
    assert candidates[0]["selectable"] is False
    assert candidates[0]["review_required"] is True
    assert not any("price" in key or "cost" in key for key in candidates[0])
    with pytest.raises(ValueError):
        discovery_candidates({"data": cards["data"] * 501})


@pytest.mark.asyncio
async def test_mistral_settlement_replay_retains_exact_catalog_charge():
    class SettlementDB:
        def __init__(self):
            self.request = {"status": "processing"}
            self.usage_inserts = 0

        @asynccontextmanager
        async def transaction(self):
            yield self

        async def fetchrow(self, query, *args):
            if "inference_requests" in query:
                return dict(self.request)
            if "inference_preauthorizations" in query:
                return {"status": "reserved"}
            raise AssertionError(query)

        async def execute(self, query, *args):
            if "INSERT INTO inference_usage" in query:
                self.usage_inserts += 1
                assert args[5:9] == ("mistral", "reviewed-test-model", 11, 7)
            elif "UPDATE inference_requests" in query:
                self.request = {
                    "status": "succeeded",
                    "response_body_text": args[1],
                    "final_charge_v": args[2],
                    "final_provider_cost_usd": args[3],
                    "provider_request_id": args[4],
                }
            else:
                raise AssertionError(query)

    db = SettlementDB()
    gateway = AIGateway(db, None, None)
    gateway._settle_preauth = AsyncMock()
    ctx = SimpleNamespace(
        model_config={
            "cost_input_per_1m": "1",
            "cost_output_per_1m": "2",
            "v_price_input_per_1m": "10",
            "v_price_output_per_1m": "20",
        },
        account_id="agent:test",
        user_id=None,
        request_id="logical-request",
        preauth_id="attempt-1",
        reservation_ref="infer-preauth:attempt-1",
        reserve_v=Decimal(1),
    )
    req = request()
    req.account_id = "agent:test"
    first = await gateway._finalize_inference_execution(ctx, req, InferenceResult("answer", 11, 7))
    second = await gateway._finalize_inference_execution(
        ctx, req, InferenceResult("not-used", 1000, 2000)
    )
    assert first.v_cost == second.v_cost == Decimal("0.000250")
    assert first.actual_cost_usd == second.actual_cost_usd == Decimal("0.000025")
    assert second.text == "answer"
    assert db.usage_inserts == 1
    gateway._settle_preauth.assert_awaited_once()
