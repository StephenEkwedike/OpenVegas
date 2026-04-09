from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.middleware.auth import get_current_user
from server.routes import image_gen as image_gen_routes
from server.routes import realtime as realtime_routes


def _app_with_router(router) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u-1"}
    return app


class _StubGateway:
    async def generate_image(self, **kwargs):
        assert kwargs["provider"] == "openai"
        return {
            "provider": "openai",
            "model": "gpt-image-1",
            "image_url": "https://example.com/a.png",
            "usage": {"image_count": 1, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "diagnostics": {"provider_request_id": "req_1", "latency_ms": 123.4},
        }

    async def create_realtime_session(self, **kwargs):
        assert kwargs["provider"] == "openai"
        return {"id": "sess_1", "client_secret": {"value": "secret"}}


class _FakeUpstreamWS:
    def __init__(self):
        self.sent: list[str] = []
        self.closed = False
        self._rx_queue: asyncio.Queue[str] = asyncio.Queue()
        self._rx_queue.put_nowait(json.dumps({"type": "session.created"}))

    async def send(self, payload):
        text = payload.decode("utf-8", errors="ignore") if isinstance(payload, (bytes, bytearray)) else str(payload)
        self.sent.append(text)
        event_type, _audio_bytes = realtime_routes._decode_event_metadata(text)
        if event_type in {"audio.input.append", "input_audio_buffer.append"}:
            await self._rx_queue.put(json.dumps({"type": "input_audio_buffer.committed"}))
        if event_type == "response.cancel":
            await self._rx_queue.put(json.dumps({"type": "response.cancelled", "reason": "fake_upstream"}))

    async def recv(self):
        return await self._rx_queue.get()

    async def close(self):
        self.closed = True


def test_image_generate_route_success(monkeypatch):
    monkeypatch.setattr(image_gen_routes, "_image_gen_enabled", lambda: True)
    monkeypatch.setattr(image_gen_routes, "resolve_capability", lambda *a, **k: True)
    monkeypatch.setattr(image_gen_routes, "get_gateway", lambda: _StubGateway())
    client = TestClient(_app_with_router(image_gen_routes.router))
    resp = client.post("/images/generate", json={"prompt": "a horse", "provider": "openai", "model": "gpt-image-1"})
    assert resp.status_code == 200
    assert resp.json()["provider"] == "openai"
    assert resp.json()["diagnostics"]["provider_request_id"] == "req_1"


def test_realtime_session_route_success(monkeypatch):
    monkeypatch.setattr(realtime_routes, "_realtime_enabled", lambda: True)
    monkeypatch.setattr(realtime_routes, "resolve_capability", lambda *a, **k: True)
    monkeypatch.setattr(realtime_routes, "get_gateway", lambda: _StubGateway())
    client = TestClient(_app_with_router(realtime_routes.router))
    resp = client.post("/realtime/session", json={"provider": "openai", "model": "gpt-4o-realtime-preview"})
    assert resp.status_code == 200
    assert resp.json()["id"] == "sess_1"
    assert isinstance(resp.json()["relay_session_id"], str)
    assert str(resp.json()["relay_ws_path"]).startswith("/realtime/relay/")


def test_realtime_websocket_relay_and_cancel(monkeypatch):
    monkeypatch.setattr(realtime_routes, "_realtime_enabled", lambda: True)
    monkeypatch.setattr(realtime_routes, "resolve_capability", lambda *a, **k: True)
    monkeypatch.setattr(realtime_routes, "get_gateway", lambda: _StubGateway())
    upstream = _FakeUpstreamWS()

    @asynccontextmanager
    async def _fake_connect(_session):
        yield upstream

    monkeypatch.setattr(realtime_routes, "_connect_realtime_upstream", _fake_connect)
    client = TestClient(_app_with_router(realtime_routes.router))
    session = client.post("/realtime/session", json={"provider": "openai", "model": "gpt-4o-realtime-preview"}).json()
    relay_id = session["relay_session_id"]

    with client.websocket_connect(f"/realtime/relay/{relay_id}/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "session.started"
        created = ws.receive_json()
        assert created["type"] == "session.created"
        ws.send_json({"type": "input_audio_buffer.append", "audio": "AAAA"})
        committed = ws.receive_json()
        assert committed["type"] == "input_audio_buffer.committed"
        ws.send_json({"type": "response.cancel"})
        seen_cancelled = False
        for _ in range(3):
            event = ws.receive_json()
            if event.get("type") == "response.cancelled":
                seen_cancelled = True
                break
        assert seen_cancelled

    sent_types = []
    for raw in upstream.sent:
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        sent_types.append(str(payload.get("type") or ""))
    assert sent_types.count("response.cancel") == 1


def test_realtime_cancel_endpoint(monkeypatch):
    monkeypatch.setattr(realtime_routes, "_realtime_enabled", lambda: True)
    monkeypatch.setattr(realtime_routes, "resolve_capability", lambda *a, **k: True)
    monkeypatch.setattr(realtime_routes, "get_gateway", lambda: _StubGateway())
    client = TestClient(_app_with_router(realtime_routes.router))
    session = client.post("/realtime/session", json={"provider": "openai", "model": "gpt-4o-realtime-preview"}).json()
    relay_id = session["relay_session_id"]
    cancel = client.post(f"/realtime/relay/{relay_id}/cancel", json={"reason": "user_cancel"})
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancel_requested"


def test_realtime_websocket_reports_upstream_connect_error(monkeypatch):
    monkeypatch.setattr(realtime_routes, "_realtime_enabled", lambda: True)
    monkeypatch.setattr(realtime_routes, "resolve_capability", lambda *a, **k: True)
    monkeypatch.setattr(realtime_routes, "get_gateway", lambda: _StubGateway())

    @asynccontextmanager
    async def _failing_connect(_session):
        raise RuntimeError("upstream_connect_failed:test")
        yield

    monkeypatch.setattr(realtime_routes, "_connect_realtime_upstream", _failing_connect)

    client = TestClient(_app_with_router(realtime_routes.router))
    session = client.post("/realtime/session", json={"provider": "openai", "model": "gpt-4o-realtime-preview"}).json()
    relay_id = session["relay_session_id"]
    with client.websocket_connect(f"/realtime/relay/{relay_id}/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "session.started"
        reconnecting = ws.receive_json()
        assert reconnecting["type"] == "session.reconnecting"
        # After reconnect budget is exhausted, relay emits terminal error.
        terminal = None
        for _ in range(5):
            candidate = ws.receive_json()
            if candidate.get("type") == "session.error":
                terminal = candidate
                break
        assert terminal is not None


def test_realtime_websocket_reconnects_after_transient_upstream_connect_failure(monkeypatch):
    monkeypatch.setattr(realtime_routes, "_realtime_enabled", lambda: True)
    monkeypatch.setattr(realtime_routes, "resolve_capability", lambda *a, **k: True)
    monkeypatch.setattr(realtime_routes, "get_gateway", lambda: _StubGateway())
    monkeypatch.setenv("OPENVEGAS_REALTIME_UPSTREAM_RECONNECT_MAX", "2")
    upstream = _FakeUpstreamWS()
    attempts = {"n": 0}

    @asynccontextmanager
    async def _flaky_connect(_session):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("upstream_connect_failed:transient")
        yield upstream

    monkeypatch.setattr(realtime_routes, "_connect_realtime_upstream", _flaky_connect)

    client = TestClient(_app_with_router(realtime_routes.router))
    session = client.post("/realtime/session", json={"provider": "openai", "model": "gpt-4o-realtime-preview"}).json()
    relay_id = session["relay_session_id"]
    with client.websocket_connect(f"/realtime/relay/{relay_id}/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "session.started"
        reconnecting = ws.receive_json()
        assert reconnecting["type"] == "session.reconnecting"
        created = ws.receive_json()
        assert created["type"] == "session.created"
