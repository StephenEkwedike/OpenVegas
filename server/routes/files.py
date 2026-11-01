"""Chat file upload routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from openvegas.telemetry import emit_metric
from server.middleware.auth import get_current_user
from server.services.dependencies import current_flags, get_file_upload_service
from server.services.file_uploads import FileUploadError

router = APIRouter()


class UploadInitRequest(BaseModel):
    filename: str
    size_bytes: int = Field(..., ge=1)
    mime_type: str
    sha256: str

    model_config = ConfigDict(extra="forbid")


class UploadCompleteRequest(BaseModel):
    upload_id: str
    content_base64: str

    model_config = ConfigDict(extra="forbid")


class FileSearchRequest(BaseModel):
    query: str
    limit: int = Field(default=5, ge=1, le=20)

    model_config = ConfigDict(extra="forbid")


def _files_feature_enabled() -> bool:
    return bool(getattr(current_flags(), "files_enabled", False))


@router.post("/files/upload/init")
async def upload_init(req: UploadInitRequest, user: dict = Depends(get_current_user)):
    if not _files_feature_enabled():
        emit_metric("file_upload_request_total", {"endpoint": "init", "outcome": "failure", "reason": "feature_disabled"})
        return JSONResponse(status_code=503, content={"error": "feature_disabled", "detail": "files uploads disabled"})
    svc = get_file_upload_service()
    try:
        payload = await svc.upload_init(
            user_id=str(user["user_id"]),
            filename=req.filename,
            size_bytes=req.size_bytes,
            mime_type=req.mime_type,
            sha256_hex=req.sha256,
        )
        emit_metric("file_upload_request_total", {"endpoint": "init", "outcome": "success"})
        return payload
    except FileUploadError as exc:
        emit_metric("file_upload_request_total", {"endpoint": "init", "outcome": "failure", "reason": str(exc.code or "unknown")})
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "detail": exc.detail})


@router.post("/files/upload/complete")
async def upload_complete(req: UploadCompleteRequest, user: dict = Depends(get_current_user)):
    if not _files_feature_enabled():
        emit_metric("file_upload_request_total", {"endpoint": "complete", "outcome": "failure", "reason": "feature_disabled"})
        return JSONResponse(status_code=503, content={"error": "feature_disabled", "detail": "files uploads disabled"})
    svc = get_file_upload_service()
    try:
        payload = await svc.upload_complete(
            user_id=str(user["user_id"]),
            upload_id=req.upload_id,
            content_base64=req.content_base64,
        )
        emit_metric("file_upload_request_total", {"endpoint": "complete", "outcome": "success"})
        return payload
    except FileUploadError as exc:
        emit_metric(
            "file_upload_request_total",
            {"endpoint": "complete", "outcome": "failure", "reason": str(exc.code or "unknown")},
        )
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "detail": exc.detail})


@router.post("/files/search")
async def search_files(req: FileSearchRequest, user: dict = Depends(get_current_user)):
    if not _files_feature_enabled():
        return JSONResponse(status_code=503, content={"error": "feature_disabled", "detail": "files uploads disabled"})
    svc = get_file_upload_service()
    hits = await svc.search_uploaded_text(
        user_id=str(user["user_id"]),
        query=req.query,
        limit=req.limit,
    )
    return {"hits": hits}
