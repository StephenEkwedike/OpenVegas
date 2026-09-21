"""Bounded text-only Mistral Chat Completions adapter using existing httpx.

API contract: https://docs.mistral.ai/api/endpoint/chat
Model discovery: https://docs.mistral.ai/api/endpoint/models
No retries, account rotation, automatic discovery, or tool execution.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import httpx

from openvegas.contracts.errors import APIErrorCode, ContractError


@asynccontextmanager
async def _client(client: httpx.AsyncClient | None):
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False) as owned:
            yield owned


def validate_text_request(req: Any) -> None:
    if req.enable_tools or req.enable_web_search:
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION,
            "Mistral supports text only here; disable tools and web search.",
        )
    if type(req.max_tokens) is not int or req.max_tokens <= 0:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "max_tokens must be positive.")
    if not req.messages or len(req.messages) > 200:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Use between 1 and 200 text messages.")
    for message in req.messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in {"system", "user", "assistant"}
            or not isinstance(message["content"], str)
        ):
            raise ContractError(
                APIErrorCode.INVALID_TRANSITION,
                "Mistral does not support images or tool history in this adapter.",
            )
    if sum(len(m["content"].encode("utf-8")) for m in req.messages) > 1_000_000:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Mistral text request is too large.")


async def complete(req: Any, api_key: str, client: httpx.AsyncClient | None = None) -> dict:
    validate_text_request(req)
    try:
        async with _client(client) as http:
            response = await http.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": req.model,
                    "messages": req.messages,
                    "max_tokens": req.max_tokens,
                    "stream": False,
                },
                timeout=60,
                follow_redirects=False,
            )
        if response.status_code in {400, 404, 422}:
            raise ContractError(
                APIErrorCode.INVALID_TRANSITION,
                "Mistral rejected the model or request; ask the operator to review the catalog.",
            )
        if not 200 <= response.status_code < 300:
            raise ContractError(
                APIErrorCode.PROVIDER_UNAVAILABLE,
                "Mistral unavailable; no automatic retry was made.",
            )
        body = response.json()
        choices, usage = body["choices"], body["usage"]
        if len(choices) != 1:
            raise ValueError("unexpected choices")
        choice = choices[0]
        message = choice["message"]
        if (
            choice.get("finish_reason") not in {"stop", "length"}
            or message.get("tool_calls")
            or not isinstance(message["content"], str)
        ):
            raise ValueError("unsupported completion")
        counts = [usage["prompt_tokens"], usage["completion_tokens"]]
        if any(type(n) is not int or n < 0 for n in counts):
            raise ValueError("missing or invalid metering")
        return {
            "text": message["content"],
            "completion_status": "complete" if choice["finish_reason"] == "stop" else "incomplete",
            "input_tokens": counts[0],
            "output_tokens": counts[1],
            "provider_request_id": body.get("id"),
        }
    except ContractError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        # Do not echo provider bodies, headers, prompts, or credentials into errors.
        raise ContractError(
            APIErrorCode.PROVIDER_UNAVAILABLE,
            "Mistral request failed or returned an unsupported/missing usage payload.",
        ) from None


def discovery_candidates(payload: Any, *, limit: int = 500) -> list[dict]:
    """Parse an operator-fetched /v1/models snapshot. Never creates prices/catalog rows."""
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or len(rows) > min(max(limit, 1), 500):
        raise ValueError("Invalid or oversized Mistral model snapshot")
    candidates = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError("Invalid Mistral model card")  # noqa: TRY004 - Preserve discovery validation's ValueError contract.
        caps = row.get("capabilities", {})
        if (
            not isinstance(caps, dict)
            or caps.get("completion_chat") is not True
            or row.get("archived")
        ):
            continue
        candidates.append(
            {
                "provider": "mistral",
                "model_id": row["id"],
                "context_window_tokens": row.get("max_context_length"),
                "selectable": False,
                "review_required": True,
                "pricing_source": "operator_catalog_required",
            }
        )
    return candidates
