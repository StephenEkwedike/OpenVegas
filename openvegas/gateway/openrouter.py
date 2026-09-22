"""Managed OpenRouter transport: exact model, bounded request, no fallback/retry.

No customer keys, consumer logins, prompt truncation or automatic local tools.
The separately reviewed bounded web path permits one Exa server-tool step.
Capability and price limits come from our server catalog, not caller settings.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.reasoning import reasoning_payload
from openvegas.gateway.openrouter_web import PreparedWebSearch, WebValidationError, prepare_server_review
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


def local_tool_definitions(model: str) -> list[dict]:
    if not model.startswith("google/"):
        return [local_tool_definition()]
    # Gemini emits native functions more reliably with one flat, required schema
    # per operation than with a generic dispatcher and conditional nested fields.
    fields = local_tool_definition()["function"]["parameters"]["properties"]["arguments"]["properties"]
    specifications = (
        ("Read", "Read a local workspace file.", ("path",), ("max_bytes", "result_content_max_chars")),
        ("Search", "Search text in local workspace files.", ("pattern",), ("path", "max_files", "max_matches")),
        ("Write", "Write a local workspace file; approval is required.", ("filepath", "content"), ("write_mode",)),
        ("FindAndReplace", "Replace exact text in a local file; approval is required.", ("filepath", "old_string", "new_string"), ("replace_all",)),
        ("InsertAtEnd", "Append text to a local file; approval is required.", ("filepath", "content"), ()),
        ("Bash", "Run a local shell command through the permission system.", ("command",), ()),
        ("List", "List local workspace directory entries.", (), ("path", "recursive", "max_entries")),
    )
    definitions = []
    for name, description, required, optional in specifications:
        properties = {key: dict(fields[key]) for key in (*required, *optional)}
        properties["timeout_sec"] = {"type": "integer", "minimum": 1, "maximum": 300}
        if name == "Bash":
            properties["shell_mode"] = {"type": "string", "enum": ["read_only", "mutating"]}
        definitions.append({"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(required), "additionalProperties": False},
        }})
    return definitions


def _flat_tool(function: dict, model: str) -> dict:
    definitions = {d["function"]["name"]: d["function"] for d in local_tool_definitions(model)}
    definition = definitions.get(function.get("name"))
    if not definition or definition["name"] == "call_local_tool":
        raise ValueError("Unapproved tool")
    raw = function.get("arguments")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32_000:
        raise ValueError("Invalid tool arguments")
    args = json.loads(raw)
    schema = definition["parameters"]
    if (not isinstance(args, dict) or set(args) - schema["properties"].keys()
            or not set(schema["required"]) <= args.keys()):
        raise ValueError("Tool request violates the advertised schema")
    types = {"string": str, "integer": int, "boolean": bool}
    for name, value in args.items():
        field = schema["properties"][name]
        if (type(value) is not types[field["type"]]
                or ("enum" in field and value not in field["enum"])
                or ("minimum" in field and value < field["minimum"])
                or ("maximum" in field and value > field["maximum"])):
            raise ValueError("Tool arguments violate the advertised primitive schema")
    name = definition["name"]
    mode = args.pop("shell_mode", "mutating" if name in {"Write", "FindAndReplace", "InsertAtEnd"} else "read_only")
    timeout = args.pop("timeout_sec", 30)
    return {"tool_name": name, "arguments": args, "shell_mode": mode, "timeout_sec": timeout}


def input_token_bound(req: Any) -> int:
    attachments = getattr(req, "_managed_attachment_context", None)
    if attachments is not None:
        return attachments.token_bound(req, local_tool_definitions(req.model) if req.enable_tools else None)
    bound = len(json.dumps(req.messages, ensure_ascii=False).encode("utf-8")) + 256
    if req.enable_tools:
        bound += len(json.dumps(local_tool_definitions(req.model)).encode("utf-8")) + 256
    return bound


def _web_binding(req: Any, model_config: dict) -> str:
    fields = {name: getattr(req, name, None) for name in (
        "account_id", "provider", "model", "messages", "max_tokens", "idempotency_key",
        "enable_tools", "enable_web_search", "strict_continuity", "reasoning_effort",
    )}
    fields["catalog"] = {name: model_config.get(name) for name in (
        "provider", "model_id", "enabled", "max_tokens", "cost_input_per_1m",
        "cost_output_per_1m", "v_price_input_per_1m", "v_price_output_per_1m",
    )}
    attachment = getattr(req, "_managed_attachment_context", None)
    fields["attachment_context"] = id(attachment) if attachment is not None else None
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class DispatchMetering:
    input_tokens: int
    binding: str


@dataclass(frozen=True)
class ManagedWebContext:
    prepared: PreparedWebSearch
    binding: str
    payload_json: str

    def payload(self, req: Any, model_config: dict, *, dispatch: bool = True) -> dict:
        if _web_binding(req, model_config) != self.binding:
            raise WebValidationError("web_request_changed_after_preflight")
        if dispatch:
            self.prepared.snapshot.preflight().require_ready()
        payload = json.loads(self.payload_json)
        if dispatch and getattr(req, "_managed_attachment_context", None) is not None:
            _web_attachment_options(req, model_config, self.prepared)
            req._managed_attachment_context.token_bound(req, payload["tools"])
        return payload


def _web_attachment_options(req: Any, model_config: dict, prepared: PreparedWebSearch) -> dict:
    context = req._managed_attachment_context
    # Accept only the server's ownership-checked immutable attachment context.
    from server.services.openrouter_attachment_request import AttachmentRequestContext

    if not isinstance(context, AttachmentRequestContext):
        raise WebValidationError("invalid_private_attachment_context")
    options = context.validate(req, model_config)
    attachment = context.prepared.review
    web = prepared.snapshot
    if (attachment.provider != web.execution.provider_slug
            or attachment.context_window_tokens != web.context_window_tokens
            or attachment.input_price_per_1m != web.prices.supplier_input_usd_per_million
            or attachment.output_price_per_1m != web.prices.supplier_output_usd_per_million
            or req.max_tokens > attachment.max_output_tokens
            or options.get("provider", {}).get("only") != [web.execution.provider_slug]):
        raise WebValidationError("web_attachment_review_mismatch")
    if options.get("plugins") not in ([], [{"id": "file-parser", "pdf": {"engine": "native"}}]):
        raise WebValidationError("web_attachment_parser_unbounded")
    return options


def prepare_web_context(req: Any, model_config: dict, capabilities: dict) -> ManagedWebContext:
    """Route/gateway preflight. Reads server review itself; accepts no caller review."""
    from openvegas.gateway.providers import get_model_review

    if req.provider != "openrouter" or req.enable_web_search is not True:
        raise WebValidationError("invalid_web_request_scope")
    if capabilities.get("web_search") is not True:
        raise WebValidationError("web_capability_unreviewed")
    if req.enable_tools and capabilities.get("tools") is not True:
        raise WebValidationError("web_local_tools_unreviewed")
    if (not isinstance(req.account_id, str) or not req.account_id.startswith("user:")
            or not isinstance(req.idempotency_key, str) or not 1 <= len(req.idempotency_key) <= 200):
        raise WebValidationError("web_requires_user_idempotency_key")
    prepared = prepare_server_review(
        req.model, req.max_tokens, get_model_review("openrouter", req.model), model_config,
    )
    if capabilities.get("context_window_tokens") != prepared.snapshot.context_window_tokens:
        raise WebValidationError("web_context_review_mismatch")
    attachment = getattr(req, "_managed_attachment_context", None)
    if attachment is None:
        payload = prepared.payload(req.messages)
    else:
        options = _web_attachment_options(req, model_config, prepared)
        # Only the immutable configuration is taken from this text-only builder;
        # the actual owned media messages are validated below and never truncated.
        payload = prepared.payload([{"role": "user", "content": ""}])
        payload["messages"] = json.loads(json.dumps(req.messages))
        payload["provider"]["max_price"]["image"] = 0
        if options["plugins"]:
            payload["plugins"] = [
                p for p in payload["plugins"] if p["id"] != "file-parser"
            ] + options["plugins"]
    if req.enable_tools:
        payload["tools"].extend(local_tool_definitions(req.model))
    payload.update(reasoning_payload(getattr(req, "reasoning_effort", None), capabilities))
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    extra_fields = {k: v for k, v in payload.items() if k not in {"messages", "tools"}}
    input_bound = (attachment.token_bound(req, payload["tools"])
                   + len(json.dumps(extra_fields, ensure_ascii=False).encode("utf-8")) + 256
                   if attachment is not None else len(encoded.encode("utf-8")) + 256)
    # Include tool/schema overhead without claiming an exact provider tokenizer.
    if (len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES
            or input_bound + req.max_tokens > prepared.snapshot.context_window_tokens):
        raise WebValidationError("web_initial_context_exceeded")
    context = ManagedWebContext(prepared, _web_binding(req, model_config), encoded)
    req._managed_web_context = context
    return context


def build_payload(req: Any, model_config: dict, capabilities: dict) -> dict:
    if not valid_model(req.model):
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION, "Choose an exact reviewed OpenRouter model ID."
        )
    if req.enable_web_search:
        try:
            context = getattr(req, "_managed_web_context", None)
            if context is None:
                context = prepare_web_context(req, model_config, capabilities)
            if not isinstance(context, ManagedWebContext):
                raise WebValidationError("invalid_private_web_context")
            return context.payload(req, model_config)
        except WebValidationError as error:
            raise ContractError(
                APIErrorCode.INVALID_TRANSITION,
                "Bounded OpenRouter web preflight blocked: " + error.code,
            ) from None
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
    attachment_context = getattr(req, "_managed_attachment_context", None)
    attachment_options = attachment_context.validate(req, model_config) if attachment_context is not None else {}
    for msg in req.messages:
        # The existing local agent presents observations as protocol text. This
        # route doesn't claim portability of provider-specific tool/reasoning IDs.
        if (
            not isinstance(msg, dict)
            or set(msg) != {"role", "content"}
            or msg["role"] not in {"system", "user", "assistant"}
            or not (isinstance(msg["content"], str) or (attachment_context is not None
                    and msg["role"] == "user" and isinstance(msg["content"], list)))
        ):
            raise ContractError(
                APIErrorCode.INVALID_TRANSITION,
                "OpenRouter accepts text or server-authorized attachments, not caller-supplied media or private state.",
            )
    input_bound = input_token_bound(req)
    context_limit = capabilities.get("context_window_tokens")
    if (
        len(json.dumps(req.messages, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES
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
        payload.update(tools=local_tool_definitions(req.model), tool_choice="auto")
    payload.update(attachment_options)
    payload.update(reasoning_payload(getattr(req, "reasoning_effort", None), capabilities))
    req._managed_openrouter_dispatch = DispatchMetering(input_bound, _web_binding(req, model_config))
    # Output metering includes reasoning. Do not forward private thought fields.
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
    web_fields = {}
    if req.enable_web_search:
        context = getattr(req, "_managed_web_context", None)
        if not isinstance(context, ManagedWebContext):
            raise ValueError("Missing private web context")
        context.payload(req, model_config, dispatch=False)
        # Local calls are validated below and handed back, never executed here.
        # Only server search use participates in the supplier/search receipt.
        receipt = context.prepared.parse_receipt(usage, {**message, "content": text, "tool_calls": []})
        counts = receipt.input_tokens, receipt.output_tokens
        cost = receipt.actual_cost_usd
        web_fields = {
            "web_search_used": receipt.web_search_used,
            "web_search_requests": receipt.web_search_requests,
            "web_search_sources": list(receipt.sources),
            "web_search_cost_v": receipt.web_search_cost_v,
            "_managed_web_receipt": receipt,
        }
    else:
        counts = usage["prompt_tokens"], usage["completion_tokens"]
        if any(type(n) is not int or n < 0 for n in counts) or counts[1] > req.max_tokens:
            raise ValueError("Invalid metering")
        dispatch = getattr(req, "_managed_openrouter_dispatch", None)
        if dispatch is not None:
            if not isinstance(dispatch, DispatchMetering) or dispatch.binding != _web_binding(req, model_config):
                raise ValueError("Request changed after dispatch")
            input_bound = dispatch.input_tokens
        elif getattr(req, "_managed_attachment_context", None) is not None:
            raise ValueError("Attachment response requires dispatch metering snapshot")
        else:
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
    native_calls = message.get("tool_calls")
    if native_calls is None:
        native_calls = []
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
        if call.get("type") != "function" or not isinstance(function, dict):
            raise ValueError("Unapproved tool")
        if req.model.startswith("google/"):
            tool = _flat_tool(function, req.model)
            arguments = json.dumps(tool)
        else:
            if function.get("name") != "call_local_tool":
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
        **web_fields,
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
            body = json.loads(raw, **({"parse_float": Decimal} if req.enable_web_search else {}))
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
