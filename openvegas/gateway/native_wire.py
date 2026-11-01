"""Native Messages/Responses transport on the existing reserve/settle ledger.

No tool execution, model fallback, history translation, or provider retries.
Host-owned tool and reasoning payloads stay on their original wire protocol.
"""
from __future__ import annotations

import asyncio
import codecs
import copy
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

import httpx

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.inference import AIGateway, InferenceRequest, InferenceResult
from openvegas.gateway.native_pricing import amount, charge, quote, web_fee
from openvegas.gateway.openrouter import _unique_response_fields
from openvegas.gateway.providers import get_model_review, model_capabilities

BASE = "https://openrouter.ai/api/v1"
MAX_REQUEST = 2_000_000
MAX_RESPONSE = 6_000_000
TIMEOUT = 180


def reject(message, code=APIErrorCode.INVALID_TRANSITION):
    raise ContractError(code, message)


class NativeSupplierBalanceError(ContractError):
    """A backend funding failure, not an invitation to retry or buy user $V."""

    def __init__(self):
        super().__init__(APIErrorCode.PROVIDER_UNAVAILABLE,
            "OpenVegas's provider account cannot fund this request. Contact OpenVegas support; "
            "buying more $V will not fix this. No automatic retry or fallback was made. "
            "The original reservation remains held pending reconciliation.")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def decode(raw):
    def invalid_constant(_):
        raise ValueError("Non-finite provider JSON")
    return json.loads(raw, object_pairs_hook=_unique_response_fields, parse_constant=invalid_constant)


async def failure_category(response):
    """Retain a diagnostic category, never supplier text that may echo input."""
    raw = bytearray()
    async for part in response.aiter_bytes():
        if len(raw) + len(part) > 128_000:
            return "oversized_error"
        raw.extend(part)
    try:
        message = decode(raw).get("error", {}).get("message", "").lower()
    except (ValueError, TypeError, AttributeError):
        return "unclassified"
    for needle, category in (
        ("no endpoints found", "no_compatible_endpoint"),
        ("namespace", "namespace_incompatible"),
        ("reasoning", "reasoning_incompatible"),
        ("parallel_tool_calls", "parallel_tools_incompatible"),
        ("tool_choice", "tool_choice_incompatible"),
        ("max_tool_calls", "hosted_tool_limit_incompatible"),
        ("web_search", "hosted_web_incompatible"),
        ("insufficient credits", "supplier_balance"),
        ("credit balance is too low", "supplier_balance"),
        ("insufficient_quota", "supplier_balance"),
        ("exceeded your current quota", "supplier_balance"),
        ("invalid model", "model_unavailable"),
    ):
        if needle in message:
            return category
    return "unclassified"


@dataclass(frozen=True)
class WireRequest:
    protocol: str
    encoded: str
    pricing_json: str
    input_bound: int
    streaming: bool
    beta: str = ""
    web_calls: int = 0

    @property
    def model_rounds(self):
        return 2 if self.web_calls else 1

    @property
    def pricing(self):
        return json.loads(self.pricing_json)


@dataclass(frozen=True)
class Receipt:
    payload_hash: str
    request_id: str
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int


def _has_media(value):
    if isinstance(value, dict):
        kind = value.get("type")
        return (isinstance(kind, str) and kind in {"image", "document", "input_image", "input_file"}) or any(_has_media(v) for v in value.values())
    return isinstance(value, list) and any(_has_media(v) for v in value)


def has_media(body):
    # Tool schemas may contain image examples; they are definitions, not uploads.
    return any(_has_media(body.get(key)) for key in ("messages", "input"))


def has_deferred_tools(tools):
    return any(tool.get("defer_loading") is True or
               (tool.get("type") == "namespace" and has_deferred_tools(tool["tools"]))
               for tool in tools)


