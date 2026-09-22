"""Managed OpenRouter transport: exact model, bounded request, no fallback/retry.

No customer keys, consumer logins, prompt truncation, provider-side plugins or
automatic tool execution. Capability and price limits come from our reviewed
server catalog, not from the caller or a model name heuristic.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.telemetry import emit_metric

BASE_URL = "https://openrouter.ai/api/v1"
MAX_RESPONSE_BYTES = 2_000_000
MAX_REQUEST_BYTES = 1_000_000
TIMEOUT = 60
MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
PROVIDER_CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")


class OpenRouterFailure(ContractError):
    """Keep operator diagnostics separate from the customer-facing error text."""

    def __init__(self, detail: str, *, reason: str, request_id: str | None = None):
        super().__init__(APIErrorCode.PROVIDER_UNAVAILABLE, detail)
        self.diagnostic_reason = reason
        self.provider_request_id = request_id


def _failure_category(error: Exception) -> str:
    # Only fixed categories enter telemetry, never upstream text, prompts or keys.
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, httpx.HTTPError):
        return "transport"
    if isinstance(error, json.JSONDecodeError):
        return "invalid_json"
    if isinstance(error, KeyError):
        return "missing_metering" if error.args == ("usage",) else "missing_response_field"
    if type(error) is not ValueError or len(error.args) != 1 or not isinstance(error.args[0], str):
        return "malformed_response"
    return {
        "Provider error response": "provider_error",
        "Provider generated malformed function call": "provider_malformed_function_call",
        "Provider failed completion": "provider_failed_completion",
        "Unexpected routed model": "unexpected_model",
        "Missing single completion/usage": "missing_completion_or_usage",
        "Invalid metering": "invalid_metering",
        "Input usage exceeded conservative reservation": "input_reservation_exceeded",
        "Inconsistent usage": "inconsistent_metering",
        "Reported cost exceeds approved token prices": "price_ceiling_exceeded",
        "Missing, invalid or duplicate provider tool call ID": "invalid_tool_identity",
        "Tool request violates the advertised schema": "invalid_tool_schema",
        "Tool arguments violate the advertised primitive schema": "invalid_tool_arguments",
        "Unapproved tool": "unapproved_tool",
        "Unconfirmed completion": "unconfirmed_completion",
        "Empty response": "empty_response",
        "Missing request identifier": "invalid_request_identity",
        "Response too large": "response_too_large",
        "Reflected credential": "reflected_credential",
    }.get(error.args[0], "malformed_response")


def _request_identity(body: Any) -> str | None:
    value = body.get("id") if isinstance(body, dict) else None
    if (isinstance(value, str) and PROVIDER_CALL_ID.fullmatch(value)
            and not value.lower().startswith("sk-")):
        return value
    return None


def valid_model(model: object) -> bool:
    # No automatic/latest aliases, routers, online plugins or silent transforms.
    return (
        isinstance(model, str)
        and MODEL_ID.fullmatch(model) is not None
        and not model.startswith("openrouter/")
        and not re.search(
            r"(^|[-_.])(latest|auto|router)([-_.]|$)", model.split("/", 1)[1], re.IGNORECASE
        )
    )


def price(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= 1_000_000:
            raise ValueError
        return amount
    except (ValueError, InvalidOperation):
        raise ValueError("OpenRouter pricing must be finite, nonnegative and bounded") from None


def local_tool_definition() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "call_local_tool",
            "description": "Request local workspace tool execution.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "enum": [
                            "Read",
                            "Search",
                            "Write",
                            "FindAndReplace",
                            "InsertAtEnd",
                            "Bash",
                            "List",
                        ],
                    },
                    "arguments": {
                        "type": "object",
                        "description": (
                            "Use only fields for the selected tool. Read requires filepath or path; "
                            "Search requires pattern; Write and InsertAtEnd require filepath and "
                            "content; FindAndReplace requires filepath, old_string and new_string; "
                            "Bash requires command; List accepts an optional path. "
                            "Runtime validation and approval still apply."
                        ),
                        "additionalProperties": False,
                        "properties": {
                            "filepath": {"type": "string"},
                            "path": {"type": "string"},
                            "pattern": {"type": "string"},
                            "content": {"type": "string"},
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                            "replace_all": {"type": "boolean"},
                            "write_mode": {
                                "type": "string",
                                "description": "Write mode, normally append or replace.",
                            },
                            "command": {"type": "string"},
                            "recursive": {"type": "boolean"},
                            "max_entries": {"type": "integer"},
                            "max_bytes": {"type": "integer"},
                            "result_content_max_chars": {"type": "integer"},
                            "max_files": {"type": "integer"},
                            "max_matches": {"type": "integer"},
                            "foreground_job_id": {"type": "string"},
                        },
                    },
                    "shell_mode": {"type": "string", "enum": ["read_only", "mutating"]},
                    "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 300},
                },
                "required": ["tool_name", "arguments"],
            },
        },
    }


def input_token_bound(req: Any) -> int:
    bound = len(json.dumps(req.messages, ensure_ascii=False).encode("utf-8")) + 256
    if req.enable_tools:
        bound += len(json.dumps(local_tool_definition()).encode("utf-8")) + 256
    return bound


def build_payload(req: Any, model_config: dict, capabilities: dict) -> dict:
    if not valid_model(req.model):
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION, "Choose an exact reviewed OpenRouter model ID."
        )
    if req.enable_web_search:
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION,
            "OpenRouter web plugins are not enabled; use a reviewed web-capable direct model.",
        )
    if req.enable_tools and capabilities.get("tools") is not True:
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION,
            "This OpenRouter model has no reviewed local-tool support.",
        )
    if type(req.max_tokens) is not int or not 1 <= req.max_tokens <= model_config.get(
        "max_tokens", 0
    ):
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION, "OpenRouter output budget exceeds the reviewed limit."
        )
    if not isinstance(req.messages, list) or not 1 <= len(req.messages) <= 200:
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION, "Use bounded OpenRouter message history."
        )
    for msg in req.messages:
        # The existing local agent presents observations as protocol text. This
        # route doesn't claim portability of provider-specific tool/reasoning IDs.
        if (
            not isinstance(msg, dict)
            or set(msg) != {"role", "content"}
            or msg["role"] not in {"system", "user", "assistant"}
            or not isinstance(msg["content"], str)
        ):
            raise ContractError(
                APIErrorCode.INVALID_TRANSITION,
                "OpenRouter history currently accepts text roles, not files or provider-private state.",
            )
    input_bound = input_token_bound(req)
    context_limit = capabilities.get("context_window_tokens")
    if (
        input_bound > MAX_REQUEST_BYTES
        or type(context_limit) is not int
        or input_bound + req.max_tokens > context_limit
    ):
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION,
            "OpenRouter context exceeds its reviewed bound; nothing was truncated.",
        )
    payload = {
        "model": req.model,
        "messages": req.messages,
        "max_tokens": req.max_tokens,
        "stream": False,
        "transforms": [],
        "provider": {
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
            "max_price": {
                "prompt": float(price(model_config["cost_input_per_1m"])),
                "completion": float(price(model_config["cost_output_per_1m"])),
                "request": 0,
            },
        },
    }
    if req.enable_tools:
        payload.update(tools=[local_tool_definition()], tool_choice="auto")
    # Let the provider budget reasoning within max_tokens, but never request or
    # persist hidden thoughts. The output parser does not forward those fields.
    return payload


@asynccontextmanager
async def _client(client):
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=False, trust_env=False
        ) as owned:
            yield owned


def parse_response(body: Any, req: Any, model_config: dict, parse_tool) -> dict:
    if not isinstance(body, dict) or body.get("error"):
        raise ValueError("Provider error response")
    # OpenRouter may return a dated canonical slug for an operator-approved ID.
    # Accept only explicit server-reviewed aliases, never a caller-provided list.
    aliases = model_config.get("response_model_ids", [])
    if (
        not isinstance(aliases, list)
        or len(aliases) > 2
        or any(not valid_model(m) for m in aliases)
    ):
        raise ValueError("Invalid reviewed response model IDs")
    accepted_models = {req.model, *aliases}
    if body.get("model") not in accepted_models:
        raise ValueError("Unexpected routed model")
    choices = body["choices"]
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError("Missing single completion/usage")
    choice = choices[0]
    # Some providers return HTTP200 with a failed generation and omit usage.
    # Preserve that known failure category before inspecting metering fields.
    if choice.get("native_finish_reason") == "MALFORMED_FUNCTION_CALL":
        raise ValueError("Provider generated malformed function call")
    if choice.get("finish_reason") == "error":
        raise ValueError("Provider failed completion")
    usage = body["usage"]
    if not isinstance(usage, dict):
        raise TypeError("Missing single completion/usage")
    message = choice["message"]
    if message.get("role") != "assistant":
        raise ValueError("Unexpected response role")
    text = message.get("content") or ""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("Unsupported response content")
    counts = usage["prompt_tokens"], usage["completion_tokens"]
    if any(type(n) is not int or n < 0 for n in counts) or counts[1] > req.max_tokens:
        raise ValueError("Invalid metering")
    input_bound = input_token_bound(req)
    if counts[0] > input_bound:
        raise ValueError("Input usage exceeded conservative reservation")
    if "total_tokens" in usage and usage["total_tokens"] != sum(counts):
        raise ValueError("Inconsistent usage")
    cost = price(usage["cost"])
    maximum = (
        counts[0] * price(model_config["cost_input_per_1m"])
        + counts[1] * price(model_config["cost_output_per_1m"])
    ) / 1_000_000
    if cost > maximum + Decimal("0.000001"):
        raise ValueError("Reported cost exceeds approved token prices")
    native_calls = message.get("tool_calls") or []
    if (
        not isinstance(native_calls, list)
        or len(native_calls) > 16
        or (native_calls and not req.enable_tools)
    ):
        raise ValueError("Unrequested or excess tool calls")
    call_ids = set()
    for call in native_calls:
        call_id = call.get("id") if isinstance(call, dict) else None
        if (
            not isinstance(call_id, str)
            or not PROVIDER_CALL_ID.fullmatch(call_id)
            or call_id.lower().startswith("sk-")
            or call_id in call_ids
        ):
            raise ValueError("Missing, invalid or duplicate provider tool call ID")
        call_ids.add(call_id)
    argument_properties = local_tool_definition()["function"]["parameters"]["properties"][
        "arguments"
    ]["properties"]
    primitive_types = {"string": str, "boolean": bool, "integer": int}
    tools = []
    for call in native_calls:
        function = call["function"]
        if call.get("type") != "function" or function.get("name") != "call_local_tool":
            raise ValueError("Unapproved tool")
        arguments = function.get("arguments")
        if not isinstance(arguments, str) or len(arguments.encode("utf-8")) > 32_000:
            raise ValueError("Invalid tool arguments")
        tool = json.loads(arguments)
        if (
            not isinstance(tool, dict)
            or set(tool) - {"tool_name", "arguments", "shell_mode", "timeout_sec"}
            or not isinstance(tool.get("tool_name"), str)
            or tool["tool_name"]
            not in {"Read", "Search", "Write", "FindAndReplace", "InsertAtEnd", "Bash", "List"}
            or not isinstance(tool.get("arguments"), dict)
            or tool.get("shell_mode", "read_only") not in {"read_only", "mutating"}
            or type(tool.get("timeout_sec", 30)) is not int
            or not 1 <= tool.get("timeout_sec", 30) <= 300
        ):
            raise ValueError("Tool request violates the advertised schema")
        for name, value in tool["arguments"].items():
            schema = argument_properties.get(name)
            if schema is None or type(value) is not primitive_types[schema["type"]]:
                raise ValueError("Tool arguments violate the advertised primitive schema")
        parsed = parse_tool(function_name="call_local_tool", raw_arguments=arguments)
        if not parsed:
            raise ValueError("Invalid local tool request")
        # The legacy parser treats {} as absent. This schema requires arguments,
        # including a valid empty object; never replace it with the outer envelope.
        parsed["arguments"] = tool["arguments"]
        parsed["provider_call_id"] = call["id"]
        tools.append(parsed)
    finish = choice.get("finish_reason")
    if finish not in {"stop", "length", "tool_calls"} or bool(tools) != (finish == "tool_calls"):
        raise ValueError("Unconfirmed completion")
    if not text.strip() and not tools:
        raise ValueError("Empty response")
    request_id = _request_identity(body)
    if request_id is None:
        raise ValueError("Missing request identifier")
    return {
        "text": text,
        "input_tokens": counts[0],
        "output_tokens": counts[1],
        "provider_request_id": request_id,
        "actual_cost_usd": cost,
        "tool_calls": tools or None,
        "completion_status": "complete" if finish == "stop" else "incomplete",
    }


async def complete(
    req, api_key: str, *, model_config: dict, capabilities: dict, parse_tool, client=None
) -> dict:
    payload = build_payload(req, model_config, capabilities)
    if not api_key or not isinstance(api_key, str):
        raise ContractError(
            APIErrorCode.PROVIDER_UNAVAILABLE, "Managed OpenRouter credential unavailable."
        )
    request_id = None
    try:
        async with asyncio.timeout(TIMEOUT), _client(client) as http:
            async with http.stream(
                "POST",
                BASE_URL + "/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "HTTP-Referer": "https://openvegas.ai",
                    "X-OpenRouter-Title": "OpenVegas",
                },
                timeout=TIMEOUT,
                follow_redirects=False,
                auth=None,
            ) as response:
                if response.status_code != 200:
                    reason = {
                        401: "credential_rejected", 402: "supplier_balance_exhausted",
                        429: "rate_limited",
                    }.get(response.status_code, "http_rejected")
                    emit_metric("openrouter_failure_total", {"reason": reason})
                    messages = {
                        401: "Managed OpenRouter credential rejected; operator action required.",
                        402: "OpenRouter balance exhausted; no automatic top-up was made.",
                        429: "OpenRouter rate limit reached; no retry was made.",
                    }
                    raise OpenRouterFailure(
                        messages.get(
                            response.status_code,
                            "OpenRouter unavailable or request rejected; no retry was made.",
                        ),
                        reason=reason,
                    )
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError("Response too large")
                    raw.extend(chunk)
            if api_key.encode("utf-8") in raw:
                raise ValueError("Reflected credential")
            body = json.loads(raw)
            candidate_id = _request_identity(body)
            if candidate_id and api_key in candidate_id:
                raise ValueError("Reflected credential")
            request_id = candidate_id
            result = parse_response(body, req, model_config, parse_tool)
            if api_key in json.dumps(result, default=str):
                raise ValueError("Reflected credential")
            return result
    except asyncio.CancelledError:
        emit_metric("openrouter_failure_total", {"reason": "cancelled"})
        raise
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
    ) as error:
        reason = _failure_category(error)
        emit_metric("openrouter_failure_total", {"reason": reason})
        detail = (
            "The selected model returned an invalid tool call. No tool from this response "
            "was executed and no retry was made. Choose another model; the accepted "
            "request may need billing reconciliation."
            if reason == "provider_malformed_function_call" else
            "OpenRouter returned an unsupported response or failed. No retry was made; "
            "an accepted request may need billing reconciliation."
        )
        raise OpenRouterFailure(
            detail,
            reason=reason,
            request_id=request_id,
        ) from None
