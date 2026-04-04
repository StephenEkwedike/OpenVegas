"""Inference routes — AI gateway."""

from __future__ import annotations

import os
import base64
import io
import json
import subprocess
import tempfile
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from openvegas.capabilities import resolve_capability
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.events import mk_event
from openvegas.security.policy import (
    contains_obvious_secret,
    enforce_before_tool_call,
    filter_trusted_sources,
)
from openvegas.telemetry import emit_metric, emit_run_metrics
from server.middleware.auth import get_current_user
from server.services.dependencies import (
    get_file_upload_service,
    get_fraud_engine,
    get_gateway,
    get_llm_mode_service,
    get_provider_thread_service,
)
from server.services.file_uploads import FileUploadError
from openvegas.gateway.catalog import ModelDisabled
from openvegas.gateway.inference import InferenceRequest
from openvegas.wallet.ledger import InsufficientBalance

router = APIRouter()


def _model_context_tokens(model_id: str) -> int:
    # Env override is authoritative; model-based values below are heuristics only.
    raw = os.getenv("OPENVEGAS_CONTEXT_MODEL_WINDOW_TOKENS", "").strip()
    if raw:
        try:
            return max(4000, int(raw))
        except Exception:
            pass

    normalized = (model_id or "").lower()
    if normalized.startswith("gpt-5"):
        return 128000
    if "4o" in normalized:
        return 128000
    return 32000


def _prune_history_by_char_budget(
    history_messages: list[dict[str, str]],
    *,
    model_context_tokens: int,
) -> tuple[list[dict[str, str]], int]:
    try:
        fraction = float(os.getenv("OPENVEGAS_CONTEXT_HISTORY_BUDGET_FRACTION", "0.60"))
    except Exception:
        fraction = 0.60
    if fraction <= 0:
        fraction = 0.60
    if fraction > 0.90:
        fraction = 0.90

    max_history_chars = int(max(4000, model_context_tokens) * fraction * 4)
    if max_history_chars < 1:
        return [], len(history_messages)

    messages = list(history_messages)
    total_chars = sum(len(str(msg.get("content") or "")) for msg in messages)
    dropped = 0
    while total_chars > max_history_chars and messages:
        removed = messages.pop(0)
        total_chars -= len(str(removed.get("content") or ""))
        dropped += 1
    return messages, dropped


def _web_sources_max_from_env() -> int:
    raw = str(os.getenv("OPENVEGAS_CHAT_WEB_SEARCH_SOURCES_MAX", "8")).strip()
    try:
        return max(1, min(50, int(raw)))
    except Exception:
        return 8


def _normalize_source_urls(urls: list[str], *, max_sources: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    for raw in urls:
        token = str(raw or "").strip()
        if not token:
            continue
        try:
            parts = urlsplit(token)
        except Exception:
            continue
        if parts.scheme not in {"http", "https"}:
            continue

        query_pairs = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
            if not k.lower().startswith("utm_")
        ]
        normalized = urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query_pairs, doseq=True), "")
        )
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
        if len(out) >= max_sources:
            break

    return out


def _is_real_estate_prompt(prompt: str) -> bool:
    text = str(prompt or "").strip().lower()
    if not text:
        return False
    markers = (
        "zillow",
        "realtor",
        "redfin",
        "homes.com",
        "home for sale",
        "property",
        "listing",
        "house",
        "condo",
        "beds",
        "baths",
    )
    return any(token in text for token in markers)


