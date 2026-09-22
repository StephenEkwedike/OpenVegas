"""Reviewed search discovery and authenticated routes; no paid requests."""

import asyncio
import base64
import copy
import json
import socket
from contextlib import suppress
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
import test_openrouter as transport
import test_openrouter_attachment_request as uploads
import test_openrouter_catalog as catalog
import test_openrouter_route_preflight as route
import test_openrouter_web_gateway as web

from openvegas.capabilities import get_caps, resolve_capability
from openvegas.gateway.inference import AIGateway
from openvegas.gateway.openrouter import build_payload
from openvegas.gateway.openrouter_catalog import ReviewError
from openvegas.gateway.providers import model_capabilities
from server.services.file_uploads import FileUploadService


@pytest.fixture
def setup(monkeypatch):
    result = route.setup_route.__wrapped__(monkeypatch)
    value = web.review()
    value["capabilities"]["web_search"] = True
    transport.install_review(monkeypatch, value)
    result.review = value
    monkeypatch.setenv("OPENVEGAS_ENABLE_WEB_SEARCH", "1")
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
async def test_reviewed_web_reaches_authenticated_gateway(setup, endpoint):
    response = await route.post(
        setup, endpoint, enable_web_search=True, idempotency_key=web.KEY,
    )
    assert response.status_code == 200 and "Fixture answer" in response.text
    req = setup.gateway.infer.call_args.args[0]
    assert req.enable_web_search is True
    payload = build_payload(req, setup.state["row"], model_capabilities("openrouter", web.MODEL))
    assert payload["tools"][0]["type"] == "openrouter:web_search"
    assert "capability_unavailable:web_search" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no_review", "expired", "price_changed", "no_key", "rollout"])
