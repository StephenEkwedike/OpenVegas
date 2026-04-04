from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.middleware.auth import get_current_user
from server.routes import files as files_routes
from server.services.file_uploads import FileUploadError


def _app_with_router() -> FastAPI:
    app = FastAPI()
    app.include_router(files_routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "11111111-1111-1111-1111-111111111111"}
    return app


class _StubUploadService:
    async def upload_init(self, **kwargs):
        assert kwargs["user_id"] == "11111111-1111-1111-1111-111111111111"
        return {"upload_id": "up-1", "status": "pending", "expires_in_sec": 900}

    async def upload_complete(self, **kwargs):
        assert kwargs["user_id"] == "11111111-1111-1111-1111-111111111111"
        return {"upload_id": "up-1", "file_id": "up-1", "status": "uploaded"}

    async def search_uploaded_text(self, **kwargs):
        assert kwargs["user_id"] == "11111111-1111-1111-1111-111111111111"
        return [
            {
                "file_id": "up-1",
                "filename": "notes.txt",
                "mime_type": "text/plain",
                "size_bytes": 12,
                "snippet": "example",
            }
        ]


class _FailingUploadService:
    async def upload_init(self, **kwargs):
        raise FileUploadError(status_code=415, code="unsupported_mime_type", detail="bad mime")

    async def upload_complete(self, **kwargs):
        raise FileUploadError(status_code=410, code="upload_expired", detail="expired")


def test_upload_routes_success(monkeypatch):
    monkeypatch.setattr(files_routes, "get_file_upload_service", lambda: _StubUploadService())
    monkeypatch.setattr(files_routes, "_files_feature_enabled", lambda: True)
    client = TestClient(_app_with_router())

    init_resp = client.post(
        "/files/upload/init",
        json={
            "filename": "notes.txt",
            "size_bytes": 12,
            "mime_type": "text/plain",
            "sha256": "f" * 64,
        },
    )
    assert init_resp.status_code == 200
    assert init_resp.json()["upload_id"] == "up-1"

    done_resp = client.post(
        "/files/upload/complete",
        json={"upload_id": "up-1", "content_base64": "aGVsbG8="},
    )
    assert done_resp.status_code == 200
    assert done_resp.json()["status"] == "uploaded"

    search_resp = client.post("/files/search", json={"query": "example", "limit": 5})
    assert search_resp.status_code == 200
    assert len(search_resp.json()["hits"]) == 1


def test_upload_routes_return_structured_errors(monkeypatch):
    monkeypatch.setattr(files_routes, "get_file_upload_service", lambda: _FailingUploadService())
    monkeypatch.setattr(files_routes, "_files_feature_enabled", lambda: True)
    client = TestClient(_app_with_router())

    init_resp = client.post(
        "/files/upload/init",
        json={
            "filename": "bad.psd",
            "size_bytes": 20,
            "mime_type": "image/vnd.adobe.photoshop",
            "sha256": "f" * 64,
        },
    )
    assert init_resp.status_code == 415
    assert init_resp.json()["error"] == "unsupported_mime_type"

    done_resp = client.post(
        "/files/upload/complete",
        json={"upload_id": "stale", "content_base64": "aGVsbG8="},
    )
    assert done_resp.status_code == 410
    assert done_resp.json()["error"] == "upload_expired"


def test_upload_routes_feature_disabled(monkeypatch):
    monkeypatch.setattr(files_routes, "_files_feature_enabled", lambda: False)
    client = TestClient(_app_with_router())
    resp = client.post(
        "/files/upload/init",
        json={
            "filename": "notes.txt",
            "size_bytes": 12,
            "mime_type": "text/plain",
            "sha256": "f" * 64,
        },
    )
    assert resp.status_code == 503
    assert resp.json()["error"] == "feature_disabled"
