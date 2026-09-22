"""Offline reasoning contracts: reviewed selection, propagation and recovery."""

import hashlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import test_openrouter as upstream
import test_openrouter_catalog as discovery
import test_openrouter_route_preflight as route_fixture
import test_provider_continuity_transactions as continuity
import test_reconciliation as recovery

from openvegas.capabilities import REASONING_EFFORTS, get_caps, resolve_capability
from openvegas.client import OpenVegasClient
from openvegas.contracts.errors import ContractError
from openvegas.gateway.inference import AIGateway, InferenceResult
from openvegas.gateway.providers import model_capabilities, validate_reasoning_effort
from openvegas.gateway.reasoning import reasoning_payload
from openvegas.gateway.reconciliation import ReconciliationError, request_payload_hash
from openvegas.gateway.conversation import CanonicalConversation
from server.routes.models import CanonicalAskRequest, ModelSelectionRequest, validate_model_selection


def install(monkeypatch, efforts=None, **changes):
    review = upstream.review(
        capabilities={"tools": True, "reasoning_efforts": efforts or ["low", "high", "xhigh"]},
        supported_parameters=["reasoning"], **changes,
    )
    upstream.install_review(monkeypatch, review)
    return review


@pytest.mark.parametrize("effort", REASONING_EFFORTS)
def test_payload_helper_only_selected_reviewed_effort_no_thought_fields(effort):
    caps = {"reasoning_efforts": [effort]}
    assert reasoning_payload(effort, caps) == {"reasoning": {"effort": effort, "exclude": True}}
    assert caps == {"reasoning_efforts": [effort]}
    assert reasoning_payload(None, caps) == {}


@pytest.mark.parametrize("value", [[], ["high", "high"], ["ultra"], [True], [None], "high", None, {}])
def test_malformed_lists_fail_closed(monkeypatch, value):
    review = install(monkeypatch)
    review["capabilities"]["reasoning_efforts"] = value
    upstream.install_review(monkeypatch, review)
    assert model_capabilities("openrouter", upstream.MODEL)["reasoning_efforts"] == []
    with pytest.raises(ContractError):
        reasoning_payload("high", {"reasoning_efforts": value})


def test_exact_fresh_review_required_and_wildcard_overrides_cannot_enable(monkeypatch):
    install(monkeypatch)
    caps = model_capabilities("openrouter", upstream.MODEL)
    assert caps["reasoning_controls"] and caps["reasoning_efforts"] == ["low", "high", "xhigh"]
    assert get_caps("openrouter", upstream.MODEL).reasoning_efforts == ("low", "high", "xhigh")
    assert model_capabilities("openrouter", "fixture/future-model")["reasoning_efforts"] == []
    for provider in ("openai", "anthropic", "gemini", "mistral"):
        with pytest.raises(ContractError):
            validate_reasoning_effort(provider, upstream.MODEL, "high")
    monkeypatch.setenv("OPENVEGAS_CAPABILITY_OVERRIDES_JSON", json.dumps({
        "openrouter:*": {"reasoning_efforts": ["high"], "reasoning_controls": True},
    }))
    monkeypatch.setenv("OPENVEGAS_ENABLE_REASONING_CONTROLS", "1")
    install(monkeypatch, expires_at="2000-01-01T00:00:00+00:00")
    assert not resolve_capability("openrouter", upstream.MODEL, "reasoning_controls")
    with pytest.raises(ContractError):
        validate_reasoning_effort("openrouter", upstream.MODEL, "high")