async def test_unreviewed_web_never_silently_becomes_plain_answer(setup, monkeypatch, case):
    value = copy.deepcopy(setup.review)
    key = web.KEY
    if case == "no_review":
        value.pop("web_search")
    elif case == "expired":
        value["web_search"]["execution"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    elif case == "price_changed":
        setup.state["row"]["v_price_input_per_1m"] = "11"
    elif case == "rollout":
        monkeypatch.setenv("OPENVEGAS_ROLLOUT_WEB_SEARCH_PCT", "0")
    else:
        key = None
    transport.install_review(monkeypatch, value)
    response = await route.post(setup, "ask", enable_web_search=True, idempotency_key=key)
    assert response.status_code == 400
    setup.gateway.infer.assert_not_awaited()
    setup.thread.append_exchange.assert_not_awaited()


def test_web_discovery_requires_exact_price_and_execution_review(setup, monkeypatch):
    assert get_caps("openrouter", web.MODEL).web_search
    assert resolve_capability("openrouter", web.MODEL, "web_search")
    assert not resolve_capability("openrouter", "other/model", "web_search")
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    monkeypatch.setenv("OPENVEGAS_CAPABILITY_OVERRIDES_JSON", json.dumps({
        "openrouter:*": {"web_search": True},
    }))
    assert not get_caps("openrouter", web.MODEL).web_search
    assert not resolve_capability("openrouter", web.MODEL, "web_search")


def plan():
    value = catalog.plan()
    entry = value["models"][0]
    policy = copy.deepcopy(web.review()["web_search"])
    expires = (catalog.NOW + timedelta(hours=1)).isoformat()
    policy["execution"].update(
        expires_at=expires, model=entry["model_id"],
        context_window_tokens=entry["context_window_tokens"],
        max_output_tokens=entry["max_tokens"],
    )
    policy["prices"].update(
        expires_at=expires,
        supplier_input_usd_per_million=entry["cost_input_per_1m"],
        supplier_output_usd_per_million=entry["cost_output_per_1m"],
        retail_input_v_per_million=entry["v_price_input_per_1m"],
        retail_output_v_per_million=entry["v_price_output_per_1m"],
        supplier_cap_usd="1", retail_cap_v="100",
    )
    entry["web_search"] = policy
    entry["capabilities"]["web_search"] = True
    return value


def test_catalog_keeps_reviewed_web_disabled_until_operator_activation():
    bundle = catalog.build(review=plan())
    assert not bundle["provider_catalog"][0]["enabled"]
    review = bundle["model_reviews"]["openrouter:vendor/model-v2"]
    assert review["capabilities"]["web_search"] is True
    assert review["pricing_scope"]["server_tools"] == "bounded_exa_search"
    assert review["web_search"]["prices"]["retail_search_v_per_call"] == "0.2"


@pytest.mark.parametrize("case", ["no_policy", "no_claim", "model", "price", "expired", "cap"])
def test_invalid_web_review_never_builds_installable_bundle(case):
    value = plan()
    entry = value["models"][0]
    if case == "no_policy":
        entry.pop("web_search")
    elif case == "no_claim":
        entry["capabilities"]["web_search"] = False
    elif case == "model":
        entry["web_search"]["execution"]["model"] = "wrong/model"
    elif case == "price":
        entry["web_search"]["prices"]["retail_input_v_per_million"] = "1"
    elif case == "expired":
        entry["web_search"]["execution"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    else:
        entry["web_search"]["prices"]["supplier_cap_usd"] = "0.00001"
    with pytest.raises(ReviewError):
        catalog.build(review=value)


@pytest_asyncio.fixture
async def integrated(setup, monkeypatch):
    """Real authenticated route, gateway and adapter; fake only external boundaries.

    Keep Descartes's private HTTP replay DB untouched. Mirror committed gateway
    rows into it because the existing billing and route SQL fakes are separate.
    This exercises the replay contract, not PostgreSQL isolation guarantees.
    """
    def no_network(*args, **kwargs):
        pytest.fail("Authenticated web composition tests must not open a network socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setenv("WRAPPER_REWARDS_ENABLED", "false")
    monkeypatch.setenv("OPENVEGAS_ENABLE_FILES", "1")
    monkeypatch.setenv("OPENVEGAS_CHAT_MAX_ATTACHMENTS", "3")
    clients = []

    def create(*, flat=False, provider_mismatch=False):
        model = "google/fixture-model-20260901" if flat else web.MODEL
        setup.state["row"]["model_id"] = model
        policy = copy.deepcopy(setup.review)
        attachment_review = uploads.model_review()
        policy.update(
            attachments=attachment_review["attachments"],
            observed_pricing=attachment_review["observed_pricing"],
            max_tokens=1024,
            context_window_tokens=16384,
            v_price_input_per_1m="10",
            v_price_output_per_1m="20",
        )
        policy["capabilities"].update(image_input=True)
        policy["web_search"]["execution"].update(model=model, context_window_tokens=16384)
        policy["attachments"].update(
            model_id=model,
            provider="other/endpoint" if provider_mismatch else "fixture/endpoint",
        )
        transport.install_review(monkeypatch, policy, model=model)
        upload_db = uploads.UploadDB()
        monkeypatch.setattr(
            route.routes, "get_file_upload_service", lambda: FileUploadService(upload_db)
        )
        body = web.response()
        body["model"] = model
        body["choices"][0]["finish_reason"] = "tool_calls"
        arguments = {"path": "README.md"}
        body["choices"][0]["message"]["tool_calls"] = [{
            "id": "call-route-read-preserved",
            "type": "function",
            "native_inference_request_id": "untrusted-provider-reference",
            "function": {
                "name": "Read" if flat else "call_local_tool",
                "arguments": json.dumps(
                    arguments if flat else {"tool_name": "Read", "arguments": arguments}
                ),
            },
        }]
        billing = web.MemoryDB()
        payloads, requests, results = [], [], []

        async def respond(request):
            assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
            assert request.headers["authorization"] == "Bearer " + web.TOKEN
            hold = next(iter(billing.data["holds"].values()))
            assert hold["status"] == "reserved"
            # Two full input contexts, two output budgets, one explicit retail fee.
            assert hold["reserved_v"] == Decimal("0.56864")
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json=body)

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        clients.append(client)
        gateway = AIGateway(
            billing, web.MemoryWallet(billing),
            SimpleNamespace(get_model=AsyncMock(return_value=copy.deepcopy(setup.state["row"]))),
            client,
        )
        gateway._resolve_provider_api_key = AsyncMock(return_value=web.TOKEN)

        def committed(req, result):
            row = billing.data["requests"][result.inference_request_id]
            assert row["status"] == "succeeded" and row["user_id"] == uploads.OWNER
            setup.db.gateway_rows[(row["user_id"], req.idempotency_key)] = copy.deepcopy(row)
            results.append(result)

        async def infer(req):
            requests.append(req)
            result = await gateway.infer(req)
            committed(req, result)
            return result

        setup.gateway.infer.side_effect = infer
        setup.gateway.stream_infer = Mock(side_effect=AssertionError(
            "Buffered OpenRouter HTTP delivery must commit the route envelope via infer first"
        ))
        return SimpleNamespace(
            route=setup, model=model, policy=policy, upload_db=upload_db,
            billing=billing, payloads=payloads, requests=requests, results=results,
            gateway=gateway,
        )

    yield create
    for client in clients:
        await client.aclose()


def stream_events(response):
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines() if line.startswith("data: ")
    ]


def completed(response, endpoint):
    assert response.status_code == 200, response.text
    if endpoint == "ask":
        return response.json()
    events = stream_events(response)
    assert not [e for e in events if e["type"] == "response.error"], response.text
    final = [e["payload"] for e in events if e["type"] == "response.completed"]
    assert len(final) == 1 and final[0]["status"] == "ok"
    assert [e["payload"]["tool"] for e in events if e["type"] == "tool.call"] == final[0]["tool_calls"]
    return final[0]


def assert_once_billed(state):
    assert len(state.payloads) == len(state.requests) == len(state.results) == 1
    assert len(state.billing.data["usage"]) == len(state.billing.data["charges"]) == 1
    usage = state.billing.data["usage"][0]
    assert usage["v_cost"] == Decimal("0.212")
    assert usage["actual_cost_usd"] == Decimal("0.0082")
    assert state.results[0].web_search_cost_v == Decimal("0.2")
    assert state.results[0].web_search_requests == 1
    assert state.billing.data["balance"] == Decimal("9.788")
    assert all(hold["status"] == "settled" for hold in state.billing.data["holds"].values())
    state.route.gateway.infer.assert_awaited_once()
    state.route.gateway.stream_infer.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.parametrize("flat", [False, True], ids=["generic-tools", "gemini-named-tools"])
@pytest.mark.parametrize("file_ids", [[], [uploads.TEXT_ID], [uploads.IMAGE_ID], [uploads.TEXT_ID, uploads.IMAGE_ID]], ids=["text-only", "owned-text", "owned-image", "owned-both"])
async def test_authenticated_web_tools_and_owned_bytes_reach_real_gateway(
    integrated, endpoint, flat, file_ids,
):
    state = integrated(flat=flat)
    response = await route.post(
        state.route, endpoint, model=state.model, enable_tools=True,
        enable_web_search=True, attachments=file_ids, idempotency_key=web.KEY,
    )
    result = completed(response, endpoint)
    req = state.requests[0]
    assert req.account_id == "user:" + uploads.OWNER
    assert req.enable_tools is True and req.enable_web_search is True
    assert req._managed_web_context is not None
    assert (req._managed_attachment_context is not None) is bool(file_ids)
    payload = state.payloads[0]
    assert payload["tools"][0]["type"] == "openrouter:web_search"
    assert payload["tools"][0]["parameters"]["engine"] == "exa"
    assert payload["tools"][0]["parameters"]["max_uses"] == 1
    functions = [tool["function"]["name"] for tool in payload["tools"][1:]]
    assert "Read" in functions if flat else functions == ["call_local_tool"]
    assert payload["provider"]["only"] == ["fixture/endpoint"]
    assert payload["provider"]["allow_fallbacks"] is False
    assert payload["messages"] == req.messages
    if file_ids:
        assert set(state.upload_db.lookups) == {(file_id, uploads.OWNER) for file_id in file_ids}
        parts = [part for message in payload["messages"] if isinstance(message["content"], list) for part in message["content"]]
        if uploads.TEXT_ID in file_ids:
            text = state.upload_db.rows[uploads.TEXT_ID]["content_bytes"].decode("utf-8")
            assert any(part.get("text", "").endswith(text) for part in parts)
        if uploads.IMAGE_ID in file_ids:
            images = [part["image_url"]["url"] for part in parts if part["type"] == "image_url"]
            assert len(images) == 1 and images[0].startswith("data:image/png;base64,")
            assert base64.b64decode(images[0].split(",", 1)[1]) == state.upload_db.rows[uploads.IMAGE_ID]["content_bytes"]
    assert result["web_search_requested"] is True
    assert result["web_search_effective"] is True
    assert result["web_search_used"] is True
    assert result["web_search_retry_without_tool"] is False
    request_id = state.results[0].inference_request_id
    assert str(UUID(request_id)) == request_id
    assert result["tool_calls"] == [{
        "tool_name": "Read", "arguments": {"path": "README.md"},
        "shell_mode": "read_only", "timeout_sec": 30,
        "provider_call_id": "call-route-read-preserved",
        "native_inference_request_id": request_id,
    }]
    assert result["warnings"] == []
    assert Decimal(result["v_cost"]) == Decimal("0.212")
    assert_once_billed(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_endpoint", ["ask", "stream"])
@pytest.mark.parametrize("replay_endpoint", ["ask", "stream"])
async def test_route_replay_preserves_native_call_scope_without_reloading_upload_or_review(
    integrated, monkeypatch, first_endpoint, replay_endpoint,
):
    state = integrated(flat=True)
    command = {
        "model": state.model, "enable_tools": True, "enable_web_search": True,
        "attachments": [uploads.IMAGE_ID], "idempotency_key": web.KEY,
    }
    first = completed(await route.post(state.route, first_endpoint, **command), first_endpoint)
    lookups = list(state.upload_db.lookups)
    state.upload_db.rows.clear()
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    replay = completed(await route.post(state.route, replay_endpoint, **command), replay_endpoint)
    for field in ("tool_calls", "v_cost", "text", "web_search_used", "web_search_effective"):
        assert replay[field] == first[field]
    assert state.upload_db.lookups == lookups
    state.route.thread.append_exchange.assert_awaited_once()
    assert_once_billed(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.parametrize("case", ["foreign-owner", "provider-review-mismatch"])
async def test_incompatible_web_attachment_never_dispatches_or_reserves(integrated, endpoint, case):
    state = integrated(provider_mismatch=case == "provider-review-mismatch")
    if case == "foreign-owner":
        state.upload_db.rows[uploads.IMAGE_ID]["user_id"] = uploads.OTHER
    response = await route.post(
        state.route, endpoint, model=state.model, enable_tools=True,
        enable_web_search=True, attachments=[uploads.IMAGE_ID], idempotency_key=web.KEY,
    )
    if endpoint == "ask":
        assert response.status_code == (404 if case == "foreign-owner" else 400), response.text
        if case == "foreign-owner":
            assert response.json()["error"] == "attachment_unavailable"
    else:
        assert response.status_code == 200
        assert any(event["type"] == "response.error" for event in stream_events(response))
    if case == "provider-review-mismatch":
        assert "web_attachment_review_mismatch" in response.text
    assert not state.requests and not state.payloads
    assert not state.billing.data["holds"] and not state.billing.data["requests"]
    state.gateway._resolve_provider_api_key.assert_not_awaited()
    state.route.thread.append_exchange.assert_not_awaited()


async def interrupt_http_stream(app, command, *, boundary, interruption, on_delivery):
    """Drive ASGI directly: httpx's ASGITransport buffers and cannot test disconnects."""
    delivered, disconnected = asyncio.Event(), asyncio.Event()
    messages = []
    request_sent = False
    body = json.dumps(command).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/inference/stream", "raw_path": b"/inference/stream",
        "query_string": b"", "root_path": "", "server": ("test", 80),
        "client": ("127.0.0.1", 12345),
        "headers": [(b"content-type", b"application/json")], "state": {},
    }

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] != "http.response.body":
            return
        events = stream_events(SimpleNamespace(text=message.get("body", b"").decode()))
        for event in events:
            assert event["type"] != "response.error", event
            if event["type"] in {"tool_start", "tool.call", "response.delta"}:
                on_delivery(event)
            if event["type"] == boundary:
                delivered.set()
                # Stop exactly on this send so response.completed cannot race ahead.
                await asyncio.Event().wait()

    running = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(delivered.wait(), timeout=2)
        assert not running.done()
        if interruption == "disconnect":
            disconnected.set()
            await asyncio.wait_for(running, timeout=2)
        else:
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
    finally:
        if not running.done():
            running.cancel()
        with suppress(asyncio.CancelledError):
            await running
    assert messages[0]["type"] == "http.response.start" and messages[0]["status"] == 200
    events = stream_events(SimpleNamespace(text="".join(
        message.get("body", b"").decode() for message in messages
    )))
    assert any(event["type"] == boundary for event in events)
    assert not any(event["type"] == "response.completed" for event in events)
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["disconnect", "cancel"])
@pytest.mark.parametrize("boundary", ["tool.call", "response.delta"])
@pytest.mark.parametrize("replay_endpoint", ["ask", "stream"])
async def test_http_interruption_after_answer_replays_committed_history_tools_and_charge(
    integrated, monkeypatch, interruption, boundary, replay_endpoint,
):
    state = integrated(flat=True)
    thread_id = "33333333-3333-4333-8333-333333333333"
    state.route.thread.prepare_thread.side_effect = None
    state.route.thread.prepare_thread.return_value = SimpleNamespace(
        thread_id=thread_id, thread_status="active",
    )
    command = {
        "provider": "openrouter", "model": state.model, "prompt": "Fixture prompt",
        "enable_tools": True, "enable_web_search": True,
        "attachments": [uploads.IMAGE_ID], "idempotency_key": web.KEY,
        "thread_id": thread_id, "persist_context": True,
    }
    snapshots = []

    def on_delivery(event):
        assert_once_billed(state)
        row = state.route.db.rows[(uploads.OWNER, web.KEY)]
        assert row["status"] == "succeeded" and row["response_status"] == 200
        envelope = json.loads(row["response_body_text"])
        assert envelope["state"] == "completed"
        assert envelope["gateway_request_id"] == state.results[0].inference_request_id
        assert len(state.route.db.history) == 2
        assert state.route.db.history[0]["role"] == "user"
        assert state.route.db.history[0]["attachment_refs"]
        assert state.route.db.history[1] == {"role": "assistant", "content": "Evidence found."}
        state.route.thread.append_exchange.assert_awaited_once()
        if event["type"] in {"tool_start", "tool.call"}:
            assert event["payload"]["tool"] == envelope["response"]["tool_calls"][0]
        snapshots.append(copy.deepcopy((state.billing.data, state.route.db.history, row)))

    await interrupt_http_stream(
        state.route.app, command, boundary=boundary, interruption=interruption,
        on_delivery=on_delivery,
    )
    assert snapshots
    committed_billing, committed_history, committed_envelope = snapshots[0]
    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    lookups = list(state.upload_db.lookups)
    state.upload_db.rows.clear()
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    replay = completed(await route.post(state.route, replay_endpoint, **command), replay_endpoint)
    original = json.loads(committed_envelope["response_body_text"])["response"]
    for field in ("text", "v_cost", "tool_calls", "web_search_used", "web_search_effective"):
        assert replay[field] == original[field]
    assert state.billing.data == committed_billing
    assert state.route.db.history == committed_history
    assert state.route.db.rows[(uploads.OWNER, web.KEY)] == committed_envelope
    assert state.upload_db.lookups == lookups
    state.route.thread.prepare_thread.assert_awaited_once()
    state.route.thread.append_exchange.assert_awaited_once()
    assert_once_billed(state)
