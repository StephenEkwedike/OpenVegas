from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.middleware.auth import get_current_user
from server.routes import speech as speech_routes
from openvegas.telemetry import get_metrics_snapshot, reset_metrics


def _app_with_router() -> FastAPI:
    app = FastAPI()
    app.include_router(speech_routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u-1"}
    return app


class _StubFileService:
    async def resolve_uploaded_for_inference(self, *, user_id: str, file_ids: list[str]):
        assert user_id == "u-1"
        assert file_ids == ["file-1"]
        return [
            {
                "file_id": "file-1",
                "filename": "voice-note.m4a",
                "mime_type": "audio/m4a",
                "content_bytes": b"\x00\x01\x02",
            }
        ]


class _StubGateway:
    async def transcribe_audio(self, **kwargs):
        assert kwargs["provider"] == "openai"
        assert kwargs["model"] == "gpt-4o-mini-transcribe"
        assert kwargs["filename"] == "voice-note.m4a"
        return {
            "provider": "openai",
            "model": "gpt-4o-mini-transcribe",
            "filename": "voice-note.m4a",
            "mime_type": "audio/m4a",
            "text": "hello world",
            "diagnostics": {"latency_ms": 12.4},
        }


def test_speech_transcribe_success(monkeypatch):
    reset_metrics()
    monkeypatch.setattr(speech_routes, "_speech_enabled", lambda: True)
    monkeypatch.setattr(speech_routes, "resolve_capability", lambda *a, **k: True)
    monkeypatch.setattr(speech_routes, "get_file_upload_service", lambda: _StubFileService())
    monkeypatch.setattr(speech_routes, "get_gateway", lambda: _StubGateway())
    client = TestClient(_app_with_router())
    resp = client.post("/speech/transcribe", json={"file_id": "file-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["file_id"] == "file-1"
    assert body["text"] == "hello world"
    assert body["mime_type"] == "audio/m4a"
    snap = get_metrics_snapshot()
    assert any(k.startswith("speech_transcribe_text_len_total|bucket=1-40") for k in snap)


def test_speech_transcribe_unsupported_mime(monkeypatch):
    class _NonAudioFileService(_StubFileService):
        async def resolve_uploaded_for_inference(self, *, user_id: str, file_ids: list[str]):
            return [
                {
                    "file_id": "file-1",
                    "filename": "report.pdf",
                    "mime_type": "application/pdf",
                    "content_bytes": b"%PDF-1.7",
                }
            ]

    monkeypatch.setattr(speech_routes, "_speech_enabled", lambda: True)
    monkeypatch.setattr(speech_routes, "resolve_capability", lambda *a, **k: True)
    monkeypatch.setattr(speech_routes, "get_file_upload_service", lambda: _NonAudioFileService())
    monkeypatch.setattr(speech_routes, "get_gateway", lambda: _StubGateway())
    client = TestClient(_app_with_router())
    resp = client.post("/speech/transcribe", json={"file_id": "file-1"})
    assert resp.status_code == 415
    assert resp.json()["error"] == "unsupported_mime_type"


def test_speech_transcribe_feature_disabled(monkeypatch):
    monkeypatch.setattr(speech_routes, "_speech_enabled", lambda: False)
    client = TestClient(_app_with_router())
    resp = client.post("/speech/transcribe", json={"file_id": "file-1"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "feature_disabled"