def validate_claude_web_history(body):
    """Completed search history is inert; unresolved server calls can resume work."""
    pending, seen = set(), set()
    for message in body.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "server_tool_use" and block.get("name") == "web_search":
                identifier = block.get("id")
                if not isinstance(identifier, str) or not identifier or identifier in seen:
                    reject("Ambiguous native web search history; nothing was removed or sent.")
                pending.add(identifier)
                seen.add(identifier)
            elif block.get("type") == "web_search_tool_result":
                identifier = block.get("tool_use_id")
                if not isinstance(identifier, str) or identifier not in pending:
                    reject("Unpaired native web search history; nothing was removed or sent.")
                pending.remove(identifier)
    if pending:
        reject("Pending Claude hosted search needs a reviewed full-request reservation. "
               "Resume with ordinary host billing; no history was removed or request sent.")


def prepare_request(session, body, protocol, *, beta=""):
    if protocol not in {"messages", "responses"} or type(body) is not dict:
        reject("Unsupported native request.")
    expected = "messages" if session["host"] == "claude" else "responses"
    if protocol != expected or body.get("model") != session["model"]:
        reject("The request does not match this host/model's approved $V session.")
    provider = session.get("provider", "openrouter")
    if (provider not in {"openrouter", "anthropic" if protocol == "messages" else "openai"}
            or session["pricing"].get("provider", "openrouter") != provider):
        reject("Native provider does not match the approved session.")
    body = copy.deepcopy(body)
    allowed = ({"model", "messages", "system", "max_tokens", "stream", "tools", "tool_choice",
                "thinking", "output_config", "temperature", "top_p", "top_k", "stop_sequences", "metadata",
                "context_management", "safeguards"}
               if protocol == "messages" else
               {"model", "input", "instructions", "max_output_tokens", "stream", "tools", "tool_choice",
                "reasoning", "text", "parallel_tool_calls", "store", "include", "temperature", "top_p",
                "truncation", "metadata", "client_metadata", "prompt_cache_key", "previous_response_id", "max_tool_calls"})
    if set(body) - allowed:
        reject("This native request contains unsupported options; nothing was dropped or sent.")
    if body.get("store") is True or body.get("previous_response_id") is not None:
        reject("This supplier needs full native history, not stored response IDs. No context was discarded.")
    if body.get("truncation", "disabled") != "disabled":
        reject("Automatic history truncation is not allowed in $V mode.")
    if type(body.get("stream", False)) is not bool:
        reject("Invalid streaming flag.")
    # Namespace groups are host-executed tools, not paid server tools. Inspect
    # the entire tree without flattening names or modifying their schemas.
    pending = [(body.get("tools", []), 0)]
    count = 0
    web_calls = 0
    while pending:
        tools, depth = pending.pop()
        if type(tools) is not list or depth > 8:
            reject("Invalid native tool definitions.")
        count += len(tools)
        if count > 256:
            reject("Too many native tool definitions.")
        for tool in tools:
            if type(tool) is not dict:
                reject("Invalid native tool definition.")
            kind = tool.get("type", "custom")
            if kind == "namespace" and protocol == "responses":
                pending.append((tool.get("tools"), depth + 1))
            elif kind == "web_search_20250305" and protocol == "messages":
                # max_uses caps searches, not the native sampling loop. Do not
                # reuse the reviewed OpenAI two-round quote for this protocol.
                reject("Claude hosted search has no reviewed full-request model-round bound for this route. "
                       "max_uses=1 alone is insufficient; nothing was removed or sent.")
            elif kind == "web_search" and protocol == "responses" and depth == 0:
                policy = session["pricing"].get("native_web")
                if policy is None or web_calls or tool.get("engine", "native") != "native":
                    reject("Native web search needs a reviewed, single-search price quote; nothing was sent.")
                if set(tool) - {"type", "engine", "filters", "search_context_size", "user_location", "external_web_access"}:
                    reject("Unreviewed native web search options; nothing was removed or sent.")
                if 'external_web_access' in tool and tool['external_web_access'] is not True:
                    reject('Cached/offline hosted search is not reviewed by this supplier. Use explicit live search or local-tools-only.')
                web_fee(session["pricing"], 1)
                tool["engine"] = "native"
                web_calls = 1
            elif kind not in {"function", "custom"}:
                reject("Provider-hosted paid tools are not priced for this route. "
                       "Nothing was removed or sent. Use ordinary host billing, or explicitly opt into local-tools-only mode.")
    if web_calls:
        if isinstance(body.get('tool_choice'), dict) and body['tool_choice'].get('type') == 'web_search':
            reject('This supplier does not accept forced web_search tool choice. Use native auto/required selection; nothing was sent.')
        if type(body.get("max_tool_calls", 1)) is not int or body.get("max_tool_calls", 1) != 1:
            reject("This quote permits one hosted search per request; start with a reviewed limit.")
        body["max_tool_calls"] = 1
    elif "max_tool_calls" in body:
        reject("Hosted tool limits require a reviewed hosted tool.")
    field = "max_tokens" if protocol == "messages" else "max_output_tokens"
    limit = body.get(field, session["max_tokens"])
    if type(limit) is not int or not 1 <= limit <= session["max_tokens"]:
        reject("Output limit exceeds your approved session limit; start a new session to increase it.")
    body[field] = limit
    if protocol == "responses":
        body["store"] = False
    encoded = canonical(body)
    if len(encoded.encode()) > MAX_REQUEST or len(beta) > 2048 or "\n" in beta or "\r" in beta:
        reject("Native request exceeds its size limit.")
    # UTF-8 bytes are a conservative text/token bound; tool schemas count too.
    # Multimodal payloads use the reviewed full context bound, never an invented
    # image/PDF token estimate. Cache writes reserve up to 2x normal input cost.
    input_bound = len(encoded.encode()) + 2048
    context = model_capabilities(provider, session["model"])["context_window_tokens"]
    if has_media(body) or web_calls:
        # No new multimodal dispatch without a reviewed native fee schedule.
        # Duplicate detection precedes fresh-work review; no answer is cached.
        input_bound = context - limit if type(context) is int else input_bound
    # Cache-control can increase supplier input charges; reserve it conservatively.
    cache_factor = 2 if '"cache_control"' in encoded else 1
    input_bound *= cache_factor
    wire = WireRequest(protocol, encoded, canonical(session["pricing"]), input_bound, body.get("stream", False), beta, web_calls)
    key = hashlib.sha256((protocol + "\n" + encoded + "\n" + beta).encode()).hexdigest()
    req = InferenceRequest(account_id="user:" + session["user_id"], provider=provider, model=session["model"],
                           messages=[{"role": "user", "content": encoded}], max_tokens=limit,
                           idempotency_key="host:" + session["session_id"] + ":" + key, strict_continuity=True)
    req._host_wire = wire
    return req, key