@pytest.mark.parametrize("metadata,efforts,valid", [
    ({"supported_efforts": ["low", "high"]}, ["high"], True),
    ({"supported_efforts": ["low", "high"]}, ["xhigh"], False),
    ({"supported_efforts": None}, ["minimal", "max"], True),
    ({}, ["high"], False),
    (None, ["high"], False),
    ({"supported_efforts": None, "mandatory": True}, ["none"], False),
    ({"supported_efforts": "high"}, ["high"], False),
    ({"supported_efforts": ["high"]}, ["high", "high"], False),
])
def test_discovery_bounds_review_by_parameter_and_per_model_metadata(metadata, efforts, valid):
    model = discovery.model(supported_parameters=["max_tokens", "tools", "tool_choice", "reasoning"])
    if metadata is not None:
        model["reasoning"] = metadata
    payload = discovery.payload(model)
    plan = discovery.plan(payload)
    plan["models"][0]["capabilities"]["reasoning_efforts"] = efforts
    if not valid:
        with pytest.raises(discovery.c.ReviewError):
            discovery.build(payload, plan)
        return
    review = discovery.build(payload, plan)["model_reviews"]["openrouter:vendor/model-v2"]
    assert review["capabilities"]["reasoning_efforts"] == efforts
    assert "reasoning" in review["supported_parameters"]
    model["supported_parameters"].remove("reasoning")
    payload = discovery.payload(model)
    plan["source_sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(discovery.c.ReviewError):
        discovery.build(payload, plan)


@pytest.mark.parametrize("effort", [None, *REASONING_EFFORTS])
def test_gateway_and_recovery_hash_agree_and_none_preserves_legacy(effort):
    history = CanonicalConversation()
    req = upstream.request(messages=[{"role": "user", "content": "hello"}], strict_continuity=True,
                           reasoning_effort=effort)
    digest = request_payload_hash(history, provider=req.provider, model=req.model, prompt="hello",
                                  max_tokens=req.max_tokens, reasoning_effort=effort)
    assert digest == AIGateway._payload_hash(req)
    old = {"provider": req.provider, "model": req.model, "messages": req.messages,
           "max_tokens": req.max_tokens, "enable_tools": False, "enable_web_search": False,
           "strict_continuity": True}
    legacy = hashlib.sha256(json.dumps(old, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert (digest == legacy) is (effort is None)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openrouter", "openai", "gemini", "anthropic", "mistral"])
async def test_gateway_rejects_before_catalog_credentials_wallet_or_provider(monkeypatch, provider):
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    catalog = SimpleNamespace(get_model=AsyncMock())
    gateway = AIGateway(None, None, catalog)
    req = upstream.request(provider=provider, reasoning_effort="high")
    with pytest.raises(ContractError):
        await gateway._prepare_inference_execution(req)
    with pytest.raises(ContractError):
        await gateway._route_to_provider(req, "not-used")
    catalog.get_model.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.parametrize("effort,allowed", [("high", True), ("medium", False), ("ultra", False)])
async def test_routes_forward_only_reviewed_effort(monkeypatch, endpoint, effort, allowed):
    setup = route_fixture.setup_route.__wrapped__(monkeypatch)
    install(monkeypatch)
    response = await route_fixture.post(setup, endpoint, reasoning_effort=effort)
    if allowed:
        assert response.status_code == 200
        assert setup.gateway.infer.call_args.args[0].reasoning_effort == effort
    else:
        setup.gateway.infer.assert_not_awaited()
        setup.thread.prepare_thread.assert_not_awaited()
        assert response.status_code in {400, 422} or "response.error" in response.text


@pytest.mark.asyncio
async def test_model_validate_checks_requested_effort(monkeypatch):
    from server.routes import models
    from fastapi import HTTPException
    install(monkeypatch)
    catalog = SimpleNamespace(validate_selection=AsyncMock(return_value={
        "provider": "openrouter", "model_id": upstream.MODEL,
        "capabilities": model_capabilities("openrouter", upstream.MODEL),
    }))
    monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "1")
    monkeypatch.setattr(models, "get_catalog", lambda: catalog)
    request = ModelSelectionRequest(provider="openrouter", model=upstream.MODEL, reasoning_effort="high")
    user = {"user_id": "11111111-1111-4111-8111-111111111111"}
    assert (await validate_model_selection(request, user))["selection_valid"]
    request.reasoning_effort = "medium"
    with pytest.raises(HTTPException):
        await validate_model_selection(request, user)
    assert catalog.validate_selection.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["ask", "ask_stream", "conversation_ask"])
@pytest.mark.parametrize("effort", [None, "xhigh", "none"])
async def test_client_keeps_effort_in_http_payload(method, effort):
    requests = []
    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, text="" if method == "ask_stream" else "{}")
    client = OpenVegasClient.__new__(OpenVegasClient)
    client.base_url, client.token = "https://fixture.invalid", None
    kwargs = {"reasoning_effort": effort}
    if method == "conversation_ask":
        kwargs.update(thread_id="thread", expected_revision="revision", idempotency_key="key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client._http_client = http
        async def request(verb, path, **params):
            return (await http.request(verb, client.base_url + path, **params)).json()
        client._request = request
        result = getattr(client, method)("hello", "openrouter", upstream.MODEL, **kwargs)
        if method == "ask_stream":
            assert [event async for event in result] == []
        else:
            await result
    assert requests[0].get("reasoning_effort") == effort
    assert ("reasoning_effort" in requests[0]) is (effort is not None)


@pytest.mark.asyncio
async def test_canonical_effort_validated_before_pending_and_carried_to_gateway(monkeypatch):
    db, service, catalog = continuity.setup.__wrapped__(monkeypatch)
    install(monkeypatch)
    db.models[("openrouter", upstream.MODEL)] = upstream.catalog_row()
    created = await service.create_canonical_thread(user_id=continuity.USER, provider="openrouter",
                                                    model_id=upstream.MODEL, catalog=catalog)
    gateway = SimpleNamespace(infer=AsyncMock(return_value=InferenceResult("Answer", 1, 2, completion_status="complete")))
    args = dict(user_id=continuity.USER, thread_id=created.thread_id, provider="openrouter",
                model_id=upstream.MODEL, expected_revision=created.revision, prompt="hello",
                idempotency_key=continuity.KEY, catalog=catalog, gateway=gateway)
    with pytest.raises(ContractError):
        await service.infer_canonical(**args, reasoning_effort="medium")
    assert "pending" not in db.messages[created.thread_id][0]["content"]
    gateway.infer.assert_not_awaited()
    result = await service.infer_canonical(**args, reasoning_effort="high")
    assert not result["continuity_blocked"]
    req = gateway.infer.call_args.args[0]
    assert req.reasoning_effort == "high" and req.strict_continuity
    assert CanonicalAskRequest(**{k: v for k, v in args.items() if k in {
        "thread_id", "provider", "expected_revision", "prompt", "idempotency_key"
    }}, model=upstream.MODEL, reasoning_effort="high").reasoning_effort == "high"


@pytest.mark.asyncio
async def test_recovery_requires_original_effort_without_live_review(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    db = recovery.MemoryDB()
    original = db.data["request"]["payload_hash"]
    # Reconstruct the same committed input with an explicitly selected effort.
    history = CanonicalConversation.from_storage({
        k: v for k, v in json.loads(db.data["records"][0]["content"]).items() if k != "pending"
    })
    db.data["request"]["payload_hash"] = request_payload_hash(
        history, provider=db.data["thread"]["provider"], model=db.data["thread"]["model_id"],
        prompt=recovery.PROMPT, max_tokens=1024, reasoning_effort="high",
    )
    assert db.data["request"]["payload_hash"] != original
    with pytest.raises(ReconciliationError, match="ORIGINAL_REQUEST_HASH_MISMATCH"):
        await recovery.plan(db, prompt=recovery.PROMPT)
    planned = await recovery.plan(db, prompt=recovery.PROMPT, reasoning_effort="high")
    result = await recovery.apply(db, planned["plan_token"], reasoning_effort="high")
    assert result["status"] == "restored"


@pytest.mark.asyncio
async def test_canonical_http_route_carries_effort_without_reasoning_state(monkeypatch):
    import test_continuity_route_integration as routes
    setup = continuity.setup.__wrapped__(monkeypatch)
    db, service, catalog = setup
    install(monkeypatch)
    db.models[("openrouter", upstream.MODEL)] = upstream.catalog_row()
    created = await service.create_canonical_thread(
        user_id=continuity.USER, provider="openrouter", model_id=upstream.MODEL, catalog=catalog,
    )
    app, _, gateway, _, _ = routes.app_setup.__wrapped__(setup, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.post("/models/conversations/ask", json={
            "provider": "openrouter", "model": upstream.MODEL,
            "thread_id": created.thread_id, "expected_revision": created.revision,
            "prompt": "hello", "idempotency_key": continuity.KEY, "reasoning_effort": "high",
        })
    assert response.status_code == 200, response.text
    assert gateway.infer.call_args.args[0].reasoning_effort == "high"
    assert "reasoning" not in json.dumps(db.messages)


def test_operator_flag_carries_effort_to_inspect_and_restore(monkeypatch):
    captured = []
    async def run(args):
        captured.append(args.reasoning_effort)
        return {"status": "inspection_only"}
    monkeypatch.setattr(recovery.command, "run", run)
    args = ["--user", recovery.USER, "--thread", recovery.THREAD, "--request", recovery.REQUEST]
    assert recovery.command.main([*args, "--reasoning-effort", "high"]) == 0
    assert recovery.command.main(args) == 0
    assert captured == ["high", None]


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", [None, "high", "xhigh"])
async def test_actual_transport_honors_helper_and_never_returns_native_thoughts(monkeypatch, effort):
    install(monkeypatch)
    sent = []
    body = upstream.response()
    message = body["choices"][0]["message"]
    message.update(reasoning="private-native-thought", reasoning_content="private-native-thought",
                   reasoning_details=[{"type": "reasoning.text", "text": "private-native-thought"}])
    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json=body)
    req = upstream.request(reasoning_effort=effort)
    req._managed_model_config = upstream.catalog_row()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        gateway = AIGateway(None, None, None, http)
        result = await gateway._route_to_provider(req, upstream.SYNTHETIC_CREDENTIAL)
    assert len(sent) == 1
    assert sent[0]["provider"]["require_parameters"] is True
    assert sent[0].get("reasoning") == (None if effort is None else {"effort": effort, "exclude": True})
    assert result.text == "Answer" and "private-native-thought" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True])
async def test_operator_run_passes_effort_to_recovery_without_provider_io(monkeypatch, tmp_path, apply):
    connection = recovery.LocalConnection()
    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=AsyncMock(return_value=connection)))
    monkeypatch.setenv(recovery.command.DB_ENV, "postgresql://fixture@127.0.0.1/ov_test_reconciliation")
    inspect = AsyncMock(return_value={"status": "inspection_only"})
    restore = AsyncMock(return_value={"status": "restored"})
    monkeypatch.setattr(recovery.command, "inspect_turn", inspect)
    monkeypatch.setattr(recovery.command, "restore_turn", restore)
    path = tmp_path / "prompt.json"
    path.write_text(json.dumps({"prompt": recovery.PROMPT, "max_tokens": 1024}))
    path.chmod(0o600)
    args = recovery.arguments(prompt_file=str(path), reasoning_effort="xhigh", apply=apply,
                              confirm_request=recovery.REQUEST, confirm_plan="a" * 64,
                              operator=recovery.OPERATOR)
    await recovery.command.run(args)
    called, unused = (restore, inspect) if apply else (inspect, restore)
    assert called.call_args.kwargs["reasoning_effort"] == "xhigh"
    called.assert_awaited_once()
    unused.assert_not_awaited()
