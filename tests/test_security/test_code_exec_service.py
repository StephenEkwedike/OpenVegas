from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.services.code_exec import CodeExecService


async def _wait_job(svc: CodeExecService, *, user_id: str, job_id: str, timeout_sec: float = 5.0):
    deadline = asyncio.get_event_loop().time() + timeout_sec
    while asyncio.get_event_loop().time() < deadline:
        job = await svc.get_job(user_id=user_id, job_id=job_id)
        if job and job.status in {"succeeded", "failed"}:
            return job
        await asyncio.sleep(0.05)
    return await svc.get_job(user_id=user_id, job_id=job_id)


@pytest.mark.asyncio
async def test_code_exec_service_collects_artifacts_local(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CODE_EXEC_RUNTIME", "local")
    monkeypatch.setenv("OPENVEGAS_CODE_EXEC_MAX_ARTIFACTS", "10")
    svc = CodeExecService()
    code = (
        "from pathlib import Path\n"
        "Path('out.txt').write_text('hello', encoding='utf-8')\n"
        "print('ok')\n"
    )
    job = await svc.create_job(user_id="u-1", language="python", code=code, timeout_sec=5)
    done = await _wait_job(svc, user_id="u-1", job_id=job.id, timeout_sec=8.0)
    assert done is not None
    assert done.status == "succeeded"
    assert "ok" in done.stdout
    paths = {str(item.get("path")) for item in (done.artifacts or [])}
    assert "out.txt" in paths
    artifact = next(item for item in done.artifacts if item.get("path") == "out.txt")
    assert str(artifact.get("mime_type", "")).startswith("text/")


@pytest.mark.asyncio
async def test_code_exec_service_docker_runtime_missing_binary(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CODE_EXEC_RUNTIME", "docker")
    monkeypatch.setattr("server.services.code_exec.shutil.which", lambda *_a, **_k: None)
    svc = CodeExecService()
    job = await svc.create_job(user_id="u-1", language="python", code="print('ok')", timeout_sec=5)
    done = await _wait_job(svc, user_id="u-1", job_id=job.id, timeout_sec=8.0)
    assert done is not None
    assert done.status == "failed"
    assert done.error == "docker_not_available"