def validate_preflight(req, model_config):
    wire = req._host_wire
    if (type(wire) is not WireRequest or req.provider not in {"openrouter", "openai", "anthropic"}
            or req.provider != wire.pricing.get("provider", "openrouter")
            or req.enable_tools or req.enable_web_search):
        reject("Invalid native wire boundary.")
    if canonical(wire.pricing) != canonical(quote(model_config, req.provider)):
        reject("Native prices changed. Start a new explicitly approved $V session.")
    routing(wire)
    if req.max_tokens > model_config["max_tokens"]:
        reject("The output budget exceeds the current catalog limit.")
    body = json.loads(wire.encoded)
    if wire.protocol == "messages":
        validate_claude_web_history(body)
    if has_deferred_tools(body.get("tools", [])):
        reject("Deferred tools can activate unpriced supplier tool search. Nothing was sent.")
    if has_media(body):
        media = wire.pricing.get("native_media")
        if media is None:
            reject("Native image/PDF supplier fees are not yet reviewed for $V mode. "
                   "Nothing was sent or removed; use ordinary host billing for attachments.")
        from openvegas.gateway.native_media import validate
        try:
            validate(body, media["modalities"])
        except ValueError:
            reject("Native attachments require valid bounded inline image/PDF bytes and an available validator. "
                   "Nothing was removed or sent.")
    context = model_capabilities(req.provider, req.model)["context_window_tokens"]
    if type(context) is not int:
        reject("The selected model needs a current context review.")
    cache_factor = 2 if '"cache_control"' in wire.encoded else 1
    if wire.input_bound // cache_factor + req.max_tokens > context:
        reject("Context exceeds the reviewed model limit; no history was truncated.")
    pricing = wire.pricing
    config = dict(model_config)
    ratio = amount(pricing["v_per_supplier_usd"])
    config["v_price_input_per_1m"] = amount(config["cost_input_per_1m"]) * ratio
    config["v_price_output_per_1m"] = amount(config["cost_output_per_1m"]) * ratio
    return config


