from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import test_provider_continuity_transactions as contracts

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway import gemini
from openvegas.gateway.conversation import ContinuityError
from openvegas.gateway.inference import AIGateway, InferenceRequest


def request(**changes):
    values = {
        "account_id": "user:synthetic",
        "provider": "gemini",
        "model": "reviewed-test",
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "next"},
        ],
        "max_tokens": 64,
        "strict_continuity": True,
    }
    return InferenceRequest(**(values | changes))


def response():
    return {
        "responseId": "synthetic-response",
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "answer"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 11,
            "candidatesTokenCount": 7,
            "totalTokenCount": 18,
        },
    }


def test_roles_and_system_are_not_flattened_or_mutated():
    req = request()
    original = copy.deepcopy(req.messages)
    payload = gemini.build_payload(req)
    assert payload == {
        "contents": [
            {"role": "user", "parts": [{"text": "first"}]},
            {"role": "model", "parts": [{"text": "answer"}]},
            {"role": "user", "parts": [{"text": "next"}]},
        ],
        "generationConfig": {"maxOutputTokens": 64, "candidateCount": 1},
    }
    assert req.messages == original
    req.strict_continuity = False
    req.messages.insert(0, {"role": "system", "content": "server instruction"})
    with_system = gemini.build_payload(req)
    assert with_system["systemInstruction"] == {
        "parts": [{"text": "server instruction"}]
    }
    assert with_system["contents"] == payload["contents"]


@pytest.mark.parametrize(
    "messages",
    [
        [],
        None,
        "prompt",
        [{"role": "assistant", "content": "first"}],
        [{"role": "system", "content": "untrusted"}],
        [{"role": "tool", "content": "result"}],
        [{"role": "user", "content": [{"text": "wrapped"}]}],
        [{"role": "user", "content": "x", "attachments": []}],
        [{"role": "user", "content": "x", "thoughtSignature": "synthetic"}],
        [{"role": "user", "content": "x", "thought_signature": "synthetic"}],
        [{"role": "user", "content": "x", "tool_calls": []}],
        [{"role": "user", "content": "x"}, {"role": "user", "content": "y"}],
        [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
        [{"role": "user", "content": "\ud800"}],
        [{"role": "user", "content": "x" * 64001}],
        [{"role": "user", "content": "x"}] * 201,
    ],
)
@pytest.mark.asyncio
async def test_unsupported_input_never_calls_provider(messages):
    def forbidden(_req):
        pytest.fail("Unexpected provider network call")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ContractError):
            await gemini.complete(request(messages=messages), "synthetic", client)


@pytest.mark.parametrize(
    "changes",
    [
        {"enable_tools": True},
        {"enable_web_search": True},
        {"max_tokens": 0},
        {"max_tokens": True},
        {"max_tokens": "1"},
        {"model": "../other"},
        {"model": "x?key=synthetic"},
        {"model": "https://evil.invalid"},
    ],
)
def test_invalid_flags_budget_and_model_fail_before_io(changes):
    with pytest.raises(ContractError):
        gemini.build_payload(request(**changes))


