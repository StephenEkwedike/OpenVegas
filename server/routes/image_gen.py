"""Image generation routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from openvegas.capabilities import resolve_capability
from openvegas.flags import features
from openvegas.telemetry import emit_metric
from server.middleware.auth import get_current_user
from server.services.dependencies import get_gateway

router = APIRouter()


class ImageGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    provider: str = Field(default="openai")
    model: str = Field(default="gpt-image-1")
    size: str = Field(default="1024x1024")

    model_config = ConfigDict(extra="forbid")


def _image_gen_enabled() -> bool:
    return bool(features().get("image_gen", False))


@router.post("/images/generate")
async def generate_image(req: ImageGenerateRequest, user: dict = Depends(get_current_user)):
    if not _image_gen_enabled():
        return JSONResponse(status_code=503, content={"error": "feature_disabled", "detail": "image_gen disabled"})
    if not resolve_capability(req.provider, req.model, "image_gen", user_id=str(user["user_id"])):
        return JSONResponse(
            status_code=400,
            content={"error": "capability_unavailable:image_gen", "detail": "Image generation unavailable"},
        )
    gateway = get_gateway()
    try:
        result = await gateway.generate_image(
            account_id=f"user:{user['user_id']}",
            provider=req.provider,
            model=req.model,
            prompt=req.prompt,
            size=req.size,
        )
    except Exception as exc:
        emit_metric("image_gen_total", {"provider": req.provider, "model": req.model, "status": "error"})
        return JSONResponse(status_code=502, content={"error": "image_gen_failed", "detail": str(exc)})
    usage = result.get("usage") if isinstance(result, dict) else {}
    diagnostics = result.get("diagnostics") if isinstance(result, dict) else {}
    image_count = 0
    try:
        image_count = int((usage or {}).get("image_count", 0) or 0)
    except Exception:
        image_count = 0
    latency_ms = 0.0
    try:
        latency_ms = float((diagnostics or {}).get("latency_ms", 0) or 0)
    except Exception:
        latency_ms = 0.0

    latency_bucket = "gte_3s"
    if latency_ms < 1000:
        latency_bucket = "lt_1s"
    elif latency_ms < 3000:
        latency_bucket = "lt_3s"

    emit_metric(
        "image_gen_total",
        {"provider": req.provider, "model": req.model, "status": "ok", "size": req.size, "latency_bucket": latency_bucket},
    )
    if image_count > 0:
        emit_metric("image_gen_images_total", {"provider": req.provider, "model": req.model}, value=image_count)
    return result
