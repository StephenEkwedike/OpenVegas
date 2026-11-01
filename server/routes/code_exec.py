"""Sandboxed code execution routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from openvegas.flags import features
from openvegas.telemetry import emit_metric
from server.middleware.auth import get_current_user
from server.services.dependencies import get_code_exec_service

router = APIRouter()


class CodeExecCreateRequest(BaseModel):
    language: str = Field(default="python")
    code: str = Field(min_length=1, max_length=300000)
    timeout_sec: int = Field(default=10, ge=1, le=120)

    model_config = ConfigDict(extra="forbid")


def _code_exec_enabled() -> bool:
    return bool(features().get("code_exec", False))


@router.post("/code-exec/jobs")
async def create_code_exec_job(req: CodeExecCreateRequest, user: dict = Depends(get_current_user)):
    if not _code_exec_enabled():
        return JSONResponse(status_code=503, content={"error": "feature_disabled", "detail": "code_exec disabled"})
    svc = get_code_exec_service()
    try:
        job = await svc.create_job(
            user_id=str(user["user_id"]),
            language=req.language,
            code=req.code,
            timeout_sec=req.timeout_sec,
        )
    except ValueError as exc:
        code = str(exc) or "invalid_request"
        return JSONResponse(status_code=400, content={"error": code, "detail": code})
    emit_metric("code_exec_job_created_total", {"language": str(req.language).lower(), "runtime": str(job.runtime)})
    return {"job_id": job.id, "status": job.status, "runtime": job.runtime}


@router.get("/code-exec/jobs/{job_id}")
async def get_code_exec_job(job_id: str, user: dict = Depends(get_current_user)):
    if not _code_exec_enabled():
        return JSONResponse(status_code=503, content={"error": "feature_disabled", "detail": "code_exec disabled"})
    svc = get_code_exec_service()
    job = await svc.get_job(user_id=str(user["user_id"]), job_id=job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "not_found", "detail": "job not found"})
    return {
        "job_id": job.id,
        "status": job.status,
        "language": job.language,
        "timeout_sec": job.timeout_sec,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }


@router.get("/code-exec/jobs/{job_id}/result")
async def get_code_exec_job_result(job_id: str, user: dict = Depends(get_current_user)):
    if not _code_exec_enabled():
        return JSONResponse(status_code=503, content={"error": "feature_disabled", "detail": "code_exec disabled"})
    svc = get_code_exec_service()
    job = await svc.get_job(user_id=str(user["user_id"]), job_id=job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "not_found", "detail": "job not found"})
    payload = svc.serialize(job)
    emit_metric("code_exec_job_result_total", {"status": str(job.status)})
    return payload