def reservation(req):
    p = req._host_wire.pricing
    cost = (req._host_wire.input_bound * amount(p["supplier_input_usd_per_million"])
            + req.max_tokens * amount(p["supplier_output_usd_per_million"])) / 1_000_000
    cost = cost * req._host_wire.model_rounds + web_fee(p, req._host_wire.web_calls)
    return charge(cost, p)


def routing(wire):
    policy = {"allow_fallbacks": False, "require_parameters": True,
              "max_price": {"prompt": float(amount(wire.pricing["supplier_input_usd_per_million"])),
                            "completion": float(amount(wire.pricing["supplier_output_usd_per_million"])),
                            "request": 0}}
    if wire.protocol == "messages":
        policy["only"] = ["Anthropic"]
    if wire.pricing.get("native_transport") == "responses_openai_v1":
        if wire.protocol != "responses":
            reject("Reviewed native transport does not match the host protocol.")
        # OpenRouter's chat-parameter availability filter rejects this native
        # Responses shape. Pin the reviewed native provider instead; preserve
        # every tool/history/parameter and let its protocol validate the body.
        policy.update(only=["openai"], require_parameters=False)
    return policy


def settlement_charge(req, result, ctx):
    receipt = result._host_wire_receipt
    if (type(receipt) is not Receipt or receipt.payload_hash != ctx.payload_hash
            or receipt.request_id != result.provider_request_id or receipt.cost_usd != result.actual_cost_usd
            or (receipt.input_tokens, receipt.output_tokens) != (result.input_tokens, result.output_tokens)):
        reject("Native usage receipt does not match the prepared request.", APIErrorCode.HOLD_CONFLICT)
    value = charge(receipt.cost_usd, req._host_wire.pricing)
    if value > reservation(req):
        reject("Native supplier usage exceeds the approved reservation.", APIErrorCode.HOLD_CONFLICT)
    return value


def frame(event, data):
    return ("event: " + event + "\n" if event else "") + "data: " + canonical(data) + "\n\n"


async def events(response):
    size = 0
    event, data = "", []
    buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")()
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE:
            reject("Native provider response exceeded its size limit.", APIErrorCode.PROVIDER_UNAVAILABLE)
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.removesuffix("\r")
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                raw = "\n".join(data)
                if raw != "[DONE]":
                    yield event, decode(raw)
                event, data = "", []
    buffer += decoder.decode(b"", final=True)
    if data or buffer.strip():
        reject("Native stream ended mid-event.", APIErrorCode.PROVIDER_UNAVAILABLE)


