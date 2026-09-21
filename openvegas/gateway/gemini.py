"""Bounded Gemini REST text adapter; no SDK globals, retries or tool replay.

Contract: https://ai.google.dev/api/generate-content
Signatures: https://ai.google.dev/gemini-api/docs/thought-signatures
Only plain text parts are portable here, not full Gemini agent state.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from typing import Any

import httpx

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.conversation import (
    MAX_HISTORY_BYTES,
    MAX_MESSAGES,
    MAX_TEXT_BYTES,
    CanonicalConversation,
    ContinuityError,
    validate_text,
)

REQUEST_TIMEOUT_SECONDS = 60.0
MAX_RESPONSE_BYTES = 1_000_000
_MODEL = re.compile(r"(?:models/)?[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")


def build_payload(req: Any) -> dict:
    """Validate without credentials or I/O; never merge or reorder history turns."""
    if req.enable_tools or req.enable_web_search:
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION,
            "Gemini continuity is text only; disable tools and web search.",
        )
    if not isinstance(req.model, str) or not _MODEL.fullmatch(req.model):
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION, "Invalid Gemini model identifier."
        )
    if type(req.max_tokens) is not int or req.max_tokens <= 0:
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION, "max_tokens must be positive."
        )
    if not isinstance(req.messages, list) or not 1 <= len(req.messages) <= MAX_MESSAGES:
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION, "Use bounded Gemini text history."
        )
    payload = {
        "contents": [],
        "generationConfig": {
            "maxOutputTokens": req.max_tokens,
            "candidateCount": 1,
        },
    }
    size = 0
    expected = "user"
    try:
        for index, message in enumerate(req.messages):
            if not isinstance(message, dict) or set(message) != {"role", "content"}:
                raise ContinuityError(
                    "Gemini rejects tools, attachments, signatures and non-text message fields."
                )
            role, content = message["role"], validate_text(message["content"])
            size += len(content.encode("utf-8")) + 32
            if size > MAX_HISTORY_BYTES:
                raise ContinuityError("Gemini history exceeds the transfer byte bound.")
            if role == "system" and index == 0 and not req.strict_continuity:
                payload["systemInstruction"] = {"parts": [{"text": content}]}
                continue
            if role != expected:
                raise ContinuityError(
                    "Gemini requires completed user/assistant pairs followed by a user prompt."
                )
            payload["contents"].append(
                {
                    "role": "model" if role == "assistant" else "user",
                    "parts": [{"text": content}],
                }
            )
            expected = "assistant" if role == "user" else "user"
        if expected != "assistant":
            raise ContinuityError(
                "Gemini requires a final user prompt; nothing was flattened."
            )
        if req.strict_continuity:
            CanonicalConversation.from_messages(req.messages[:-1])
    except ContinuityError as exc:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, str(exc)) from None
    return payload


@asynccontextmanager
async def _client(client: httpx.AsyncClient | None):
    if client is not None:
        # Injected clients must use a trusted, non-retrying transport.
        yield client
    else:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        ) as owned:
            yield owned


def _parse_response(body: Any) -> dict:
    candidates, usage = body["candidates"], body["usageMetadata"]
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise ValueError("unexpected candidates")
    if body.get("promptFeedback", {}).get("blockReason"):
        raise ValueError("blocked prompt")
    candidate = candidates[0]
    content = candidate["content"]
    parts = content["parts"]
    if (
        set(content) != {"role", "parts"}
        or content.get("role") != "model"
        or not isinstance(parts, list)
        or not parts
        or candidate.get("groundingMetadata")
        or candidate.get("urlContextMetadata")
    ):
        raise ValueError("unsupported content")
    # Presence, not truthiness: never silently discard signatures or thought fields.
    if any(
        not isinstance(p, dict) or set(p) != {"text"} or not isinstance(p["text"], str)
        for p in parts
    ):
        raise ContractError(
            APIErrorCode.PROVIDER_UNAVAILABLE,
            "Gemini returned unsupported tools, attachments, thoughts or signatures; "
            "continuity cannot retain this response. No retry was made.",
        )
    text = "".join(p["text"] for p in parts)
    if not text.strip() or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("empty or oversized response")
    finish = candidate.get("finishReason")
    if finish not in {"STOP", "MAX_TOKENS"}:
        raise ValueError("unconfirmed finish")
    counts = [
        usage["promptTokenCount"],
        usage["candidatesTokenCount"],
        usage.get("thoughtsTokenCount", 0),
        usage.get("toolUsePromptTokenCount", 0),
    ]
    if any(type(n) is not int or n < 0 for n in counts) or counts[3]:
        raise ValueError("invalid metering or unexpected tool use")
    total = usage.get("totalTokenCount")
    if total is not None and (type(total) is not int or total != sum(counts)):
        raise ValueError("inconsistent metering")
    request_id = body.get("responseId")
    if request_id is not None and (
        not isinstance(request_id, str) or len(request_id) > 256
    ):
        raise ValueError("invalid response identifier")
    return {
        "text": text,
        "completion_status": "complete" if finish == "STOP" else "incomplete",
        "input_tokens": counts[0],
        # Thinking tokens are output usage, not free or replayable history.
        "output_tokens": counts[1] + counts[2],
        "provider_request_id": request_id,
    }


async def complete(
    req: Any, api_key: str, client: httpx.AsyncClient | None = None
) -> dict:
    payload = build_payload(req)
    if not isinstance(api_key, str) or not api_key.strip():
        raise ContractError(
            APIErrorCode.PROVIDER_UNAVAILABLE, "Managed Gemini credential missing."
        )
    model = req.model.removeprefix("models/")
    try:
        # HTTPX timeouts bound individual operations; this also bounds total elapsed time.
        async with (
            asyncio.timeout(REQUEST_TIMEOUT_SECONDS),
            _client(client) as http,
            http.stream(
                "POST",
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": api_key},
                auth=None,
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as response,
        ):
            if response.status_code in {400, 404, 422}:
                raise ContractError(
                    APIErrorCode.INVALID_TRANSITION,
                    "Gemini rejected the model or request; operator review required. No retry was made.",
                )
            if not 200 <= response.status_code < 300:
                raise ContractError(
                    APIErrorCode.PROVIDER_UNAVAILABLE,
                    "Gemini unavailable; no automatic retry was made.",
                )
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError("response too large")
                raw.extend(chunk)
        result = _parse_response(json.loads(raw))
        if result["output_tokens"] > req.max_tokens:
            raise ValueError("provider exceeded reserved output budget")
        return result
    except ContractError:
        raise
    except (
        httpx.HTTPError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        RecursionError,
    ):
        raise ContractError(
            APIErrorCode.PROVIDER_UNAVAILABLE,
            "Gemini request failed or returned unsupported content/missing usage. "
            "The outcome may be billed; no retry was made.",
        ) from None
