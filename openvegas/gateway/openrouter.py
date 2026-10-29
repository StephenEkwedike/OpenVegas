"""Managed OpenRouter transport: exact model, bounded request, no fallback/retry.

No customer keys, consumer logins, prompt truncation or automatic local tools.
The separately reviewed bounded web path permits one Exa server-tool step.
Capability and price limits come from our server catalog, not caller settings.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.contracts.native_tool_schema import (
    FLAT_V2,
    GENERIC_V1,
    GOOGLE_FLAT_V1,
    flat_v2_definitions,
    validate_operation,
)
from openvegas.gateway.openrouter_web import (
    PreparedWebSearch,
    WebValidationError,
    prepare_server_review,
)
from openvegas.gateway.reasoning import reasoning_payload
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
        "Duplicate provider response field": "ambiguous_response",
        "Reflected credential": "reflected_credential",
    }.get(error.args[0], "malformed_response")


def _request_identity(body: Any) -> str | None:
    value = body.get("id") if isinstance(body, dict) else None
    if (isinstance(value, str) and PROVIDER_CALL_ID.fullmatch(value)
            and not value.lower().startswith("sk-")):
        return value
    return None


def _unique_response_fields(pairs: list[tuple[str, Any]]) -> dict:
    # Never choose one of conflicting IDs, tool fields or billing receipts.
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError("Duplicate provider response field")
        fields[key] = value
    return fields


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


def _google_flat_v1_definitions() -> list[dict]:
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


def local_tool_definitions(model: str) -> list[dict]:
    """All fresh local-tool requests use the per-operation v2 contract."""
    return flat_v2_definitions()


def _versioned_tool_definitions(model: str, version: str) -> list[dict]:
    if version == FLAT_V2:
        return local_tool_definitions(model)
    if version == GENERIC_V1 and not model.startswith("google/"):
        return [local_tool_definition()]
    if version == GOOGLE_FLAT_V1 and model.startswith("google/"):
        return _google_flat_v1_definitions()
    raise ContractError(APIErrorCode.INVALID_TRANSITION, "Unrecognized retained native tool schema.")


def _schema_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _retained_tool_version(req: Any, payload: dict) -> str:
    tools = payload.get("tools")
    has_web = (type(tools) is list and bool(tools) and type(tools[0]) is dict
               and tools[0].get("type") == "openrouter:web_search")
    if bool(req.enable_web_search) != has_web:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Unrecognized retained native tool schema.")
    return retained_tool_schema_version(payload, model=req.model)


def retained_tool_schema_version(payload: dict, *, model: str) -> str:
    """Classify exact local definitions from an already validated private request.

    This recognizes the optional web prefix, not its execution authority. The
    caller must retain the existing full-envelope ownership and settings checks.
    """
    if type(payload) is not dict or payload.get("model") != model or not valid_model(model):
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Unrecognized retained native tool schema.")
    tools = payload.get("tools")
    if type(tools) is not list:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Unrecognized retained native tool schema.")
    if tools and type(tools[0]) is dict and tools[0].get("type") == "openrouter:web_search":
        # The full web prefix is revalidated against the current reviewed payload
        # below. Only this separately bounded server tool may precede local tools.
        tools = tools[1:]
    versions = (FLAT_V2, GOOGLE_FLAT_V1 if model.startswith("google/") else GENERIC_V1)
    for version in versions:
        if _schema_json(tools) == _schema_json(_versioned_tool_definitions(model, version)):
            return version
    raise ContractError(APIErrorCode.INVALID_TRANSITION, "Unrecognized retained native tool schema.")


def _native_payload(req: Any) -> dict | None:
    from openvegas.agent.native_envelope import continuation_payload
    from openvegas.agent.native_generation import NativeGenerationClaim

    claim = getattr(req, "_native_generation_claim", None)
    if claim is not None and type(claim) is not NativeGenerationClaim:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Invalid private native generation claim.")
    return continuation_payload(req)


def _request_tool_version(req: Any) -> str:

    # continuation_payload accepts only the exact private NativeGenerationClaim
    # type. Neither an HTTP option nor a response function name selects a version.
    native = _native_payload(req)
    return _retained_tool_version(req, native) if native is not None else FLAT_V2


def _decode_tool_arguments(raw: str) -> Any:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                # Never reflect file paths or other provider argument contents.
                raise ValueError("Duplicate tool argument key")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique_object)


def _flat_tool(function: dict, model: str, *, schema_version: str = FLAT_V2) -> dict:
    definitions = {d["function"]["name"]: d["function"]
                   for d in _versioned_tool_definitions(model, schema_version)}
    definition = definitions.get(function.get("name"))
    if not definition or definition["name"] == "call_local_tool":
        raise ValueError("Unapproved tool")
    raw = function.get("arguments")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32_000:
        raise ValueError("Invalid tool arguments")
    args = _decode_tool_arguments(raw)
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
    result = {"tool_name": name, "arguments": args, "shell_mode": mode, "timeout_sec": timeout}
    validate_operation(result)
    return result


def input_token_bound(req: Any) -> int:
    native = _native_payload(req)
    if native is not None:
        return _native_input_bound(req, native)
    return _initial_input_token_bound(req, local_tool_definitions(req.model) if req.enable_tools else None)


def supplier_cost_bound(req: Any, model_config: dict, capabilities: dict) -> Decimal:
    """Preflight the exact request before a caller reserves an approved USD cap.

    No IO or dispatch. Retail wallet reservation remains separately token-priced.
    The bound includes supplier image fees and the receipt rounding tolerance.
    """
    build_payload(req, model_config, capabilities)
    if req.enable_web_search:
        return req._managed_web_context.prepared.budget.supplier_total_usd
    dispatch = req._managed_openrouter_dispatch
    return (
        (dispatch.input_tokens * price(model_config["cost_input_per_1m"])
         + req.max_tokens * price(model_config["cost_output_per_1m"])) / 1_000_000
        + dispatch.supplier_image_cost_usd + Decimal("0.000001")
    )


def _initial_input_token_bound(req: Any, tools: list[dict] | None) -> int:
    attachments = getattr(req, "_managed_attachment_context", None)
    if attachments is not None:
        return _attachment_input_bound(req, tools)
    bound = len(json.dumps(req.messages, ensure_ascii=False).encode("utf-8")) + 256
    if req.enable_tools:
        bound += len(json.dumps(tools).encode("utf-8")) + 256
    return bound


def _attachment_input_bound(req: Any, tools: list[dict] | None) -> int:
    from openvegas.agent.native_envelope import continuation_payload

    attachment = req._managed_attachment_context
    if continuation_payload(req) is None:
        return attachment.token_bound(req, tools)
    projected = copy.copy(req)
    projected.messages = _native_metering_view(req)
    base = attachment.token_bound(projected, tools)
    # The media validator sees exact owned media and plain text; additionally
    # reserve ALL opaque/native metadata bytes omitted only from that view.
    overhead = max(0, len(json.dumps(req.messages, ensure_ascii=False).encode("utf-8"))
                   - len(json.dumps(projected.messages, ensure_ascii=False).encode("utf-8")))
    return base + overhead + 256


def _native_metering_view(req: Any) -> list[dict]:
    from openvegas.agent.native_envelope import metering_messages

    projected = metering_messages(req.messages)
    attachment = getattr(req, "_managed_attachment_context", None)
    if attachment is not None:
        expected = attachment.prepared.blocks
        for message in projected:
            if message["role"] == "user" and isinstance(message["content"], list):
                # Existing media accounting compares encoded key order. Match a
                # semantically identical freshly owned block ONLY in this view;
                # the original wire payload and signatures are never rewritten.
                message["content"] = [copy.deepcopy(next((owned for owned in expected if owned == block), block))
                                      for block in message["content"]]
    return projected


def _native_input_bound(req: Any, native: dict) -> int:
    if getattr(req, "_managed_attachment_context", None) is not None:
        extra = {k: v for k, v in native.items() if k not in {"messages", "tools"}}
        return _attachment_input_bound(req, native.get("tools")) + len(json.dumps(extra).encode("utf-8")) + 256
    return len(json.dumps(native, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 256


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
    supplier_image_cost_usd: Decimal = Decimal(0)


def _dispatch_metering(req: Any, model_config: dict, input_bound: int, payload: dict) -> DispatchMetering:
    context = getattr(req, "_managed_attachment_context", None)
    image_cost = Decimal(0)
    if context is not None:
        prepared = context.prepared
        if prepared.review.implicit_cache_only:
            # Validate actual wire metadata, including retained native messages.
            # Text/tool argument strings are not parsed or searched for keywords.
            pending = [payload]
            while pending:
                item = pending.pop()
                if isinstance(item, dict):
                    if "cache_control" in item:
                        raise ContractError(APIErrorCode.INVALID_TRANSITION,
                                            "Paid prompt caching is not reviewed for this attachment route.")
                    pending.extend(item.values())
                elif isinstance(item, list):
                    pending.extend(item)
        image_cost = prepared.supplier_image_cost_usd
    return DispatchMetering(input_bound, _web_binding(req, model_config), image_cost)


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
            _attachment_input_bound(req, payload["tools"])
        return payload


def _web_attachment_options(req: Any, model_config: dict, prepared: PreparedWebSearch) -> dict:
    context = req._managed_attachment_context
    # Accept only the server's ownership-checked immutable attachment context.
    from server.services.openrouter_attachment_request import AttachmentRequestContext

    if not isinstance(context, AttachmentRequestContext):
        raise WebValidationError("invalid_private_attachment_context")
    options = context.validate(req, model_config)
    attachment = context.prepared.review
    if attachment.image_price_usd:
        raise WebValidationError("web_attachment_image_fee_unreviewed")
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
    return _prepare_web_context(req, model_config, capabilities, _request_tool_version(req))


def _prepare_web_context(req: Any, model_config: dict, capabilities: dict, tool_version: str) -> ManagedWebContext:
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
        output_parameter = attachment.prepared.review.output_token_parameter
        if output_parameter != "max_tokens":
            payload[output_parameter] = payload.pop("max_tokens")
        payload["provider"]["max_price"]["image"] = 0
        if options["plugins"]:
            payload["plugins"] = [
                p for p in payload["plugins"] if p["id"] != "file-parser"
            ] + options["plugins"]
    if req.enable_tools:
        payload["tools"].extend(_versioned_tool_definitions(req.model, tool_version))
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
    native = _native_payload(req)
    if native is not None:
        return _build_native_continuation(req, native, model_config, capabilities)
    return _build_fresh_payload(req, model_config, capabilities, FLAT_V2)


def _build_fresh_payload(req: Any, model_config: dict, capabilities: dict, tool_version: str) -> dict:
    if not valid_model(req.model):
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION, "Choose an exact reviewed OpenRouter model ID."
        )
    if req.enable_web_search:
        try:
            context = getattr(req, "_managed_web_context", None)
            if context is None:
                context = _prepare_web_context(req, model_config, capabilities, tool_version)
            if not isinstance(context, ManagedWebContext):
                raise WebValidationError("invalid_private_web_context")
            payload = context.payload(req, model_config)
            if req.enable_tools and _retained_tool_version(req, payload) != tool_version:
                raise WebValidationError("web_request_changed_after_preflight")
            return payload
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
    tools = _versioned_tool_definitions(req.model, tool_version) if req.enable_tools else None
    input_bound = _initial_input_token_bound(req, tools)
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
    # Endpoint metadata may advertise only max_completion_tokens. The choice is
    # server-reviewed and bound to the attachment context, never caller input.
    output_parameter = (
        attachment_context.prepared.review.output_token_parameter
        if attachment_context is not None else "max_tokens"
    )
    payload = {
        "model": req.model,
        "messages": req.messages,
        output_parameter: req.max_tokens,
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
        payload.update(tools=tools, tool_choice="auto")
    payload.update(attachment_options)
    payload.update(reasoning_payload(getattr(req, "reasoning_effort", None), capabilities))
    req._managed_openrouter_dispatch = _dispatch_metering(req, model_config, input_bound, payload)
    # Output metering includes reasoning. Do not forward private thought fields.
    return payload


def _build_native_continuation(req: Any, native: dict, model_config: dict, capabilities: dict) -> dict:
    tool_version = _retained_tool_version(req, native)
    projected = copy.copy(req)
    projected.messages = _native_metering_view(req)
    projected._native_generation_claim = None
    projected._native_history_required = False
    projected._managed_web_context = None
    fresh = _build_fresh_payload(projected, model_config, capabilities, tool_version)
    if {k: v for k, v in native.items() if k != "messages"} != {k: v for k, v in fresh.items() if k != "messages"}:
        raise ContractError(APIErrorCode.INVALID_TRANSITION,
                            "Native provider settings changed; no altered continuation was sent.")
    encoded = json.dumps(native, ensure_ascii=False, separators=(",", ":"))
    bound = _native_input_bound(req, native)
    context_limit = capabilities.get("context_window_tokens")
    if (len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES or type(context_limit) is not int
            or bound + req.max_tokens > context_limit):
        raise ContractError(APIErrorCode.INVALID_TRANSITION,
                            "Native history exceeds its reviewed bound; nothing was truncated.")
    if req.enable_web_search:
        previous = req._managed_web_context
        prepared = projected._managed_web_context.prepared
        if previous is not None:
            if (not isinstance(previous, ManagedWebContext) or previous.prepared.snapshot != prepared.snapshot
                    or previous.payload(req, model_config) != native):
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Native web dispatch snapshot changed.")
            # Settlement holds this exact object, not merely an equivalent new
            # snapshot with a later timestamp. Rechecking dispatch must not replace it.
        else:
            req._managed_web_context = ManagedWebContext(prepared, _web_binding(req, model_config), encoded)
    req._managed_openrouter_dispatch = _dispatch_metering(req, model_config, bound, native)
    return native


@asynccontextmanager
async def _client(client):
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=False, trust_env=False
        ) as owned:
            yield owned


def parse_response(body: Any, req: Any, model_config: dict, parse_tool, *, expected_tool_schema: str | None = None) -> dict:
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
        if dispatch is not None:
            maximum += dispatch.supplier_image_cost_usd
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
    tool_version = _request_tool_version(req)
    if expected_tool_schema is not None and tool_version != expected_tool_schema:
        raise ValueError("Tool request violates the advertised schema")
    argument_properties = local_tool_definition()["function"]["parameters"]["properties"][
        "arguments"
    ]["properties"]
    primitive_types = {"string": str, "boolean": bool, "integer": int}
    tools = []
    for call in native_calls:
        function = call["function"]
        if call.get("type") != "function" or not isinstance(function, dict):
            raise ValueError("Unapproved tool")
        if tool_version != GENERIC_V1:
            tool = _flat_tool(function, req.model, schema_version=tool_version)
            # _flat_tool bounds the original wire arguments. Internal dispatch
            # wrapping must not shrink that allowance or expand Unicode escapes.
            arguments = json.dumps(tool, ensure_ascii=False)
        else:
            if function.get("name") != "call_local_tool":
                raise ValueError("Unapproved tool")
            arguments = function.get("arguments")
            if not isinstance(arguments, str) or len(arguments.encode("utf-8")) > 32_000:
                raise ValueError("Invalid tool arguments")
        tool = _decode_tool_arguments(arguments)
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
        validate_operation(tool, legacy_read_alias=tool_version == GENERIC_V1)
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
    req, api_key: str, *, model_config: dict, capabilities: dict, parse_tool, client=None,
    handoff_binding=None,
) -> dict:
    payload = build_payload(req, model_config, capabilities)
    tool_schema_version = _request_tool_version(req)
    if handoff_binding is not None:
        from server.services.native_handoff_guard import (
            validate_bound_request,
            validate_dispatch_deadline,
        )
        validate_bound_request(req, payload=payload, expected=handoff_binding)
        validate_dispatch_deadline(req, expected=handoff_binding)
    from openvegas.agent.native_envelope import capture_dispatch, capture_response
    native_dispatch = capture_dispatch(req, payload)
    if not api_key or not isinstance(api_key, str):
        raise ContractError(
            APIErrorCode.PROVIDER_UNAVAILABLE, "Managed OpenRouter credential unavailable."
        )
    request_id = None
    try:
        if native_dispatch is not None and api_key in native_dispatch.payload_json:
            raise ValueError("Reflected credential")
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
            body = json.loads(
                raw, object_pairs_hook=_unique_response_fields,
                **({"parse_float": Decimal} if req.enable_web_search else {}),
            )
            if native_dispatch is not None and api_key in json.dumps(body, default=str):
                raise ValueError("Reflected credential")
            candidate_id = _request_identity(body)
            if candidate_id and api_key in candidate_id:
                raise ValueError("Reflected credential")
            request_id = candidate_id
            result = parse_response(body, req, model_config, parse_tool, expected_tool_schema=tool_schema_version)
            if api_key in json.dumps(result, default=str):
                raise ValueError("Reflected credential")
            capture_response(req, native_dispatch, bytes(raw), result)
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
