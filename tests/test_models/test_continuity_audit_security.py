"""Audit regressions for policy gates, retries and incomplete provider output."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import test_continuity_route_integration as routes_fixture
from fastapi import FastAPI

from openvegas.contracts.errors import ContractError
from openvegas.fraud.engine import AbuseThresholds, FraudEngine
from openvegas.gateway.inference import AIGateway, InferenceRequest
from server.routes import inference, models
from server.services import dependencies


@pytest.fixture
def app_setup(monkeypatch):
    setup = routes_fixture.setup.__wrapped__(monkeypatch)
    return routes_fixture.app_setup.__wrapped__(setup, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/models/conversations", "/models/switch"])
@pytest.mark.parametrize("failure", ["limiter", "byok", "ephemeral", "unknown_mode"])
async def test_writes_cannot_bypass_rate_or_account_policy(app_setup, path, failure):
    app, db, gateway, fraud, mode = app_setup
    if failure == "limiter":
        fraud.check_inference.side_effect = RuntimeError("synthetic limiter failure")
    else:
        mode.resolve_for_user.return_value = {
            "effective_mode": "byok"
            if failure == "byok"
            else ("unknown" if failure == "unknown_mode" else "wrapper"),
            "conversation_mode": "ephemeral" if failure == "ephemeral" else "persistent",
        }
    payload = {"provider": "openai", "model": "reviewed-test"}
    if path.endswith("/switch"):
        payload["thread_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(path, json=payload)
    assert response.status_code in {400, 429}
    assert response.headers["cache-control"] == "private, no-store"
    assert not db.writes
    gateway.infer.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_and_canonical_routes_share_actual_fraud_velocity_key(app_setup, monkeypatch):
    _app, db, gateway, _fraud, mode = app_setup
    keys = []

    async def incr(key):
        keys.append(key)
        return len(keys)

    redis = SimpleNamespace(incr=incr, expire=AsyncMock())
    fraud = FraudEngine(redis, None, AbuseThresholds(max_infer_requests_per_minute=1))
    monkeypatch.setattr(dependencies, "get_fraud_engine", lambda: fraud)
    monkeypatch.setattr(inference, "get_fraud_engine", lambda: fraud)
    monkeypatch.setattr(inference, "get_llm_mode_service", lambda: mode)
    monkeypatch.setattr(
        inference,
        "get_provider_thread_service",
        lambda: SimpleNamespace(context_enabled=lambda: True),
    )
    app = FastAPI()
    app.include_router(models.router)
    app.include_router(inference.router, prefix="/inference")
    app.dependency_overrides[models.get_current_user] = lambda: {
        "user_id": "11111111-1111-4111-8111-111111111111"
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        main = await client.post(
            "/inference/ask",
            json={"provider": "openai", "model": "reviewed-test", "prompt": "sk-" + "x" * 30},
        )
        assert main.status_code == 400
        canonical = await client.post(
            "/models/conversations", json={"provider": "openai", "model": "reviewed-test"}
        )
        assert canonical.status_code == 429
    assert len(keys) == 2 and keys[0] == keys[1]
    assert not db.writes
    gateway.infer.assert_not_awaited()


def request():
    return InferenceRequest(
        "user:local",
        "openai",
        "legacy-test",
        [{"role": "user", "content": "test"}],
        strict_continuity=True,
    )


@pytest.mark.asyncio
async def test_strict_openai_disables_sdk_retries_and_parameter_fallback(monkeypatch):
    gateway = AIGateway(None, None, None)
    create = AsyncMock(side_effect=RuntimeError("Unsupported parameter 'max_completion_tokens'"))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    options = []
    client.with_options = lambda **kwargs: options.append(kwargs) or client
    monkeypatch.setattr(gateway, "_build_openai_client", lambda key: client)
    with pytest.raises(ContractError):
        await gateway._call_openai(request(), "synthetic-key")
    create.assert_awaited_once()
    assert options == [{"max_retries": 0, "timeout": 60.0}]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stop", "length", "content_filter", None])
async def test_chat_completion_reason_survives_adapter(reason):
    gateway = AIGateway(None, None, None)
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=reason, message=SimpleNamespace(content="partial", tool_calls=[])
            )
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
        id="synthetic",
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response)))
    )
    result = await gateway._call_openai_chat_completions(client=client, req=request())
    assert result.completion_status == ("complete" if reason == "stop" else "incomplete")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["end_turn", "max_tokens", None])
async def test_anthropic_completion_and_zero_retry_policy(monkeypatch, reason):
    options = []
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="first"),
            SimpleNamespace(type="text", text="second"),
        ],
        stop_reason=reason,
        usage=SimpleNamespace(input_tokens=1, output_tokens=2),
        id="synthetic",
    )
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=lambda **kw: options.append(kw) or client),
    )
    result = await AIGateway(None, None, None)._call_anthropic(request(), "synthetic-key")
    assert result.text == "firstsecond"
    assert result.completion_status == ("complete" if reason == "end_turn" else "incomplete")
    assert options[0]["max_retries"] == 0


@pytest.mark.parametrize("status", ["completed", "incomplete", None])
def test_responses_completion_and_billing_replay_metadata(status):
    gateway = AIGateway(None, None, None)
    response = SimpleNamespace(
        status=status,
        output_text="text",
        output=[],
        usage=SimpleNamespace(input_tokens=1, output_tokens=2),
    )
    result = gateway._build_openai_responses_result(resp=response)
    assert result.completion_status == ("complete" if status == "completed" else "incomplete")
    replay = gateway._deserialize_result(
        {"response_body_text": gateway._serialize_success_body(result)}
    )
    assert replay.completion_status == result.completion_status
    legacy = json.loads(gateway._serialize_success_body(result))
    legacy.pop("completion_status")
    assert (
        gateway._deserialize_result({"response_body_text": json.dumps(legacy)}).completion_status
        == "unknown"
    )


def test_strict_requests_have_distinct_idempotency_hash():
    req = request()
    strict = AIGateway._payload_hash(req)
    req.strict_continuity = False
    assert strict != AIGateway._payload_hash(req)
