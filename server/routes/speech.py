"""Speech-to-text routes for uploaded audio attachments."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from openvegas.capabilities import resolve_capability
from openvegas.flags import features
from openvegas.telemetry import emit_metric
from server.middleware.auth import get_current_user
from server.services.dependencies import get_file_upload_service, get_gateway
from server.services.file_uploads import FileUploadError

router = APIRouter()


class SpeechTranscribeRequest(BaseModel):
    file_id: str
    provider: str = Field(default="openai")
    model: str = Field(default="gpt-4o-mini-transcribe")
    language: str | None = Field(default=None)
    prompt: str | None = Field(default=None, max_length=500)

    model_config = ConfigDict(extra="forbid")


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

    file_svc = get_file_upload_service()
    try:
        items = await file_svc.resolve_uploaded_for_inference(user_id=uid, file_ids=[str(req.file_id or "")])
    except FileUploadError as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "detail": exc.detail})
    if not items:
        return JSONResponse(status_code=404, content={"error": "file_not_found", "detail": "file_id not found"})

    item = items[0]
    mime_type = str(item.get("mime_type") or "").strip().lower()
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
            filename=str(item.get("filename") or ""),
            mime_type=mime_type,
            audio_bytes=bytes(item.get("content_bytes") or b""),
            language=req.language,
            prompt=req.prompt,
        )
    except Exception as exc:
        emit_metric("speech_transcribe_total", {"outcome": "failure", "reason": "gateway_error"})
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
        "file_id": str(item.get("file_id") or req.file_id),
        "filename": str(item.get("filename") or ""),
        "mime_type": mime_type,
        **(result if isinstance(result, dict) else {"text": str(result or "")}),
    }

