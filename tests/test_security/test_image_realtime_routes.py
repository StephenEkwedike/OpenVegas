from __future__ import annotations

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
    client = TestClient(_app_with_router(realtime_routes.router))
    session = client.post("/realtime/session", json={"provider": "openai", "model": "gpt-4o-realtime-preview"}).json()
    relay_id = session["relay_session_id"]

    with client.websocket_connect(f"/realtime/relay/{relay_id}/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "session.started"
        ws.send_json({"type": "audio.input.append", "pcm16": "AAAA"})
        ack = ws.receive_json()
        assert ack["type"] == "audio.input.ack"
        ws.send_json({"type": "response.cancel"})
        cancelled = ws.receive_json()
        assert cancelled["type"] == "response.cancelled"


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
