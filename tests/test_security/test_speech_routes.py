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


def test_speech_transcribe_empty_text_returns_success(monkeypatch):
    class _EmptyGateway(_StubGateway):
        async def transcribe_audio(self, **kwargs):
            return {
                "provider": "openai",
                "model": "gpt-4o-mini-transcribe",
                "filename": "voice-note.m4a",
                "mime_type": "audio/m4a",
                "text": "",
                "diagnostics": {"latency_ms": 12.4, "empty_text": True},
            }

    reset_metrics()
    monkeypatch.setattr(speech_routes, "_speech_enabled", lambda: True)
    monkeypatch.setattr(speech_routes, "resolve_capability", lambda *a, **k: True)
    monkeypatch.setattr(speech_routes, "get_file_upload_service", lambda: _StubFileService())
    monkeypatch.setattr(speech_routes, "get_gateway", lambda: _EmptyGateway())
    client = TestClient(_app_with_router())

    resp = client.post("/speech/transcribe", json={"file_id": "file-1"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["file_id"] == "file-1"
    assert body["text"] == ""
    assert body["diagnostics"]["empty_text"] is True
    snap = get_metrics_snapshot()
    assert any(k.startswith("speech_transcribe_text_len_total|bucket=0") for k in snap)


def test_speech_transcribe_inline_audio_success(monkeypatch):
    class _InlineGateway:
        async def transcribe_audio(self, **kwargs):
            assert kwargs["filename"] == "voice-note.wav"
            assert kwargs["mime_type"] == "audio/wav"
            assert kwargs["audio_bytes"] == b"\x00\x01\x02"
            return {
                "provider": "openai",
                "model": "gpt-4o-mini-transcribe",
                "filename": "voice-note.wav",
                "mime_type": "audio/wav",
                "text": "hello world",
                "diagnostics": {"latency_ms": 8.0},
            }

    monkeypatch.setattr(speech_routes, "_speech_enabled", lambda: True)
    monkeypatch.setattr(speech_routes, "resolve_capability", lambda *a, **k: True)
    monkeypatch.setattr(speech_routes, "get_gateway", lambda: _InlineGateway())
    client = TestClient(_app_with_router())

    resp = client.post(
        "/speech/transcribe",
        json={
            "content_base64": "AAEC",
            "filename": "voice-note.wav",
            "mime_type": "audio/wav",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["file_id"] == ""
    assert body["filename"] == "voice-note.wav"
    assert body["text"] == "hello world"


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


def test_speech_transcribe_gateway_failure_logs_context(monkeypatch):
    class _FailingGateway:
        async def transcribe_audio(self, **kwargs):
            raise RuntimeError("upstream boom")

    logged: dict[str, object] = {}

    class _Logger:
        def exception(self, message, *args):
            logged["message"] = message
            logged["args"] = args

    monkeypatch.setattr(speech_routes, "_speech_enabled", lambda: True)
    monkeypatch.setattr(speech_routes, "resolve_capability", lambda *a, **k: True)
    monkeypatch.setattr(speech_routes, "get_file_upload_service", lambda: _StubFileService())
    monkeypatch.setattr(speech_routes, "get_gateway", lambda: _FailingGateway())
    monkeypatch.setattr(speech_routes, "logger", _Logger())
    client = TestClient(_app_with_router())

    resp = client.post("/speech/transcribe", json={"file_id": "file-1"})

    assert resp.status_code == 502
    assert resp.json()["error"] == "speech_transcription_failed"
    assert logged["message"] == (
        "speech_transcribe_failed user_id=%s file_id=%s provider=%s model=%s mime_type=%s filename=%s"
    )
    assert logged["args"] == (
        "u-1",
        "file-1",
        "openai",
        "gpt-4o-mini-transcribe",
        "audio/m4a",
        "voice-note.m4a",
    )
