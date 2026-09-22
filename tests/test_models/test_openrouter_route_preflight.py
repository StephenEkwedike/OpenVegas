"""ASGI-only route preflight; no upload, provider, database or real auth traffic."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import test_inference_replay as replay_fixtures
import test_openrouter as fixtures
from fastapi import FastAPI

from openvegas.gateway.catalog import ProviderCatalog
from openvegas.gateway.inference import AIGateway, InferenceResult
from server.routes import inference as routes
from server.services.inference_replay import GATEWAY_KEY_PREFIX, InferenceReplayService


@pytest.fixture
def setup_route(monkeypatch):
    events = []
    db = replay_fixtures.MemoryDB()
    state = {"row": fixtures.catalog_row(), "credential": {"key_alias": "OPENROUTER_ROUTE_FIXTURE"}}

    async def fetchrow(query, *args):
        if "provider_catalog" in query:
            events.append("catalog")
            row = state["row"]
            return row if row and args == (row["provider"], row["model_id"]) else None
        if "provider_credentials" in query:
            events.append("credential")
            assert args == ("openrouter", "production")
            return state["credential"]
        pytest.fail("Unexpected database work")

    async def prepare_thread(**kwargs):
        events.append("thread")
        return SimpleNamespace(thread_id=None, thread_status="disabled")

    async def history(**kwargs):
        return copy.deepcopy(db.history), len(db.history), 0

    async def append_exchange(**kwargs):
        tx = kwargs.get("tx")
        if tx is None:
            return  # Keyless fixture cases retain their legacy no-op persistence.
        assert isinstance(tx, replay_fixtures.MemoryTx) and tx.db is db
        if kwargs["thread_ctx"].thread_id is None or not kwargs["persist_context"]:
            return
        user = {"role": "user", "content": kwargs["prompt"]}
        if "attachment_refs" in kwargs:
            user["attachment_refs"] = kwargs["attachment_refs"]
        assistant = {"role": "assistant", "content": kwargs["response_text"]}
        await tx.execute("INSERT TEST ROUTE HISTORY", user, assistant)

    thread = SimpleNamespace(
        context_enabled=lambda: True,
        prepare_thread=AsyncMock(side_effect=prepare_thread),
        append_exchange=AsyncMock(side_effect=append_exchange),
        get_recent_messages_with_stats=AsyncMock(side_effect=history),
        db=db,
    )
    gateway = SimpleNamespace(
        infer=AsyncMock(return_value=InferenceResult("Fixture answer", 11, 7)),
        db=db,
    )

    async def infer(req):
        result = copy.deepcopy(gateway.infer.return_value)
        if isinstance(req.idempotency_key, str) and req.idempotency_key.startswith(
            GATEWAY_KEY_PREFIX
        ):
            scope = SimpleNamespace(
                user_id=req.account_id.removeprefix("user:"),
                gateway_idempotency_key=req.idempotency_key,
            )
            result.inference_request_id = replay_fixtures.settled_gateway(db, scope)
            db.gateway_rows[(scope.user_id, req.idempotency_key)].update(
                payload_hash=AIGateway._payload_hash(req),
                response_body_text=AIGateway._serialize_success_body(result),
            )
        return result

    gateway.infer.side_effect = infer
    fraud = SimpleNamespace(check_inference=AsyncMock())
    mode = SimpleNamespace(
        resolve_for_user=AsyncMock(
            return_value={
                "effective_mode": "wrapper",
                "conversation_mode": "persistent",
            }
        )
    )
    monkeypatch.setattr(
        routes, "get_catalog", lambda: ProviderCatalog(SimpleNamespace(fetchrow=fetchrow))
    )
    monkeypatch.setattr(routes, "get_provider_thread_service", lambda: thread)
    monkeypatch.setattr(routes, "get_gateway", lambda: gateway)
    monkeypatch.setattr(routes, "get_fraud_engine", lambda: fraud)
    monkeypatch.setattr(routes, "get_llm_mode_service", lambda: mode)
    monkeypatch.setattr(routes, "get_inference_replay_service", lambda: InferenceReplayService(db))
    monkeypatch.setattr(routes, "emit_metric", lambda *a, **k: None)
    monkeypatch.setattr(routes, "emit_run_metrics", lambda *a, **k: None)
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", "production")
    monkeypatch.setenv("OPENROUTER_ROUTE_FIXTURE", fixtures.SYNTHETIC_CREDENTIAL)
    monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "0")
    fixtures.install_review(monkeypatch)
    app = FastAPI()
    app.include_router(routes.router, prefix="/inference")
    app.dependency_overrides[routes.get_current_user] = lambda: {
        "user_id": "11111111-1111-4111-8111-111111111111",
    }
    return SimpleNamespace(
        app=app,
        state=state,
        events=events,
        thread=thread,
        gateway=gateway,
        fraud=fraud,
        mode=mode,
        db=db,
    )


async def post(setup, endpoint, **changes):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url="http://test"
    ) as client:
        return await client.post(
            f"/inference/{endpoint}",
            json={
                "provider": "openrouter",
                "model": fixtures.MODEL,
                "prompt": "Fixture prompt",
                **changes,
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.parametrize("attachments", [["fixture-file"], ["fixture-image", "fixture-audio"]])
async def test_attachments_rejected_before_thread_catalog_upload_or_provider(
    setup_route, monkeypatch, endpoint, attachments
):
    def forbidden():
        pytest.fail("Unsupported attachments reached side effects")

    for name in (
        "get_provider_thread_service",
        "get_catalog",
        "get_gateway",
        "get_file_upload_service",
    ):
        monkeypatch.setattr(routes, name, forbidden)
    response = await post(setup_route, endpoint, attachments=attachments)
    if endpoint == "ask":
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_transition"
    else:
        assert response.status_code == 200 and "event: response.error" in response.text
    assert "no file was sent" in response.text
    setup_route.fraud.check_inference.assert_awaited_once()
    setup_route.thread.prepare_thread.assert_not_awaited()
    setup_route.gateway.infer.assert_not_awaited()
    assert setup_route.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.parametrize(
    "failure",
    [
        "unknown",
        "disabled",
        "unpriced",
        "nan",
        "negative",
        "missing_review",
        "price_changed",
        "review_expired",
        "no_access",
        "no_context",
        "no_credential",
    ],
)
async def test_invalid_managed_selection_never_creates_thread_or_paid_request(
    setup_route, monkeypatch, endpoint, failure
):
    setup = setup_route
    row = setup.state["row"]
    if failure == "unknown":
        setup.state["row"] = None
    elif failure == "disabled":
        row["enabled"] = False
    elif failure == "unpriced":
        row.pop("cost_output_per_1m")
    elif failure == "nan":
        row["cost_input_per_1m"] = "NaN"
    elif failure == "negative":
        row["v_price_input_per_1m"] = "-1"
    elif failure == "price_changed":
        row["cost_output_per_1m"] = "3"
    elif failure == "missing_review":
        monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    elif failure == "review_expired":
        fixtures.install_review(
            monkeypatch, fixtures.review(expires_at="2000-01-01T00:00:00+00:00")
        )
    elif failure == "no_access":
        fixtures.install_review(monkeypatch, fixtures.review(account_access=False))
    elif failure == "no_context":
        fixtures.install_review(monkeypatch, fixtures.review(context_window_tokens=None))
    else:
        setup.state["credential"] = None
        monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-production-fallback")
    response = await post(setup, endpoint)
    if endpoint == "ask":
        assert response.status_code == 503 and response.json()["error"] == "provider_unavailable"
    else:
        assert response.status_code == 200 and "event: response.error" in response.text
        assert "provider_unavailable" in response.text
    setup.thread.prepare_thread.assert_not_awaited()
    setup.gateway.infer.assert_not_awaited()
    assert "thread" not in setup.events
    assert fixtures.SYNTHETIC_CREDENTIAL not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.parametrize("tools", [False, True])
async def test_exact_managed_model_reaches_gateway_after_catalog_and_credential_only(
    setup_route, endpoint, tools
):
    setup = setup_route
    response = await post(setup, endpoint, enable_tools=tools)
    assert response.status_code == 200 and "Fixture answer" in response.text
    assert setup.events[:3] == ["catalog", "credential", "thread"]
    setup.gateway.infer.assert_awaited_once()
    request = setup.gateway.infer.call_args.args[0]
    assert request.provider == "openrouter" and request.model == fixtures.MODEL
    assert request.messages == [{"role": "user", "content": "Fixture prompt"}]
    assert request.enable_tools is tools and not request.enable_web_search
    assert fixtures.SYNTHETIC_CREDENTIAL not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.parametrize("guard", ["rate", "byok"])
async def test_account_and_rate_guards_still_precede_openrouter_preflight(
    setup_route, endpoint, guard
):
    setup = setup_route
    if guard == "rate":
        setup.fraud.check_inference.side_effect = RuntimeError("synthetic rate limit")
    else:
        setup.mode.resolve_for_user.return_value = {"effective_mode": "byok"}
    response = await post(setup, endpoint, attachments=["fixture-file"])
    if endpoint == "ask":
        assert response.status_code == (429 if guard == "rate" else 400)
    else:
        assert "event: response.error" in response.text
    assert setup.events == []
    setup.gateway.infer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
async def test_unauthenticated_request_cannot_reach_provider_catalog(setup_route, endpoint):
    setup_route.app.dependency_overrides.clear()
    response = await post(setup_route, endpoint)
    assert response.status_code in {401, 403, 422}
    assert setup_route.events == []
    setup_route.gateway.infer.assert_not_awaited()