def _rank_and_filter_web_sources(
    *,
    prompt: str,
    sources: list[str],
    source_scores: list[dict[str, object]],
    max_sources: int,
) -> tuple[list[str], list[dict[str, object]]]:
    trust_by_url: dict[str, float] = {}
    for row in source_scores:
        url = str((row or {}).get("url") or "").strip()
        try:
            score = float((row or {}).get("score") or 0.0)
        except Exception:
            score = 0.0
        if url:
            trust_by_url[url] = score

    real_estate = _is_real_estate_prompt(prompt)
    ranking: list[dict[str, object]] = []
    for url in sources:
        token = str(url or "").strip()
        if not token:
            continue
        parts = urlsplit(token)
        host = str(parts.netloc or "").lower()
        path = str(parts.path or "").lower()
        trust = float(trust_by_url.get(token, 0.0))
        score = trust
        reasons: list[str] = []

        if path and path.count("/") >= 2:
            score += 0.05
            reasons.append("specific_path")

        if real_estate:
            listing_patterns = ("/homedetails/", "/listing/", "/property/", "/mls/")
            landing_patterns = ("/under-", "/search", "/homes-for-sale", "/map", "/austin-", "/for-sale")
            stale_patterns = ("/sold/", "off-market", "/pending/")
            is_listing = any(p in path for p in listing_patterns) or ("zpid" in path and "zillow.com" in host)
            is_landing = any(p in path for p in landing_patterns)
            if is_listing:
                score += 0.30
                reasons.append("listing_page")
            if is_landing and not is_listing:
                score -= 0.20
                reasons.append("landing_page")
            if any(p in path for p in stale_patterns):
                score -= 0.10
                reasons.append("stale_hint")

        ranking.append(
            {
                "url": token,
                "host": host,
                "score": round(score, 4),
                "trust_score": round(trust, 4),
                "reasons": reasons,
            }
        )

    ranking.sort(key=lambda row: float(row.get("score") or 0.0), reverse=True)

    listing_only = str(os.getenv("OPENVEGAS_WEB_SEARCH_LISTING_ONLY_REAL_ESTATE", "1")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if real_estate and listing_only:
        listing_rows = [r for r in ranking if "listing_page" in list(r.get("reasons") or [])]
        if listing_rows:
            ranking = listing_rows

    ranking = ranking[: max(1, int(max_sources))]
    ranked_sources = [str(r.get("url") or "") for r in ranking if str(r.get("url") or "").strip()]
    return ranked_sources, ranking


class AskRequest(BaseModel):
    prompt: str
    provider: str
    model: str
    idempotency_key: str | None = None
    thread_id: str | None = None
    conversation_mode: str | None = None
    persist_context: bool = True
    enable_tools: bool = False
    enable_web_search: bool = False
    attachments: list[str] = Field(default_factory=list)


class ModeUpdateRequest(BaseModel):
    llm_mode: str | None = None
    conversation_mode: str | None = None


def _attachment_text_max_chars() -> int:
    raw = str(os.getenv("OPENVEGAS_CHAT_ATTACHMENT_PREVIEW_MAX_CHARS", "6000")).strip()
    try:
        return max(512, min(100000, int(raw)))
    except Exception:
        return 6000


def _decode_attachment_text(content: bytes, *, mime_type: str) -> str:
    mime = str(mime_type or "").strip().lower()
    if mime == "application/pdf":
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception:
            PdfReader = None  # type: ignore
        if PdfReader is not None:
            try:
                reader = PdfReader(io.BytesIO(content))
                pages: list[str] = []
                for page in list(getattr(reader, "pages", []) or []):
                    try:
                        txt = str(page.extract_text() or "").strip()
                    except Exception:
                        txt = ""
                    if txt:
                        pages.append(txt)
                joined = "\n\n".join(pages).strip()
                if joined:
                    return joined
            except Exception:
                pass
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(prefix="ov_pdf_", suffix=".pdf", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            proc = subprocess.run(
                ["pdftotext", "-enc", "UTF-8", "-q", tmp_path, "-"],
                check=False,
                capture_output=True,
                text=True,
                timeout=8.0,
            )
            if proc.returncode == 0:
                return str(proc.stdout or "").strip()
        except Exception:
            return ""
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        return ""
    likely_text = mime.startswith("text/") or mime in {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-yaml",
        "text/markdown",
    }
    if not likely_text:
        return ""
    try:
        return content.decode("utf-8")
    except Exception:
        try:
            return content.decode("latin-1")
        except Exception:
            return ""


def _build_openai_user_parts(
    *,
    prompt: str,
    attachments: list[dict[str, Any]],
    image_input_effective: bool,
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [{"type": "input_text", "text": str(prompt or "")}]
    max_chars = _attachment_text_max_chars()

    for item in attachments:
        filename = str(item.get("filename") or "attachment")
        mime_type = str(item.get("mime_type") or "application/octet-stream")
        content = item.get("content_bytes")
        if not isinstance(content, (bytes, bytearray, memoryview)):
            continue
        payload = bytes(content)
        if mime_type.startswith("image/") and image_input_effective:
            mime_clean = str(mime_type.split(";", 1)[0] or "").strip() or "image/png"
            data_url = f"data:{mime_clean};base64,{base64.b64encode(payload).decode('ascii')}"
            parts.append(
                {
                    "type": "input_image",
                    "image_url": data_url,
                }
            )
            continue

        text = _decode_attachment_text(payload, mime_type=mime_type)
        if text:
            text = text[:max_chars]
        else:
            text = "[Binary attachment content not inline-decodable]"
        parts.append(
            {
                "type": "input_text",
                "text": f"Attachment [{filename}] (mime={mime_type}, bytes={len(payload)})\n{text}",
            }
        )
    return parts


def _build_attachment_fallback_text(attachments: list[dict[str, Any]]) -> str:
    if not attachments:
        return ""
    max_chars = _attachment_text_max_chars()
    blocks: list[str] = []
    for item in attachments:
        filename = str(item.get("filename") or "attachment")
        mime_type = str(item.get("mime_type") or "application/octet-stream")
        content = item.get("content_bytes")
        payload = bytes(content) if isinstance(content, (bytes, bytearray, memoryview)) else b""
        text = _decode_attachment_text(payload, mime_type=mime_type)
        if text:
            text = text[:max_chars]
        else:
            text = "[Binary attachment content not inline-decodable]"
        blocks.append(f"Attachment [{filename}] (mime={mime_type}, bytes={len(payload)})\n{text}")
    return "\n\n---\n\n".join(blocks)


@router.get("/mode")
async def get_mode(user: dict = Depends(get_current_user)):
    mode_svc = get_llm_mode_service()
    resolved = await mode_svc.resolve_for_user(user_id=user["user_id"])
    return resolved.as_dict()


@router.post("/mode")
async def set_mode(req: ModeUpdateRequest, user: dict = Depends(get_current_user)):
    mode_svc = get_llm_mode_service()
    resolved = await mode_svc.resolve_for_user(
        user_id=user["user_id"],
        requested_mode=req.llm_mode,
        requested_conversation_mode=req.conversation_mode,
    )
    return resolved.as_dict()


@router.post("/ask")
async def ask(
    req: AskRequest,
    user: dict = Depends(get_current_user),
):
    started = time.monotonic()
    run_id = str(uuid.uuid4())
    fraud = get_fraud_engine()
    try:
        await fraud.check_inference(user["user_id"])
    except Exception as e:
        return JSONResponse(
            status_code=429,
            content={"error": "rate_limited", "detail": str(e)},
        )

    mode_svc = get_llm_mode_service()
    mode_state = await mode_svc.resolve_for_user(user_id=user["user_id"])
    mode_payload = mode_state.as_dict() if hasattr(mode_state, "as_dict") else dict(mode_state)
    if mode_payload.get("effective_mode") == "byok":
        return JSONResponse(
            status_code=400,
            content={
                "error": APIErrorCode.BYOK_NOT_ALLOWED.value,
                "detail": "BYOK inference mode is not enabled for /inference/ask yet.",
                **mode_payload,
            },
        )

    thread_svc = get_provider_thread_service()
    context_enabled = bool(thread_svc.context_enabled())
    web_search_requested = bool(req.enable_web_search)
    attachments_requested = bool(req.attachments)
    web_search_effective = bool(
        web_search_requested and resolve_capability(
            req.provider,
            req.model,
            "web_search",
            user_id=user["user_id"],
        )
    )
    file_upload_effective = bool(
        attachments_requested and resolve_capability(
            req.provider,
            req.model,
            "file_upload",
            user_id=user["user_id"],
        )
    )
    attachments_gateway_effective = bool(req.provider == "openai" and file_upload_effective)
    image_input_effective = bool(
        attachments_requested and resolve_capability(
            req.provider,
            req.model,
            "image_input",
            user_id=user["user_id"],
        )
    )
    response_warnings: list[str] = []
    if contains_obvious_secret(req.prompt):
        return JSONResponse(
            status_code=400,
            content={
                "error": "policy.secret_like_input_blocked",
                "detail": "Input appears to contain a secret/token. Remove secrets before sending.",
                "run_id": run_id,
                "context_enabled": context_enabled,
                **mode_payload,
            },
        )
    policy = enforce_before_tool_call(
        str(user["user_id"]),
        "web_search" if web_search_requested else "inference",
        {"prompt": req.prompt, "provider": req.provider, "model": req.model},
    )
    if not policy.allow:
        emit_metric("policy_decision_total", {"capability": "web_search", "allow": "0", "code": policy.code})
        return JSONResponse(
            status_code=400,
            content={
                "error": policy.code,
                "detail": policy.reason,
                "run_id": run_id,
                "context_enabled": context_enabled,
                **mode_payload,
            },
        )
    emit_metric(
        "policy_decision_total",
        {"capability": "web_search" if web_search_requested else "inference", "allow": "1", "code": policy.code},
    )
    if web_search_requested and not web_search_effective:
        response_warnings.append("capability_unavailable:web_search")
    if attachments_requested and not attachments_gateway_effective:
        response_warnings.append("capability_unavailable:file_upload")
    try:
        thread_ctx = await thread_svc.prepare_thread(
            user_id=user["user_id"],
            provider=req.provider,
            model_id=req.model,
            thread_id=req.thread_id,
            conversation_mode=req.conversation_mode or mode_payload.get("conversation_mode"),
        )
    except ContractError as e:
        status = 503 if e.code == APIErrorCode.PROVIDER_UNAVAILABLE else 400
        return JSONResponse(
            status_code=status,
            content={"error": e.code.value, "detail": e.detail, "run_id": run_id, "context_enabled": context_enabled, **mode_payload},
        )

    gateway = get_gateway()
    history_messages: list[dict[str, str]] = []
    history_messages_loaded = 0
    history_messages_skipped = 0
    history_messages_dropped = 0
    did_prune = False
    if thread_ctx.thread_id and thread_ctx.thread_status != "disabled":
        (
            history_messages,
            history_messages_loaded,
            history_messages_skipped,
        ) = await thread_svc.get_recent_messages_with_stats(
            thread_id=thread_ctx.thread_id,
            limit=int(os.getenv("OPENVEGAS_CONTEXT_MAX_MESSAGES", "200")),
        )
        history_messages, history_messages_dropped = _prune_history_by_char_budget(
            history_messages,
            model_context_tokens=_model_context_tokens(req.model),
        )
        did_prune = history_messages_dropped > 0

    outbound_messages: list[dict[str, Any]] = list(history_messages)
    resolved_attachments: list[dict[str, Any]] = []
    if attachments_requested:
        file_svc = get_file_upload_service()
        try:
            resolved_attachments = await file_svc.resolve_uploaded_for_inference(
                user_id=str(user["user_id"]),
                file_ids=list(req.attachments),
            )
        except FileUploadError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": exc.code,
                    "detail": exc.detail,
                    "context_enabled": context_enabled,
                    **mode_payload,
                },
            )
    has_image_attachment = any(
        str(a.get("mime_type") or "").lower().startswith("image/")
        for a in resolved_attachments
    )
    if attachments_requested and has_image_attachment and not image_input_effective:
        if "capability_unavailable:image_input" not in response_warnings:
            response_warnings.append("capability_unavailable:image_input")
        return {
            "text": "Image input is unavailable for this model/provider. Remove image attachments or switch model.",
            "v_cost": "0",
            "input_tokens": 0,
            "output_tokens": 0,
            "provider_request_id": None,
            "tool_calls": [],
            "thread_id": thread_ctx.thread_id,
            "run_id": run_id,
            "thread_status": thread_ctx.thread_status,
            "context_enabled": context_enabled,
            "history_messages_loaded": history_messages_loaded,
            "history_messages_skipped": history_messages_skipped,
            "history_messages_used": len(history_messages),
            "history_messages_dropped": history_messages_dropped,
            "did_prune": did_prune,
            "web_search_requested": web_search_requested,
            "web_search_effective": web_search_effective,
            "web_search_used": False,
            "web_search_sources": [],
            "web_search_retry_without_tool": False,
            "attachments_requested": attachments_requested,
            "attachments_effective": attachments_gateway_effective,
            "attachments_used": False,
            "warning": (response_warnings[0] if response_warnings else None),
            "warnings": response_warnings,
            "diagnostics": {
                "run_id": run_id,
                "provider": req.provider,
                "model": req.model,
                "history_loaded": history_messages_loaded,
                "history_used": len(history_messages),
                "history_dropped": history_messages_dropped,
                "history_skipped": history_messages_skipped,
                "did_prune": did_prune,
                "attachments_requested": attachments_requested,
                "attachments_effective": attachments_gateway_effective,
            },
            **mode_payload,
        }

    attachments_used = False
    if attachments_requested and resolved_attachments:
        if attachments_gateway_effective:
            parts = _build_openai_user_parts(
                prompt=req.prompt,
                attachments=resolved_attachments,
                image_input_effective=image_input_effective,
            )
            outbound_messages.append({"role": "user", "content": parts})
            attachments_used = True
        else:
            fallback = _build_attachment_fallback_text(resolved_attachments)
            merged_prompt = req.prompt
            if fallback:
                merged_prompt = f"{req.prompt}\n\nAttached file context:\n\n{fallback}"
            if not (
                outbound_messages
                and outbound_messages[-1].get("role") == "user"
                and str(outbound_messages[-1].get("content") or "") == merged_prompt
            ):
                outbound_messages.append({"role": "user", "content": merged_prompt})
                attachments_used = True
    elif not (
        outbound_messages
        and outbound_messages[-1].get("role") == "user"
        and str(outbound_messages[-1].get("content") or "") == req.prompt
    ):
        outbound_messages.append({"role": "user", "content": req.prompt})

    try:
        result = await gateway.infer(InferenceRequest(
            account_id=f"user:{user['user_id']}",
            provider=req.provider,
            model=req.model,
            messages=outbound_messages,
            idempotency_key=req.idempotency_key,
            enable_tools=bool(req.enable_tools),
            enable_web_search=web_search_effective,
        ))
        await thread_svc.append_exchange(
            thread_ctx=thread_ctx,
            prompt=req.prompt,
            response_text=result.text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            persist_context=req.persist_context,
        )
        web_sources_max = _web_sources_max_from_env()
        normalized_sources = _normalize_source_urls(
            list(getattr(result, "web_search_sources", None) or []),
            max_sources=web_sources_max,
        )
        trusted_sources, source_scores = filter_trusted_sources(normalized_sources)
        ranked_sources, source_ranking = _rank_and_filter_web_sources(
            prompt=req.prompt,
            sources=trusted_sources,
            source_scores=source_scores,
            max_sources=web_sources_max,
        )
        latency_ms = max(0.0, (time.monotonic() - started) * 1000.0)
        emit_run_metrics(
            run_id=run_id,
            data={
                "provider": req.provider,
                "model": req.model,
                "turn_latency_ms": round(latency_ms, 3),
                "input_tokens": int(result.input_tokens),
                "output_tokens": int(result.output_tokens),
                "tool_calls": len(result.tool_calls or []),
                "tool_failures": 0,
                "fallbacks": 1 if response_warnings else 0,
                "cost_usd": float(getattr(result, "actual_cost_usd", 0) or 0),
            },
        )
        return {
            "text": result.text,
            "v_cost": str(result.v_cost),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "provider_request_id": result.provider_request_id,
            "tool_calls": result.tool_calls or [],
            "thread_id": thread_ctx.thread_id,
            "run_id": run_id,
            "thread_status": thread_ctx.thread_status,
            "context_enabled": context_enabled,
            "history_messages_loaded": history_messages_loaded,
            "history_messages_skipped": history_messages_skipped,
            "history_messages_used": len(history_messages),
            "history_messages_dropped": history_messages_dropped,
            "did_prune": did_prune,
            "web_search_requested": web_search_requested,
            "web_search_effective": web_search_effective,
            "web_search_used": bool(getattr(result, "web_search_used", False)),
            "web_search_sources": ranked_sources,
            "web_search_source_scores": source_scores,
            "web_search_source_ranking": source_ranking,
            "web_search_retry_without_tool": bool(getattr(result, "web_search_retry_without_tool", False)),
            "attachments_requested": attachments_requested,
            "attachments_effective": attachments_gateway_effective,
            "attachments_used": attachments_used,
            "warning": (response_warnings[0] if response_warnings else None),
            "warnings": response_warnings,
            "diagnostics": {
                "run_id": run_id,
                "provider": req.provider,
                "model": req.model,
                "history_loaded": history_messages_loaded,
                "history_used": len(history_messages),
                "history_dropped": history_messages_dropped,
                "history_skipped": history_messages_skipped,
                "did_prune": did_prune,
                "attachments_requested": attachments_requested,
                "attachments_effective": attachments_gateway_effective,
                "web_search_requested": web_search_requested,
                "web_search_effective": web_search_effective,
                "web_search_used": bool(getattr(result, "web_search_used", False)),
                "web_search_sources_ranked": len(ranked_sources),
            },
            **mode_payload,
        }
    except ContractError as e:
        status = 503 if e.code == APIErrorCode.PROVIDER_UNAVAILABLE else 400
        return JSONResponse(
            status_code=status,
            content={"error": e.code.value, "detail": e.detail, "run_id": run_id, "context_enabled": context_enabled, **mode_payload},
        )
    except ModelDisabled as e:
        return JSONResponse(
            status_code=400,
            content={"error": "model_disabled", "detail": str(e), "run_id": run_id, "context_enabled": context_enabled, **mode_payload},
        )
    except InsufficientBalance as e:
        return JSONResponse(
            status_code=400,
            content={
                "error": APIErrorCode.INSUFFICIENT_BALANCE.value,
                "detail": str(e),
                "run_id": run_id,
                "context_enabled": context_enabled,
                **mode_payload,
            },
        )


def _chunk_text(value: str, *, chunk_size: int = 600) -> list[str]:
    text = str(value or "")
    if not text:
        return []
    if chunk_size < 1:
        return [text]
    return [text[idx: idx + chunk_size] for idx in range(0, len(text), chunk_size)]


def _sse_encode(event_name: str, payload: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


@router.post("/stream")
async def ask_stream(
    req: AskRequest,
    user: dict = Depends(get_current_user),
):
    run_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())

    async def _gen():
        seq = 0

        def _emit(event_name: str, payload: dict[str, Any]) -> str:
            nonlocal seq
            seq += 1
            envelope = mk_event(
                run_id=run_id,
                turn_id=turn_id,
                sequence_no=seq,
                event_type=event_name,  # type: ignore[arg-type]
                payload=payload,
            )
            return _sse_encode(event_name, envelope.to_dict())

        yield _emit(
            "response.started",
            {"provider": req.provider, "model": req.model},
        )
        yield _emit(
            "stream_start",
            {"provider": req.provider, "model": req.model},
        )
        result = await ask(req=req, user=user)
        if isinstance(result, JSONResponse):
            body: dict[str, Any]
            try:
                body = json.loads((result.body or b"{}").decode("utf-8"))
            except Exception:
                body = {"error": "stream_bridge_error", "detail": "Failed to parse route error payload"}
            yield _emit("response.error", body)
            yield _emit(
                "stream_end",
                {
                    "status": "error",
                },
            )
            yield _emit(
                "response.completed",
                {
                    "status": "error",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "v_cost": "0",
                    "thread_id": body.get("thread_id"),
                    "thread_status": body.get("thread_status"),
                    "context_enabled": body.get("context_enabled"),
                    "warning": body.get("warning"),
                    "warnings": list(body.get("warnings") or []),
                    "web_search_requested": bool(body.get("web_search_requested", False)),
                    "web_search_effective": bool(body.get("web_search_effective", False)),
                    "web_search_used": False,
                    "web_search_sources": [],
                    "web_search_source_ranking": [],
                },
            )
            return

        tool_calls = list(result.get("tool_calls") or [])
        for tool in tool_calls:
            yield _emit("tool_start", {"tool": tool})
            yield _emit("tool_progress", {"tool": tool, "phase": "executing"})
            yield _emit("tool.call", {"tool": tool})
            yield _emit("tool_result", {"tool": tool, "status": "emitted"})
            yield _emit("tool.result", {"tool": tool, "status": "emitted"})

        for chunk in _chunk_text(str(result.get("text") or "")):
            yield _emit("response.delta", {"text": chunk})
            yield _emit("stream_delta", {"chars": len(chunk)})

        yield _emit("stream_end", {"status": "ok"})
        yield _emit(
            "response.completed",
            {
                "status": "ok",
                "text": str(result.get("text", "") or ""),
                "v_cost": str(result.get("v_cost", "0") or "0"),
                "thread_id": result.get("thread_id"),
                "thread_status": result.get("thread_status"),
                "context_enabled": result.get("context_enabled"),
                "warning": result.get("warning"),
                "input_tokens": int(result.get("input_tokens", 0) or 0),
                "output_tokens": int(result.get("output_tokens", 0) or 0),
                "total_tokens": int(result.get("total_tokens", 0) or 0),
                "web_search_requested": bool(result.get("web_search_requested", False)),
                "web_search_effective": bool(result.get("web_search_effective", False)),
                "web_search_used": bool(result.get("web_search_used", False)),
                "web_search_retry_without_tool": bool(result.get("web_search_retry_without_tool", False)),
                "web_search_sources": list(result.get("web_search_sources") or []),
                "web_search_source_scores": list(result.get("web_search_source_scores") or []),
                "web_search_source_ranking": list(result.get("web_search_source_ranking") or []),
                "warnings": list(result.get("warnings") or []),
            },
        )

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
