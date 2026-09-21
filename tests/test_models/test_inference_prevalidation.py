from __future__ import annotations  # noqa: I001 - Partial staging trees change Ruff's first-party import discovery.

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from openvegas.gateway.catalog import ProviderCatalog
from openvegas.gateway.inference import InferenceResult
from server.routes import inference as routes


@pytest.fixture
def setup_route(monkeypatch):
    events = []
    row = {
        "provider": "openai",
        "model_id": "catalog-model",
        "enabled": True,
        "max_tokens": 1024,
        "cost_input_per_1m": "1",
        "cost_output_per_1m": "2",
        "v_price_input_per_1m": "10",
        "v_price_output_per_1m": "20",
    }

    async def fetchrow(query, *args):
        if "provider_catalog" in query:
            events.append("catalog")
            return row if args == (row["provider"], row["model_id"]) else None
        if "provider_credentials" in query:
            events.append("credentials")
            return {"key_alias": "FAKE_PREVALIDATION_KEY"}
        raise AssertionError(query)

    catalog = ProviderCatalog(SimpleNamespace(fetchrow=fetchrow))

    async def prepare_thread(**kwargs):
        events.append("prepare_thread")
        return SimpleNamespace(thread_id=None, thread_status="disabled")

    thread = SimpleNamespace(
        context_enabled=lambda: True,
        prepare_thread=AsyncMock(side_effect=prepare_thread),
        append_exchange=AsyncMock(),
    )
    gateway = SimpleNamespace(infer=AsyncMock(return_value=InferenceResult("answer", 3, 2)))
    monkeypatch.setattr(routes, "get_catalog", lambda: catalog)
    monkeypatch.setattr(routes, "get_provider_thread_service", lambda: thread)
    monkeypatch.setattr(routes, "get_gateway", lambda: gateway)
    monkeypatch.setattr(
        routes, "get_fraud_engine", lambda: SimpleNamespace(check_inference=AsyncMock())
    )
    monkeypatch.setattr(
        routes,
        "get_llm_mode_service",
        lambda: SimpleNamespace(
            resolve_for_user=AsyncMock(
                return_value={
                    "effective_mode": "wrapper",
                    "user_pref_mode": "wrapper",
                    "effective_reason": "user_pref_applied",
                    "conversation_mode": "persistent",
                }
            )
        ),
    )
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", "production")
    monkeypatch.setenv("FAKE_PREVALIDATION_KEY", "fake-test-only")
    # Ordinary inference is not gated by the picker rollback flag.
    monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "0")
    app = FastAPI()
    app.include_router(routes.router, prefix="/inference")
    app.dependency_overrides[routes.get_current_user] = lambda: {"user_id": "test-user"}
    return app, row, events, thread, gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.parametrize(
    "failure",
    [
        "unknown_model",
        "disabled",
        "unpriced",
        "nan",
        "negative",
        "provider",
        "credential",
        "mistral_review",
    ],
)
async def test_rejected_model_never_prepares_thread_or_calls_gateway(
    setup_route, monkeypatch, endpoint, failure
):
    app, row, events, thread, gateway = setup_route
    payload = {"prompt": "hello", "provider": "openai", "model": "catalog-model"}
    if failure == "unknown_model":
        payload["model"] = "not-in-catalog"
    if failure == "disabled":
        row["enabled"] = False
    if failure == "unpriced":
        row.pop("cost_input_per_1m")
    if failure == "nan":
        row["cost_input_per_1m"] = "NaN"
    if failure == "negative":
        row["v_price_output_per_1m"] = "-1"
    if failure == "provider":
        payload["provider"] = row["provider"] = "consumer-session"
    if failure == "credential":
        monkeypatch.delenv("FAKE_PREVALIDATION_KEY")
    if failure == "mistral_review":
        payload["provider"] = row["provider"] = "mistral"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/inference/{endpoint}", json=payload)
    if endpoint == "ask":
        assert response.status_code == 503
        assert response.json()["error"] == "provider_unavailable"
        assert response.json()["effective_mode"] == "wrapper"
    else:
        assert response.status_code == 200  # Existing SSE envelope reports application errors.
        assert "provider_unavailable" in response.text
        assert "event: response.error" in response.text
        assert '"status":"error"' in response.text
    thread.prepare_thread.assert_not_awaited()
    gateway.infer.assert_not_awaited()
    assert "prepare_thread" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "mistral"])
async def test_valid_catalog_precedes_thread_and_legacy_needs_no_review(
    setup_route, monkeypatch, endpoint, provider
):
    app, row, events, thread, gateway = setup_route
    row["provider"] = provider
    if provider == "mistral":
        now = datetime.now(UTC)
        monkeypatch.setenv(
            "OPENVEGAS_MODEL_REVIEWS_JSON",
            json.dumps(
                {
                    "mistral:catalog-model": {
                        "reviewed_at": (now - timedelta(hours=1)).isoformat(),
                        "expires_at": (now + timedelta(hours=1)).isoformat(),
                        "account_access": True,
                        "completion_chat": True,
                        "context_window_tokens": 8192,
                    }
                }
            ),
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/inference/{endpoint}",
            json={"prompt": "hello", "provider": provider, "model": "catalog-model"},
        )
    assert response.status_code == 200
    assert "answer" in response.text
    assert events[:3] == ["catalog", "credentials", "prepare_thread"]
    thread.prepare_thread.assert_awaited_once()
    gateway.infer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
async def test_unauthorized_request_cannot_reach_catalog_or_thread(setup_route, endpoint):
    app, _row, events, thread, _gateway = setup_route
    app.dependency_overrides.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/inference/{endpoint}",
            json={"prompt": "hello", "provider": "openai", "model": "catalog-model"},
        )
    assert response.status_code in {401, 403, 422}
    assert events == []
    thread.prepare_thread.assert_not_awaited()