@pytest.mark.asyncio
async def test_concurrent_per_request_credentials_are_not_global_or_in_url():
    entered = asyncio.Event()
    calls = []

    async def handler(req):
        calls.append(req)
        if len(calls) == 2:
            entered.set()
        await entered.wait()
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        before = dict(client.headers)
        gateway = AIGateway(None, None, None, client)
        left, right = await asyncio.gather(
            gateway._call_gemini(request(), "synthetic-left"),
            gateway._call_gemini(
                request(model="models/reviewed-test"), "synthetic-right"
            ),
        )
        assert left.text == right.text == "answer"
        assert left.completion_status == "complete"
        assert dict(client.headers) == before and not client.is_closed
    assert {r.headers["x-goog-api-key"] for r in calls} == {
        "synthetic-left",
        "synthetic-right",
    }
    for req in calls:
        assert not req.url.query
        assert req.url.host == "generativelanguage.googleapis.com"
        assert req.extensions["timeout"] == dict.fromkeys(
            ["connect", "read", "write", "pool"], 60.0
        )
        assert "synthetic-" not in req.content.decode()
        assert [m["role"] for m in json.loads(req.content)["contents"]] == [
            "user",
            "model",
            "user",
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 307, 400, 401, 403, 404, 422, 429, 500, 503])
async def test_http_errors_never_retry_follow_redirect_or_echo(status):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            status,
            text="private-provider-body",
            headers={"Location": "https://evil.invalid"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(ContractError) as error:
            await gemini.complete(request(), "synthetic-key", client)
    assert len(calls) == 1
    assert "private-provider-body" not in str(error.value)
    assert "synthetic-key" not in str(error.value)
    expected = (
        APIErrorCode.INVALID_TRANSITION
        if status in {400, 404, 422}
        else APIErrorCode.PROVIDER_UNAVAILABLE
    )
    assert error.value.code == expected


@pytest.mark.parametrize(
    "part",
    [
        {"text": "answer", "thoughtSignature": "private-state"},
        {"text": "answer", "thought": False},
        {"text": "answer", "thought": True},
        {"functionCall": {"name": "Bash"}},
        {"inlineData": {"data": "synthetic"}},
        {"fileData": {"fileUri": "https://invalid"}},
        {"functionResponse": {}},
        {"executableCode": {}},
        {"text": None},
    ],
)
def test_response_parts_reject_signatures_tools_attachments_even_next_to_text(part):
    body = response()
    body["candidates"][0]["content"]["parts"].append(part)
    with pytest.raises(ContractError, match="signatures"):
        gemini._parse_response(body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "missing_usage",
        "negative",
        "boolean",
        "float",
        "missing_candidates",
        "multiple",
        "safety",
        "unknown_finish",
        "empty",
        "total",
        "tool_usage",
        "bad_json",
        "oversized",
        "output_budget",
    ],
)
async def test_bad_response_is_never_success_or_automatic_retry(mutation):
    body = response()
    if mutation == "missing_usage":
        del body["usageMetadata"]
    elif mutation in {"negative", "boolean", "float"}:
        body["usageMetadata"]["promptTokenCount"] = {
            "negative": -1,
            "boolean": True,
            "float": 1.5,
        }[mutation]
    elif mutation == "missing_candidates":
        del body["candidates"]
    elif mutation == "multiple":
        body["candidates"] *= 2
    elif mutation in {"safety", "unknown_finish"}:
        body["candidates"][0]["finishReason"] = (
            "SAFETY" if mutation == "safety" else None
        )
    elif mutation == "empty":
        body["candidates"][0]["content"]["parts"] = []
    elif mutation == "total":
        body["usageMetadata"]["totalTokenCount"] = 100
    elif mutation == "output_budget":
        body["usageMetadata"].update(candidatesTokenCount=65, totalTokenCount=76)
    elif mutation == "tool_usage":
        body["usageMetadata"]["toolUsePromptTokenCount"] = 1
    calls = []

    def handler(req):
        calls.append(req)
        if mutation in {"bad_json", "oversized"}:
            return httpx.Response(
                200, content=b"{" if mutation == "bad_json" else b"x" * 1_000_001
            )
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ContractError):
            await gemini.complete(request(), "synthetic", client)
    assert len(calls) == 1