class NativeGateway(AIGateway):
    @staticmethod
    def _wrapper_rewards_enabled():
        return False

    async def _estimate_grant_cover_v(self, **kwargs):
        # Native consent covers a dollar-equivalent wallet budget, not unrelated
        # legacy model grants or rewards. Reuse wallet reserves and settlement.
        return Decimal(0)

    async def _consume_grants(self, **kwargs):
        return 0

    async def _replay_native(self, req):
        async with self.db.transaction() as tx:
            row = await tx.fetchrow(
                "SELECT * FROM inference_requests WHERE user_id=$1 AND idempotency_key=$2 FOR UPDATE",
                self._extract_user_id(req.account_id), req.idempotency_key)
            if row is None:
                return None
            if row.get("native_route_command_id") is not None:
                reject("Native generation requires its original scoped route replay.", APIErrorCode.HOLD_CONFLICT)
            if row["payload_hash"] != self._payload_hash(req):
                reject("Native replay payload mismatch.", APIErrorCode.IDEMPOTENCY_CONFLICT)
            if row["status"] == "succeeded" and row["response_status"] == 200:
                return self._deserialize_result(row)
            reject("Prior native request requires reconciliation; no retry was made.", APIErrorCode.HOLD_CONFLICT)

    async def _abort_inference_execution(self, ctx):
        if not getattr(ctx, "native_dispatched", False):
            return await super()._abort_inference_execution(ctx)
        # A disconnected caller is not proof the supplier stopped or charged zero.
        # Preserve the existing escrow, including across sessions/process exits.
        async with self.db.transaction() as tx:
            row = await tx.fetchrow("SELECT status,response_body_text FROM inference_requests WHERE id=$1 FOR UPDATE", ctx.request_id)
            if not row or row["status"] != "processing":
                return
            evidence = json.loads(row["response_body_text"] or "{}")
            evidence.update(reconciliation_required=True, wallet_hold_retained=True)
            evidence["native_failure"] = {
                "stage": getattr(ctx, "native_stage", "dispatch"),
                "upstream_status": getattr(ctx, "native_upstream_status", None),
                "category": getattr(ctx, "native_failure_category", None),
            }
            await self._mark_request_failed(request_id=ctx.request_id, tx=tx, web_failure=evidence,
                                            provider_request_id=ctx.provider_request_id)

    async def execute(self, req):
        """Yield native wire chunks and one settled result; terminal event is last."""
        replay = await self._replay_native(req)
        ctx = None
        if replay is None:
            ctx, replay = await self._prepare_inference_execution(req)
        if replay is not None:
            reject("Native response replay is disabled; use the host's saved conversation. "
                   "No new request or charge was made.", APIErrorCode.HOLD_CONFLICT)
        wire, body = req._host_wire, json.loads(req._host_wire.encoded)
        usage, response_body, terminal = {}, {}, None
        identifier = None
        aliases = {req.model, *get_model_review(req.provider, req.model).get("response_model_ids", [])}
        def identify(response):
            value = response.get("id")
            if (not isinstance(value, str) or not 1 <= len(value) <= 256 or value.startswith("sk-")
                    or response.get("model") not in aliases or identifier is not None and identifier != value):
                raise ValueError("Invalid or changed provider identity")
            return value
        try:
            async with asyncio.timeout(TIMEOUT):
                ctx.native_stage = "dispatch"
                # Server credentials never leave this backend. No caller-selected
                # destination, provider fallback or price override is accepted.
                if req.provider == "openrouter":
                    body["provider"] = routing(wire)
                if has_media(body) and req.provider == "openrouter":
                    from openvegas.gateway.native_media import validate
                    pdf = validate(body, wire.pricing["native_media"]["modalities"])
                    body["plugins"] = [{"id": "file-parser", "pdf": {"engine": "native"}}] if pdf else []
                    body["provider"]["max_price"]["image"] = 0
                headers = {"Authorization": "Bearer " + ctx.provider_api_key, "Content-Type": "application/json"}
                endpoint = BASE + "/" + wire.protocol
                if req.provider != "openrouter":
                    from openvegas.gateway.native_direct import transport
                    endpoint, headers = transport(req.provider, wire.protocol, ctx.provider_api_key, wire.beta)
                if wire.protocol == "messages":
                    headers["anthropic-version"] = "2023-06-01"
                    if wire.beta:
                        headers["anthropic-beta"] = wire.beta
                async with httpx.AsyncClient(transport=getattr(self, "native_transport", None),
                                             timeout=TIMEOUT, follow_redirects=False) as client:
                    # Only server state sets this marker; uncertainty after this
                    # point must retain session allowance until reconciliation.
                    dispatch = {"protocol": wire.protocol, "model": req.model,
                            "pricing": wire.pricing, "provider": req.provider, "payload_hash": ctx.payload_hash,
                            "input_bound": wire.input_bound * wire.model_rounds,
                            "output_bound": req.max_tokens * wire.model_rounds,
                            "web_calls": wire.web_calls, "account_id": req.account_id,
                            "credential_sha256": hashlib.sha256(ctx.provider_api_key.encode()).hexdigest(),
                            "budget": req._host_budget}
                    await self.db.execute("UPDATE inference_requests SET response_body_text=$2,updated_at=now() WHERE id=$1 AND status='processing'",
                        ctx.request_id, canonical({"native_dispatch": dispatch,
                            "reconciliation_required": True, "wallet_hold_retained": True}))
                    ctx.native_dispatched = True
                    req._host_dispatch_started = True
                    async with client.stream("POST", endpoint, headers=headers, json=body) as upstream:
                        ctx.native_upstream_status = upstream.status_code
                        if upstream.status_code != 200:
                            ctx.native_failure_category = await failure_category(upstream)
                            if upstream.status_code in {400, 402, 429} and ctx.native_failure_category == "supplier_balance":
                                raise NativeSupplierBalanceError()
                            reject("Native supplier rejected this request (HTTP " + str(upstream.status_code)
                                   + "; " + ctx.native_failure_category + "). No retry was made.",
                                   APIErrorCode.PROVIDER_UNAVAILABLE)
                        ctx.native_stage = "identity_and_stream"
                        if wire.streaming:
                            async for name, value in events(upstream):
                                if type(value) is not dict:
                                    raise ValueError("Invalid provider event")
                                if ctx.provider_api_key in canonical(value):
                                    raise ValueError("Reflected provider credential")
                                kind = value.get("type", name)
                                if kind in {"error", "response.failed"}:
                                    raise ValueError("Provider failed")
                                if kind == "message_start":
                                    response_body = value["message"]
                                    usage.update(response_body.get("usage", {}))
                                    identifier = identify(response_body)
                                if kind == "message_delta":
                                    usage.update(value.get("usage", {}))
                                    response_body.update(value.get("delta", {}))
                                if kind.startswith("response.") and isinstance(value.get("response"), dict):
                                    response_body = value["response"]
                                    identifier = identify(response_body)
                                    usage.update(response_body.get("usage") or {})
                                if identifier is None:
                                    if kind == "ping":
                                        continue
                                    raise ValueError("Native event arrived before confirmed identity")
                                if ctx.provider_request_id is None:
                                    await self.db.execute("UPDATE inference_requests SET provider_request_id=$2 WHERE id=$1 AND status='processing'",
                                                          ctx.request_id, identifier)
                                ctx.provider_request_id = identifier
                                chunk = frame(name, value)
                                if kind in {"message_stop", "response.completed", "response.incomplete"}:
                                    terminal = chunk
                                    break
                                yield chunk
                            if terminal is None:
                                raise ValueError("Missing terminal event")
                        else:
                            data = bytearray()
                            async for chunk in upstream.aiter_bytes():
                                data.extend(chunk)
                                if len(data) > MAX_RESPONSE:
                                    raise ValueError("Response too large")
                            response_body = decode(data)
                            if ctx.provider_api_key in canonical(response_body):
                                raise ValueError("Reflected provider credential")
                            usage = response_body.get("usage") or {}
                            identifier = identify(response_body)
                            ctx.provider_request_id = identifier
                    if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 256
                            or identifier.startswith("sk-") or response_body.get("model") not in aliases):
                        raise ValueError("Invalid provider identity")
                    cost = usage.get("cost")
                    ctx.native_stage = "receipt"
                    if req.provider != "openrouter":
                        from openvegas.gateway.native_direct import metered_cost
                        cost = metered_cost(req.provider, usage, wire.pricing, response_body)
                    elif cost is None:
                        # Read-only receipt lookup is not another model request.
                        receipt = await client.get(BASE + "/generation", params={"id": identifier},
                                                   headers={"Authorization": headers["Authorization"]})
                        if receipt.status_code != 200 or len(receipt.content) > 128_000:
                            raise ValueError("Metering receipt unavailable")
                        metering = decode(receipt.content)["data"]
                        if metering.get("id") != identifier or metering.get("model") not in aliases:
                            raise ValueError("Metering identity mismatch")
                        cost = metering["total_cost"]
                counts = [usage.get("input_tokens"), usage.get("output_tokens")]
                if any(type(n) is not int or n < 0 for n in counts):
                    raise ValueError("Invalid native usage")
                if wire.protocol == "messages":
                    for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
                        n = usage.get(key, 0)
                        if type(n) is not int or n < 0:
                            raise ValueError("Invalid cache usage")
                        counts[0] += n
                if counts[0] > wire.input_bound * wire.model_rounds or counts[1] > req.max_tokens * wire.model_rounds:
                    raise ValueError("Usage exceeds approved bounds")
                if wire.web_calls:
                    search_count = sum(item.get('type') == 'web_search_call'
                                       for item in response_body.get('output', []) if isinstance(item, dict))
                    reports = [usage.get(key, {}).get('web_search_requests', search_count)
                               for key in ('server_tool_use', 'server_tool_use_details')]
                    if (any(type(value) is not int or not 0 <= value <= wire.web_calls for value in reports)
                            or search_count > wire.web_calls):
                        raise ValueError('Hosted search exceeded the approved limit')
                if wire.protocol == "messages":
                    status = response_body.get("stop_reason")
                    if status not in {"end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "pause_turn"}:
                        raise ValueError("Unconfirmed message completion")
                elif response_body.get("status") not in {"completed", "incomplete"}:
                    raise ValueError("Unconfirmed response completion")
                actual_cost = amount(cost)
                if req.provider != "openrouter":
                    from openvegas.gateway.native_receipt import journal
                    receipt = journal(dispatch, identifier, usage, response_body)
                    # Commit metering independently before the wallet transaction.
                    # A crash can then settle this receipt without another model call.
                    await self.db.execute(
                        "UPDATE inference_requests SET response_body_text="
                        "(response_body_text::jsonb || $2::jsonb)::text,provider_request_id=$3,"
                        "updated_at=now() WHERE id=$1 AND status='processing'",
                        ctx.request_id, canonical({"native_receipt": receipt}), identifier)
                ctx.native_stage = "settlement"
                result = InferenceResult(text="", input_tokens=counts[0], output_tokens=counts[1],
                                         actual_cost_usd=actual_cost, provider_request_id=identifier,
                                         completion_status="completed")
                result._host_wire_receipt = Receipt(ctx.payload_hash, identifier, actual_cost, *counts)
                result._host_wire_response = {"protocol": wire.protocol,
                                             "body": response_body if not wire.streaming else None,
                                             "pricing": wire.pricing, "supplier_cost_usd": str(actual_cost)}
                result = await self._finalize_inference_execution(ctx, req, result)
            yield result
            if terminal is not None:
                yield terminal
        except (Exception, asyncio.CancelledError, GeneratorExit) as exc:
            ctx.provider_request_id = identifier
            await self._cleanup_inference_after_failure(ctx)
            if isinstance(exc, (asyncio.CancelledError, GeneratorExit, ContractError)):
                raise
            reject("Native request could not be verified or completed; do not automatically retry. Check wallet history.",
                   APIErrorCode.PROVIDER_UNAVAILABLE)
