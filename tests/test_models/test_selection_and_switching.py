from __future__ import annotations  # noqa: I001 - Partial staging trees change Ruff's first-party import discovery.

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from openvegas.contracts.errors import ContractError
from openvegas.gateway.catalog import ProviderCatalog
from openvegas.gateway.inference import AIGateway, InferenceRequest
from openvegas.gateway.providers import PROVIDERS, resolve_provider_api_key
from openvegas.gateway.switching import plan_context_transfer
from server.services.provider_threads import ProviderThreadService

USER = "11111111-1111-4111-8111-111111111111"
THREAD = "22222222-2222-4222-8222-222222222222"


def row(provider="mistral", model="reviewed-test-model"):
    return {
        "provider": provider,
        "model_id": model,
        "enabled": True,
        "max_tokens": 2000,
        "cost_input_per_1m": "1",
        "cost_output_per_1m": "2",
        "v_price_input_per_1m": "10",
        "v_price_output_per_1m": "20",
    }


def review():
    now = datetime.now(UTC)
    return {
        "reviewed_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "account_access": True,
        "completion_chat": True,
        "context_window_tokens": 8192,
        "capabilities": {"tools": True, "image_input": True, "web_search": True},
    }


class DB:
    def __init__(self, model=None):
        self.model = row() if model is None else model
        self.owner = USER
        self.rows = [
            {"role": "user", "content": {"text": "hello"}},
            {"role": "assistant", "content": {"text": "answer"}},
        ]
        self.writes = 0
        self.alias = "FAKE_OPERATOR_KEY"
        self.expires = datetime.now(UTC) + timedelta(hours=1)

    async def fetchrow(self, query, *args):
        if "provider_credentials" in query:
            return {"key_alias": self.alias} if self.alias else None
        if "provider_catalog" in query:
            return (
                self.model
                if (self.model.get("provider"), self.model.get("model_id")) == args
                else None
            )
        if "provider_threads" in query:
            assert args == (THREAD, USER)
            return (
                {"id": THREAD, "provider": "openai", "expires_at": self.expires}
                if self.owner == USER
                else None
            )
        raise AssertionError(query)

    async def fetch(self, query, *args):
        if "provider_thread_messages" in query:
            assert args == (THREAD, 201)
            return self.rows[:201]
        if "provider_catalog" in query:
            return [self.model]
        raise AssertionError(query)

    async def execute(self, *args):
        self.writes += 1
        raise AssertionError("Preflight must never mutate state")

    @asynccontextmanager
    async def transaction(self):
        yield self


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", "production")
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "1")
    monkeypatch.setenv("FAKE_OPERATOR_KEY", "fake-test-key")
    monkeypatch.setenv(
        "OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({"mistral:reviewed-test-model": review()})
    )
    return DB()


@pytest.mark.asyncio
async def test_selection_uses_catalog_prices_and_exposes_truthful_capabilities(configured):
    result = await ProviderCatalog(configured).validate_selection("mistral", "reviewed-test-model")
    assert result["available"] is True
    assert result["availability"] == "configured_not_live_verified"
    assert result["cost_input_per_1m"] == "1"
    assert result["capabilities"]["streaming_mode"] == "buffered"
    assert result["capabilities"]["tools"] is False
    assert result["capabilities"]["image_input"] is False
    assert "fake-test-key" not in json.dumps(result)
    assert "FAKE_OPERATOR_KEY" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "disabled",
        "price",
        "nan",
        "negative",
        "review",
        "expired",
        "wildcard",
        "access",
        "unknown_context",
        "credential",
    ],
)
async def test_unavailable_models_fail_before_mutation(configured, monkeypatch, failure):
    if failure == "missing":
        configured.model = {}
    if failure == "disabled":
        configured.model["enabled"] = False
    if failure == "price":
        configured.model.pop("cost_input_per_1m")
    if failure == "nan":
        configured.model["cost_input_per_1m"] = "NaN"
    if failure == "negative":
        configured.model["cost_input_per_1m"] = "-1"
    if failure == "credential":
        monkeypatch.delenv("FAKE_OPERATOR_KEY")
    if failure in {"review", "expired", "wildcard", "access", "unknown_context"}:
        r = review()
        if failure == "expired":
            r["expires_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        if failure == "access":
            r["account_access"] = False
        if failure == "unknown_context":
            r.pop("context_window_tokens")
        payload = {"mistral:*" if failure == "wildcard" else "mistral:reviewed-test-model": r}
        monkeypatch.setenv(
            "OPENVEGAS_MODEL_REVIEWS_JSON", "{}" if failure == "review" else json.dumps(payload)
        )
    with pytest.raises(ContractError):
        await ProviderCatalog(configured).validate_selection("mistral", "reviewed-test-model")
    assert configured.writes == 0


@pytest.mark.asyncio
async def test_selection_rejects_missing_features_unknown_provider_and_budget(configured):
    catalog = ProviderCatalog(configured)
    for kwargs in [
        {"required_capabilities": ["tools"]},
        {"required_capabilities": ["invented"]},
        {"max_tokens": 3000},
        {"max_tokens": 0},
    ]:
        with pytest.raises(ContractError):
            await catalog.validate_selection("mistral", "reviewed-test-model", **kwargs)
    with pytest.raises(ContractError):
        await catalog.validate_selection("consumer_session", "model")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(PROVIDERS))
