"""Speech-to-text routes for uploaded audio attachments."""

from __future__ import annotations

import base64
import binascii
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from openvegas.capabilities import resolve_capability
from openvegas.flags import features
from openvegas.telemetry import emit_metric
from server.middleware.auth import get_current_user
from server.services.dependencies import get_file_upload_service, get_gateway
from server.services.file_uploads import FileUploadError, FileUploadService

router = APIRouter()
logger = logging.getLogger(__name__)


class SpeechTranscribeRequest(BaseModel):
    file_id: str | None = None
    content_base64: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    provider: str = Field(default="openai")
    model: str = Field(default="gpt-4o-mini-transcribe")
    language: str | None = Field(default=None)
    prompt: str | None = Field(default=None, max_length=500)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_source(self) -> "SpeechTranscribeRequest":
        has_file = bool(str(self.file_id or "").strip())
        has_inline = bool(str(self.content_base64 or "").strip())
        if has_file == has_inline:
            raise ValueError("Provide exactly one of file_id or content_base64.")
        if has_inline and not str(self.mime_type or "").strip():
            raise ValueError("mime_type is required when content_base64 is provided.")
        return self


def _speech_enabled() -> bool:
    return bool(features().get("speech_to_text", False))


def _transcript_length_bucket(text: str) -> str:
    size = len(str(text or "").strip())
    if size <= 0:
        return "0"
    if size <= 40:
        return "1-40"
    if size <= 200:
        return "41-200"
    return "200+"

@router.post("/speech/transcribe")
async def speech_transcribe(req: SpeechTranscribeRequest, user: dict = Depends(get_current_user)):
    if not _speech_enabled():
        return JSONResponse(
            status_code=503,
            content={"error": "feature_disabled", "detail": "speech_to_text disabled"},
        )
    uid = str(user["user_id"])
    if not resolve_capability(req.provider, req.model, "speech_to_text", user_id=uid):
        return JSONResponse(
            status_code=400,
            content={"error": "capability_unavailable:speech_to_text", "detail": "Speech-to-text unavailable"},
        )

    resolved_file_id = str(req.file_id or "").strip()
    filename = ""
    mime_type = ""
    audio_bytes = b""
    if resolved_file_id:
        file_svc = get_file_upload_service()
        try:
            items = await file_svc.resolve_uploaded_for_inference(user_id=uid, file_ids=[resolved_file_id])
        except FileUploadError as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "detail": exc.detail})
        if not items:
            return JSONResponse(status_code=404, content={"error": "file_not_found", "detail": "file_id not found"})

        item = items[0]
        filename = str(item.get("filename") or "")
        mime_type = str(item.get("mime_type") or "").strip().lower()
        audio_bytes = bytes(item.get("content_bytes") or b"")
    else:
        try:
            audio_bytes = base64.b64decode(str(req.content_base64 or "").strip(), validate=True)
        except (ValueError, binascii.Error):
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_base64", "detail": "content_base64 is not valid base64"},
            )
        try:
            mime_type = FileUploadService._normalize_mime(str(req.mime_type or ""))
            FileUploadService._normalize_size(len(audio_bytes))
        except FileUploadError as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "detail": exc.detail})
        filename = FileUploadService._normalize_filename(str(req.filename or "audio.wav"))

    if not mime_type.startswith("audio/"):
        return JSONResponse(
            status_code=415,
            content={"error": "unsupported_mime_type", "detail": f"Expected audio/* attachment, got {mime_type or 'unknown'}"},
        )

    gateway = get_gateway()
    try:
        result = await gateway.transcribe_audio(
            provider=req.provider,
            model=req.model,
            filename=filename,
            mime_type=mime_type,
            audio_bytes=audio_bytes,
            language=req.language,
            prompt=req.prompt,
        )
    except Exception as exc:
        emit_metric("speech_transcribe_total", {"outcome": "failure", "reason": "gateway_error"})
        logger.exception(
            "speech_transcribe_failed user_id=%s file_id=%s provider=%s model=%s mime_type=%s filename=%s",
            uid,
            resolved_file_id,
            str(req.provider or "openai"),
            str(req.model or "gpt-4o-mini-transcribe"),
            mime_type,
            filename,
        )
        return JSONResponse(status_code=502, content={"error": "speech_transcription_failed", "detail": str(exc)})

    transcript_text = str((result.get("text") if isinstance(result, dict) else result) or "").strip()
    emit_metric(
        "speech_transcribe_text_len_total",
        {
            "bucket": _transcript_length_bucket(transcript_text),
            "provider": str(req.provider or "unknown"),
            "model": str(req.model or "unknown"),
        },
    )
    emit_metric(
        "speech_transcribe_total",
        {"outcome": "success", "provider": str(req.provider or "unknown"), "model": str(req.model or "unknown")},
    )
    return {
        "file_id": resolved_file_id,
        "filename": filename,
        "mime_type": mime_type,
        **(result if isinstance(result, dict) else {"text": str(result or "")}),
    }
