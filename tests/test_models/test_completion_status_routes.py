"""Actual HTTP/SSE routing with offline gateway and persistence boundaries."""

import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from openvegas.emotes.events import Phase
from server.routes import inference as routes
from tests.test_emotes.test_cli_finalization_events import _bridge, _outer_turn, _terminal
from tests.test_models import test_cli_native_loop as native

loop_driver = native.loop_driver

MISSING = object()


@pytest.fixture
def setup(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No network or database access is permitted")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(routes, "emit_run_metrics", lambda **kwargs: None)
    result = SimpleNamespace(
        text="Visible answer", v_cost="0.25", input_tokens=3, output_tokens=4,
        provider_request_id="fixture", tool_calls=[], completion_status="complete",
        _native_envelope={"secret": "PRIVATE_ENVELOPE_SENTINEL"},
    )
    gateway = SimpleNamespace(infer=AsyncMock(return_value=result))
    thread = SimpleNamespace(append_exchange=AsyncMock())
    state = SimpleNamespace(result=result, gateway=gateway, thread=thread, saved=None, replay=None)

    async def complete(claim, *, response, append, **kwargs):
        await append()
        state.saved = response.copy()
        return response

    replay = SimpleNamespace(
        begin=AsyncMock(side_effect=lambda **kwargs: SimpleNamespace(
            response=state.replay, native_claim=None, gateway_idempotency_key="gateway-key")),
        complete=AsyncMock(side_effect=complete), abandon_before_dispatch=AsyncMock(),
    )
    monkeypatch.setattr(routes, "get_inference_replay_service", lambda: replay)
    monkeypatch.setattr(routes, "get_fraud_engine", lambda: SimpleNamespace(check_inference=AsyncMock()))

    async def prepare(req, *, run_id, started, **kwargs):
        return routes._PreparedAskContext(
            req=req, started=started, run_id=run_id, gateway=gateway, thread_svc=thread,
            thread_ctx=SimpleNamespace(thread_id="thread", thread_status="active"),
            mode_payload={}, context_enabled=False,
            inference_request=SimpleNamespace(_managed_attachment_context=None),
            web_search_requested=False, web_search_effective=False,
            attachments_requested=False, attachments_effective=False, attachments_used=False,
            response_warnings=[], history_messages_loaded=0, history_messages_skipped=0,
            history_messages_used=0, history_messages_dropped=0, did_prune=False,
        )

    monkeypatch.setattr(routes, "_prepare_authorized_ask_context", prepare)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_current_user] = lambda: {"user_id": "offline-user"}
    state.app = app
    return state


async def request(setup, transport):
    provider = "openai" if transport == "direct_stream" else "openrouter"
    if transport == "direct_stream":
        async def stream_infer(req):
            yield {"type": "text_delta", "text": setup.result.text}
            yield {"type": "result", "result": setup.result}
        setup.gateway.stream_infer = stream_infer
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup.app), base_url="http://offline") as client:
        response = await client.post("/ask" if transport == "http" else "/stream", json={
            "prompt": "Hello", "provider": provider, "model": "fixture-model",
            "idempotency_key": "offline-key",
        })
    assert response.status_code == 200, response.text
    assert "PRIVATE_ENVELOPE_SENTINEL" not in response.text
    if transport == "http":
        return response.json(), []
    events = []
    for frame in response.text.strip().split("\n\n"):
        lines = frame.splitlines()
        events.append({"event": lines[0].removeprefix("event: "),
                       "data": json.loads(lines[1].removeprefix("data: "))})
    final = [row["data"]["payload"] for row in events if row["event"] == "response.completed"]
    assert len(final) == 1
    return final[0], events


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "buffered_stream", "direct_stream"])
@pytest.mark.parametrize("status", ["complete", "incomplete", "unknown", "failed", "cancelled",
                                   None, True, ["complete"], {"status": "complete"}, MISSING])
async def test_fresh_status_is_preserved_or_fail_neutral(setup, transport, status):
    if status is MISSING:
        del setup.result.completion_status
    else:
        setup.result.completion_status = status
    payload, _ = await request(setup, transport)
    expected = status if type(status) is str and status in {"complete", "incomplete"} else "incomplete"
    assert payload["completion_status"] == expected
    assert payload["text"] == "Visible answer"
    assert "native_generation" not in payload
    setup.thread.append_exchange.assert_awaited_once()
    if transport != "direct_stream":
        assert setup.saved["completion_status"] == expected
        setup.replay = setup.saved
        replayed, _ = await request(setup, transport)
        assert replayed["completion_status"] == expected
        setup.gateway.infer.assert_awaited_once()
        setup.thread.append_exchange.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "buffered_stream"])
@pytest.mark.parametrize("status", [MISSING, "complete", "incomplete"])
async def test_historical_replay_preserves_missing_status(setup, transport, status):
    setup.replay = {"text": "Historical answer", "tool_calls": [], "v_cost": "0.25"}
    if status is not MISSING:
        setup.replay["completion_status"] = status
    payload, _ = await request(setup, transport)
    if status is MISSING:
        assert "completion_status" not in payload
    else:
        assert payload["completion_status"] == status
    setup.gateway.infer.assert_not_awaited()
    setup.thread.append_exchange.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "buffered_stream", "direct_stream"])
@pytest.mark.parametrize("status", ["complete", "incomplete", "unknown"])
async def test_route_to_production_cli_mapping_and_real_bridge(setup, loop_driver, transport, status):
    setup.result.completion_status = status
    payload, events = await request(setup, transport)
    driver = loop_driver(batches=[])
    bridge, emitted = _bridge(driver)
    driver.namespace["_env_flag"] = lambda name, default: (
        transport != "http" if name == "OPENVEGAS_CHAT_STREAM_EVENTS" else
        False if name in {"OPENVEGAS_CHAT_NATIVE_GENERATION_SCOPE", "OPENVEGAS_CHAT_NATIVE_GENERATION_HISTORY"}
        else default == "1"
    )
    driver.client.ask = AsyncMock(return_value=payload)

    async def stream(*args, **kwargs):
        for event in events:
            yield event

    driver.client.ask_stream = stream
    await _outer_turn(driver, bridge, message="Hello")()
    assert driver.rendered == ["Visible answer"]
    assert _terminal(emitted) == [Phase.COMPLETE if status == "complete" else Phase.ERROR]
    if transport == "http":
        driver.client.ask.assert_awaited_once()
    else:
        driver.client.ask.assert_not_awaited()