def test_truncation_and_thinking_usage_are_not_hidden():
    body = response()
    body["candidates"][0]["finishReason"] = "MAX_TOKENS"
    body["usageMetadata"].update(thoughtsTokenCount=4, totalTokenCount=22)
    result = gemini._parse_response(body)
    assert result["completion_status"] == "incomplete"
    assert result["input_tokens"] == 11 and result["output_tokens"] == 11


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "http_timeout", "deadline"])
async def test_cancellation_and_timeout_release_gateway_without_settlement(
    failure, monkeypatch
):
    calls = []

    async def handler(req):
        calls.append(req)
        if failure == "deadline":
            await asyncio.Event().wait()
        if failure == "cancel":
            raise asyncio.CancelledError
        raise httpx.ReadTimeout("private-body")

    monkeypatch.setattr(gemini, "REQUEST_TIMEOUT_SECONDS", 0.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = AIGateway(None, None, None, client)
        ctx = SimpleNamespace(provider_api_key="synthetic")
        gateway._prepare_inference_execution = AsyncMock(return_value=(ctx, None))
        gateway._cleanup_inference_after_failure = AsyncMock()
        gateway._finalize_inference_execution = AsyncMock()
        with pytest.raises(
            asyncio.CancelledError if failure == "cancel" else ContractError
        ):
            await gateway.infer(request())
        gateway._cleanup_inference_after_failure.assert_awaited_once_with(ctx)
        gateway._finalize_inference_execution.assert_not_awaited()
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["context", "unreviewed", "output", "input"])
async def test_gateway_prevalidates_before_credentials_or_wallet(failure, monkeypatch):
    row = {
        "enabled": True,
        "max_tokens": 64,
        "cost_input_per_1m": "1",
        "cost_output_per_1m": "1",
        "v_price_input_per_1m": "1",
        "v_price_output_per_1m": "1",
    }
    catalog = SimpleNamespace(get_model=AsyncMock(return_value=row))
    gateway = AIGateway(None, None, catalog)
    gateway._resolve_provider_api_key = AsyncMock()
    import openvegas.gateway.inference as module

    limit = None if failure == "unreviewed" else 1 if failure == "context" else 100000
    monkeypatch.setattr(
        module, "model_capabilities", lambda *args: {"context_window_tokens": limit}
    )
    req = request(max_tokens=65 if failure == "output" else 64)
    if failure == "input":
        req.messages[0]["attachments"] = []
    with pytest.raises(ContractError):
        await gateway._prepare_inference_execution(req)
    gateway._resolve_provider_api_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_history_and_output_reserved_before_provider(monkeypatch):
    tx = SimpleNamespace(fetchrow=AsyncMock(return_value=None), execute=AsyncMock())

    @asynccontextmanager
    async def transaction():
        yield tx

    row = {
        "enabled": True,
        "max_tokens": 64,
        "cost_input_per_1m": "1",
        "cost_output_per_1m": "2",
        "v_price_input_per_1m": "10",
        "v_price_output_per_1m": "20",
    }
    wallet = SimpleNamespace(
        get_balance=AsyncMock(return_value=Decimal(1)), reserve=AsyncMock()
    )
    gateway = AIGateway(
        SimpleNamespace(transaction=transaction),
        wallet,
        SimpleNamespace(get_model=AsyncMock(return_value=row)),
    )
    gateway._resolve_provider_api_key = AsyncMock(return_value="synthetic")
    gateway._estimate_grant_cover_v = AsyncMock(return_value=Decimal(0))
    gateway._begin_inference_request = AsyncMock(
        return_value=("synthetic-request", None)
    )
    import openvegas.gateway.inference as module

    monkeypatch.setattr(
        module, "model_capabilities", lambda *args: {"context_window_tokens": 100000}
    )
    req = request()
    ctx, replay = await gateway._prepare_inference_execution(req)
    input_bound = sum(len(m["content"].encode()) + 32 for m in req.messages) + 256
    expected = (Decimal(input_bound * 10 + 64 * 20) / 1_000_000).quantize(
        Decimal("0.000001")
    )
    assert replay is None and ctx.reserve_v == expected
    assert wallet.reserve.await_args.kwargs["amount"] == expected
    assert (
        gateway._estimate_grant_cover_v.await_args.kwargs["estimated_total_tokens"]
        == input_bound + 64
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["complete", "truncated", "signature", "cancelled"])
async def test_real_adapter_canonical_storage_and_uncertain_turn_guard(
    monkeypatch, outcome
):
    db, service, catalog = contracts.setup.__wrapped__(monkeypatch)
    created = await service.create_canonical_thread(
        user_id=contracts.USER,
        provider="gemini",
        model_id="reviewed-test",
        catalog=catalog,
    )
    calls = []

    async def handler(req):
        calls.append(req)
        if outcome == "cancelled":
            raise asyncio.CancelledError
        body = response()
        if outcome == "truncated":
            body["candidates"][0]["finishReason"] = "MAX_TOKENS"
        if outcome == "signature":
            body["candidates"][0]["content"]["parts"][0]["thoughtSignature"] = (
                "synthetic-state"
            )
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = AIGateway(None, None, None, client)
        gateway._prepare_inference_execution = AsyncMock(
            return_value=(
                SimpleNamespace(provider_api_key="synthetic-managed"),
                None,
            )
        )
        gateway._finalize_inference_execution = AsyncMock(
            side_effect=lambda ctx, req, result: result
        )
        gateway._cleanup_inference_after_failure = AsyncMock()
        args = {
            "user_id": contracts.USER,
            "thread_id": created.thread_id,
            "provider": "gemini",
            "model_id": "reviewed-test",
            "expected_revision": created.revision,
            "prompt": "hello",
            "idempotency_key": contracts.KEY,
            "catalog": catalog,
            "gateway": gateway,
            "max_output_tokens": 64,
        }
        if outcome in {"signature", "cancelled"}:
            with pytest.raises(
                asyncio.CancelledError if outcome == "cancelled" else ContinuityError
            ):
                await service.infer_canonical(**args)
            gateway._finalize_inference_execution.assert_not_awaited()
            gateway._cleanup_inference_after_failure.assert_awaited_once()
        else:
            result = await service.infer_canonical(**args)
            assert result["continuity_blocked"] == (outcome == "truncated")
            assert bool(result["revision"]) == (outcome == "complete")
            gateway._finalize_inference_execution.assert_awaited_once()
        stored = db.messages[created.thread_id][0]["content"]
        assert ("pending" in stored) == (outcome != "complete")
        assert stored["messages"] == (
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "answer"},
            ]
            if outcome == "complete"
            else []
        )
        with pytest.raises(ContinuityError):
            await service.infer_canonical(**args)
    assert len(calls) == 1