async def test_server_credentials_preserve_registry_precedence_and_local_only_fallback(
    provider, monkeypatch
):
    db = DB()
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", "production")
    monkeypatch.setenv(PROVIDERS[provider].credential_env, "fake-fallback")
    monkeypatch.setenv("FAKE_OPERATOR_KEY", "fake-registry")
    assert await resolve_provider_api_key(db, provider) == "fake-registry"
    monkeypatch.delenv("FAKE_OPERATOR_KEY")
    with pytest.raises(ContractError):
        await resolve_provider_api_key(db, provider)
    db.alias = None
    with pytest.raises(ContractError):
        await resolve_provider_api_key(db, provider)
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", "test")
    assert await resolve_provider_api_key(db, provider) == "fake-fallback"


@pytest.mark.asyncio
async def test_mistral_preflight_checks_bounds_before_billing_or_network(configured):
    gateway = AIGateway(configured, None, ProviderCatalog(configured))
    gateway._resolve_provider_api_key = AsyncMock(
        side_effect=AssertionError("No credential/billing access expected")
    )
    req = InferenceRequest(
        "user:u1", "mistral", "reviewed-test-model", [{"role": "user", "content": "a" * 9000}]
    )
    with pytest.raises(ContractError, match="budget"):
        await gateway._prepare_inference_execution(req)
    gateway._resolve_provider_api_key.assert_not_awaited()


def target():
    return {
        "available": True,
        "max_tokens": 2000,
        "capabilities": {"role_preserving_history": True, "context_window_tokens": 8192},
    }


@pytest.mark.parametrize(
    "scenario",
    [
        "active",
        "pending",
        "tool",
        "image",
        "system",
        "unfinished",
        "overlong",
        "context",
        "gemini",
        "trace",
    ],
)
def test_unsafe_context_transfers_block_without_dropping_or_replaying(scenario):
    history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "answer"}]
    model = target()
    kwargs = {}
    if scenario == "active":
        kwargs["active_generation"] = True
    if scenario == "pending":
        kwargs["pending_tool_calls"] = True
    if scenario == "tool":
        history[0]["role"] = "tool"
    if scenario == "image":
        history[0]["content"] = [{"type": "image"}]
    if scenario == "system":
        history[0]["role"] = "system"
    if scenario == "unfinished":
        history.pop()
    if scenario == "overlong":
        history[0]["content"] = "x" * 10_000
    if scenario == "context":
        model["capabilities"]["context_window_tokens"] = None
    if scenario == "gemini":
        model["capabilities"]["role_preserving_history"] = False
    if scenario == "trace":
        history[1]["content"] = '{"tool_name":"Bash"}'
    plan = plan_context_transfer(history, model, **kwargs)
    assert plan.status == "blocked"
    assert plan.messages == []
    assert plan.tool_replay_allowed is False


@pytest.mark.asyncio
async def test_server_switch_plan_scoped_complete_history_no_mutation(configured):
    service = ProviderThreadService(configured)
    kwargs = {
        "user_id": USER,
        "thread_id": THREAD,
        "provider": "mistral",
        "model_id": "reviewed-test-model",
        "catalog": ProviderCatalog(configured),
    }
    plan = await service.plan_model_switch(**kwargs)
    assert plan.status == "ready"
    assert plan.messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "answer"},
    ]
    assert plan.requires_new_thread and not plan.context_transferred
    assert plan.requires_confirmation and plan.history_scope == "retained_server_text_only"
    assert configured.writes == 0
    configured.owner = "another-user"
    with pytest.raises(ContractError, match="user scope"):
        await service.plan_model_switch(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["expired", "compacted", "tool", "overflow"])
async def test_server_switch_rejects_expiry_and_filtered_history(configured, scenario):
    if scenario == "expired":
        configured.expires = datetime.now(UTC) - timedelta(days=1)
    if scenario == "compacted":
        configured.rows[-1]["content"]["text"] = "conversation_summary_v1\nEarlier context"
    if scenario == "tool":
        configured.rows[-1]["content"] = {"text": "answer", "tool_calls": []}
    if scenario == "overflow":
        configured.rows *= 101
    plan = await ProviderThreadService(configured).plan_model_switch(
        user_id=USER,
        thread_id=THREAD,
        provider="mistral",
        model_id="reviewed-test-model",
        catalog=ProviderCatalog(configured),
    )
    assert plan.status == "blocked" and not plan.messages
    assert configured.writes == 0


@pytest.mark.asyncio
async def test_model_routes_require_auth_reject_byok_and_validate(configured, monkeypatch):
    from server.routes import models

    app = FastAPI()
    app.include_router(models.router)
    monkeypatch.setattr(models, "get_catalog", lambda: ProviderCatalog(configured))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/models")).status_code in {401, 403, 422}
        app.dependency_overrides[models.get_current_user] = lambda: {"user_id": USER}
        data = (await client.get("/models")).json()
        assert [p["id"] for p in data["providers"]] == list(PROVIDERS)
        payload = {"provider": "mistral", "model": "reviewed-test-model"}
        response = await client.post("/models/validate", json=payload)
        assert response.status_code == 200
        assert response.json()["state_changed"] is False
        assert (
            await client.post("/models/validate", json={**payload, "api_key": "fake-client-key"})
        ).status_code == 422
        assert (
            await client.post("/models/validate", json={**payload, "model": "future-unreviewed"})
        ).status_code == 409
        assert (await client.get("/models?provider=consumer-session")).status_code == 422
        monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "0")
        assert (await client.post("/models/validate", json=payload)).status_code == 409
