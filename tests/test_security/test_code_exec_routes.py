from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.middleware.auth import get_current_user
from server.routes import code_exec as code_exec_routes


def _app_with_router() -> FastAPI:
    app = FastAPI()
    app.include_router(code_exec_routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u-1"}
    return app


class _StubCodeExecService:
    async def create_job(self, *, user_id: str, language: str, code: str, timeout_sec: int):
        assert user_id == "u-1"
        assert language == "python"
        assert code
        return type("Job", (), {"id": "j1", "status": "queued", "runtime": "local"})

    async def get_job(self, *, user_id: str, job_id: str):
        assert user_id == "u-1"
        if job_id != "j1":
            return None
        return type(
            "Job",
            (),
            {
                "id": "j1",
                "status": "succeeded",
                "language": "python",
                "timeout_sec": 10,
                "created_at": "now",
                "started_at": "now",
                "completed_at": "now",
                "exit_code": 0,
                "error": None,
                "stdout": "ok",
                "stderr": "",
                "runtime": "local",
                "artifacts": [{"path": "out.txt", "mime_type": "text/plain", "size_bytes": 2}],
            },
        )

    @staticmethod
    def serialize(job):
        return {
            "job_id": job.id,
            "status": job.status,
            "language": job.language,
            "timeout_sec": job.timeout_sec,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "exit_code": job.exit_code,
            "error": job.error,
            "stdout": job.stdout,
            "stderr": job.stderr,
            "runtime": job.runtime,
            "artifacts": job.artifacts,
        }


def test_code_exec_routes_success(monkeypatch):
    monkeypatch.setattr(code_exec_routes, "_code_exec_enabled", lambda: True)
    monkeypatch.setattr(code_exec_routes, "get_code_exec_service", lambda: _StubCodeExecService())
    client = TestClient(_app_with_router())

    create_resp = client.post("/code-exec/jobs", json={"language": "python", "code": "print('ok')", "timeout_sec": 10})
    assert create_resp.status_code == 200
    assert create_resp.json()["job_id"] == "j1"
    assert create_resp.json()["runtime"] == "local"

    status_resp = client.get("/code-exec/jobs/j1")
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "succeeded"

    result_resp = client.get("/code-exec/jobs/j1/result")
    assert result_resp.status_code == 200
    assert result_resp.json()["stdout"] == "ok"
    assert result_resp.json()["artifacts"][0]["path"] == "out.txt"


def test_code_exec_feature_disabled(monkeypatch):
    monkeypatch.setattr(code_exec_routes, "_code_exec_enabled", lambda: False)
    client = TestClient(_app_with_router())
    resp = client.post("/code-exec/jobs", json={"language": "python", "code": "print('ok')"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "feature_disabled"
