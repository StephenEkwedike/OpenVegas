"""Private, lossless native-generation evidence. Never serialize into HTTP results.

Request bytes are historical evidence, not authorization to reuse an attachment.
Continuations must reauthorize the separately retained owned-file references.
Lock order matches native ownership: run, route, gateway, preauthorization.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from openvegas.agent.native_generation import NativeGenerationClaim, verify_source_scope_tx
from openvegas.contracts.errors import APIErrorCode, ContractError

MAX_ASSISTANT_BYTES = 2_000_000
MAX_PAYLOAD_BYTES = 1_100_000
MAX_INPUTS_BYTES = 65_536
MAX_DEPTH = 64
MAX_NODES = 100_000


class NativeHistoryPrecisionError(ContractError):
    diagnostic_reason = "native_history_numeric_precision"

    def __init__(self):
        super().__init__(APIErrorCode.INVALID_TRANSITION,
                         "This response contains vendor numeric state that cannot be continued without precision loss. "
                         "No tool or follow-up request was executed.")


def _fail() -> None:
    raise ContractError(APIErrorCode.INVALID_TRANSITION,
                        "Private native history is missing, invalid or mismatched; no continuation is authorized.") from None


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            _fail()
        out[key] = value
    return out


def _constant(_value):
    _fail()


def _decode(raw: str, bound: int) -> Any:
    try:
        if type(raw) is not str or len(raw.encode("utf-8")) > bound:
            _fail()
        value = json.loads(raw, object_pairs_hook=_pairs, parse_float=Decimal, parse_constant=_constant)
        pending, visited = [(value, 0)], 0
        while pending:
            item, depth = pending.pop()
            visited += 1
            if depth > MAX_DEPTH or visited > MAX_NODES:
                _fail()
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        return value
    except (UnicodeError, ValueError, TypeError, RecursionError):
        _fail()


def _encode(value: Any, bound: int) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        _decode(raw, bound)
        return raw
    except (UnicodeError, ValueError, TypeError, RecursionError):
        _fail()


def _portable_json(raw: str, bound: int) -> Any:
    """Refuse opaque numeric values the current JSON wire encoder would round."""
    exact = _decode(raw, bound)
    try:
        value = json.loads(raw)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if _decode(encoded, bound) != exact:
            raise NativeHistoryPrecisionError() from None
        return value
    except (ValueError, TypeError, RecursionError):
        raise NativeHistoryPrecisionError() from None


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _uuid(value: str) -> str:
    try:
        if type(value) is not str or str(UUID(value)) != value or UUID(value).int == 0:
            _fail()
        return value
    except (ValueError, TypeError, AttributeError):
        _fail()


@dataclass(frozen=True, repr=False)
class NativeHistoryInputs:
    _json: str = field(repr=False)

    def __repr__(self):
        return "<NativeHistoryInputs private>"

    def values(self) -> dict:
        return json.loads(self._json)


def history_inputs(*, attachment_refs: list[dict], settings: dict,
                   incoming_handoff: dict | None = None) -> NativeHistoryInputs:
    # Reuse the owned upload contract, including order and duplicate validation.
    from server.services.attachment_history import validate_refs

    if type(attachment_refs) is not list or type(settings) is not dict:
        _fail()
    refs = validate_refs(attachment_refs) if attachment_refs else []
    values = {"attachment_refs": refs, "settings": settings}
    if incoming_handoff is not None:
        if (type(incoming_handoff) is not dict or set(incoming_handoff) != {
                "handoff_id", "handoff_sha256", "document_sha256"}):
            _fail()
        _uuid(incoming_handoff["handoff_id"])
        from re import fullmatch
        if any(type(incoming_handoff[key]) is not str or not fullmatch(r"[0-9a-f]{64}", incoming_handoff[key])
               for key in ("handoff_sha256", "document_sha256")):
            _fail()
        values["incoming_handoff"] = incoming_handoff
    return NativeHistoryInputs(_encode(values, MAX_INPUTS_BYTES))


def prepare_history_request(req: Any) -> bool:
    enabled = os.getenv("OPENVEGAS_NATIVE_GENERATION_HISTORY", "0") == "1"
    claim = req._native_generation_claim
    required = type(claim) is NativeGenerationClaim and claim.history_revision is not None
    req._native_history_required = required
    req._native_envelope_capture = None
    if not required:
        if req._native_history_inputs is not None:
            _fail()
        return False
    if (not enabled or req.provider != "openrouter" or not req.enable_tools
            or type(req._native_history_inputs) is not NativeHistoryInputs):
        _fail()
    inputs = req._native_history_inputs.values()
    validated = history_inputs(**inputs)
    if validated._json != req._native_history_inputs._json:
        _fail()
    attachment = req._managed_attachment_context
    files = tuple(ref["file_id"] for ref in inputs["attachment_refs"])
    if (inputs.get("incoming_handoff") is not None or getattr(req, "_native_handoff_binding", None) is not None
            or getattr(req, "_native_handoff_continuation_binding", None) is not None):
        from server.services.native_handoff_guard import attachment_file_ids
        files = attachment_file_ids(req)
    if attachment is not None:
        if files != attachment.prepared.file_ids:
            _fail()
    elif files:
        _fail()
    return True


@dataclass(frozen=True, repr=False)
class NativeDispatch:
    payload_json: str = field(repr=False)
    inputs_json: str = field(repr=False)
    request_hash: str

    def __repr__(self):
        return "<NativeDispatch private>"


def capture_dispatch(req: Any, payload: dict) -> NativeDispatch | None:
    if (getattr(req, "_native_handoff_binding", None) is not None
            or getattr(req, "_native_handoff_continuation_binding", None) is not None
            or (type(getattr(req, "_native_history_inputs", None)) is NativeHistoryInputs
                and req._native_history_inputs.values().get("incoming_handoff") is not None)):
        from server.services.native_handoff_guard import (
            validate_bound_request,
            validate_dispatch_deadline,
        )
        validate_bound_request(req, payload=payload)
        validate_dispatch_deadline(req)
        if not getattr(req, "_native_history_required", False):
            _fail()
    if not getattr(req, "_native_history_required", False):
        return None
    if type(req._native_history_inputs) is not NativeHistoryInputs:
        _fail()
    # No transport headers, cookies, API key or HTTP client state are captured.
    from openvegas.gateway.inference import AIGateway

    return NativeDispatch(_encode(payload, MAX_PAYLOAD_BYTES), req._native_history_inputs._json,
                          AIGateway._payload_hash(req))


def continuation_payload(req: Any) -> dict | None:
    claim = getattr(req, "_native_generation_claim", None)
    if type(claim) is not NativeGenerationClaim or claim.previous_request_id is None:
        return None
    if (not req._native_history_required or claim.history_revision is None or claim.history_revision < 1
            or type(claim.continuation_payload_json) is not str):
        _fail()
    payload = _portable_json(claim.continuation_payload_json, MAX_PAYLOAD_BYTES)
    if (type(payload) is not dict or payload.get("model") != req.model
            or payload.get("messages") != req.messages or not req.enable_tools
            or req.provider != "openrouter"):
        _fail()
    return payload


def metering_messages(messages: list[dict]) -> list[dict]:
    """Media validator view only. NEVER send this lossy view to a provider."""
    if type(messages) is not list or not 1 <= len(messages) <= 200:
        _fail()
    projected = []
    for message in messages:
        if type(message) is not dict or message.get("role") not in {"system", "user", "assistant", "tool"}:
            _fail()
        content = message.get("content")
        if content is None and message["role"] == "assistant":
            content = ""
        if not (type(content) is str or (message["role"] == "user" and type(content) is list)):
            _fail()
        projected.append({"role": "user" if message["role"] == "tool" else message["role"], "content": content})
    return projected


def _member_raw(raw: str, name: str) -> str:
    """Return an object member's original JSON token, without re-encoding it."""
    decoder = json.JSONDecoder(parse_float=Decimal, parse_constant=_constant)
    pos = raw.index("{") + 1
    while pos < len(raw):
        while raw[pos] in " \t\r\n,":
            pos += 1
        if raw[pos] == "}":
            break
        key, pos = decoder.raw_decode(raw, pos)
        while raw[pos] in " \t\r\n":
            pos += 1
        if raw[pos] != ":":
            _fail()
        pos += 1
        while raw[pos] in " \t\r\n":
            pos += 1
        start = pos
        _, pos = decoder.raw_decode(raw, pos)
        if key == name:
            return raw[start:pos]
    _fail()


def _public_binding(result: Any) -> str:
    def get(name):
        return result[name] if isinstance(result, dict) else getattr(result, name)

    return _digest(_encode({name: get(name) for name in (
        "text", "input_tokens", "output_tokens", "provider_request_id", "tool_calls", "completion_status",
    )}, MAX_ASSISTANT_BYTES))


@dataclass(frozen=True, repr=False)
class NativeEnvelope:
    assistant_message_json: str = field(repr=False)
    request_payload_json: str = field(repr=False)
    history_inputs_json: str = field(repr=False)
    provider_request_id: str
    response_model: str
    finish_reason: str
    request_hash: str
    public_binding: str

    def __repr__(self):
        return "<NativeEnvelope private>"

    def assistant_message(self) -> dict:
        return _portable_json(self.assistant_message_json, MAX_ASSISTANT_BYTES)

    def request_payload(self) -> dict:
        return _portable_json(self.request_payload_json, MAX_PAYLOAD_BYTES)

    def history_inputs(self) -> dict:
        return _portable_json(self.history_inputs_json, MAX_INPUTS_BYTES)

    @property
    def continuation_safe(self) -> bool:
        return self.continuation_block_reason is None

    @property
    def continuation_block_reason(self) -> str | None:
        if self.finish_reason != "tool_calls":
            return "not_tool_call_turn"
        try:
            message = self.assistant_message()
            self.request_payload()
            self.history_inputs()
            return None if message.get("tool_calls") else "no_pending_tool_calls"
        except NativeHistoryPrecisionError:
            return NativeHistoryPrecisionError.diagnostic_reason
        except ContractError:
            return "native_history_invalid"


def capture_response(req: Any, dispatch: NativeDispatch | None, raw: bytes, result: dict) -> None:
    if dispatch is None:
        return
    try:
        text = raw.decode("utf-8")
        body = _decode(text, MAX_ASSISTANT_BYTES)
        choices_raw = _member_raw(text, "choices")
        choices = body["choices"]
        if type(choices) is not list or len(choices) != 1:
            _fail()
        choice_raw = choices_raw[1:].lstrip()
        message_raw = _member_raw(choice_raw, "message")
        message = _decode(message_raw, MAX_ASSISTANT_BYTES)
        if type(message) is not dict or message.get("role") != "assistant":
            _fail()
        req._native_envelope_capture = NativeEnvelope(
            message_raw, dispatch.payload_json, dispatch.inputs_json, body["id"], body["model"],
            choices[0]["finish_reason"], dispatch.request_hash, _public_binding(result),
        )
    except (UnicodeError, ValueError, TypeError, IndexError, KeyError, RecursionError):
        _fail()


async def _owned_source_tx(tx, *, user_id, run_id, runtime_session_id, request_id,
                           expected_route_command_id=None):
    for value in (user_id, run_id, runtime_session_id, request_id):
        _uuid(value)
    if expected_route_command_id is not None:
        _uuid(expected_route_command_id)
    run = await tx.fetchrow(
        "SELECT * FROM agent_runs WHERE id=$1::uuid AND user_id=$2::uuid FOR UPDATE", run_id, user_id,
    )
    if not run or str(run["runtime_session_id"]) != runtime_session_id:
        _fail()
    route_id = await tx.fetchval(
        "SELECT native_route_command_id FROM inference_requests WHERE id=$1::uuid AND user_id=$2::uuid",
        request_id, user_id,
    )
    if (route_id is None or (expected_route_command_id is not None
                            and str(route_id) != expected_route_command_id)):
        _fail()
    route = await tx.fetchrow("SELECT * FROM inference_route_commands WHERE id=$1::uuid FOR UPDATE", route_id)
    source = await tx.fetchrow("SELECT * FROM inference_requests WHERE id=$1::uuid FOR UPDATE", request_id)
    if (route is None or source is None
            or str(source.get("native_route_command_id")) != str(route_id)):
        _fail()
    scope = await verify_source_scope_tx(tx, run=run, source=source, request_id=request_id)
    if scope is None:
        _fail()
    return run, route, source


async def lock_envelope_owner_tx(tx, *, req, request_id: str) -> None:
    claim = req._native_generation_claim
    if type(claim) is not NativeGenerationClaim:
        _fail()
    _, route, source = await _owned_source_tx(
        tx, user_id=claim.user_id, run_id=claim.scope.run_id,
        runtime_session_id=claim.scope.runtime_session_id, request_id=request_id,
        expected_route_command_id=claim.route_command_id,
    )
    if (str(route["id"]) != claim.route_command_id or source["payload_hash"] != req._native_envelope_capture.request_hash
            or req.account_id != "user:" + claim.user_id or req.provider != "openrouter"):
        _fail()


async def _settled_source_tx(tx, *, source, user_id, provider, model):
    if (not source or source["status"] != "succeeded" or source["response_status"] != 200
            or source["inference_source"] != "wrapper" or str(source["user_id"]) != user_id):
        _fail()
    request_id = str(source["id"])
    preauth = await tx.fetchrow(
        "SELECT * FROM inference_preauthorizations WHERE request_id=$1 AND user_id=$2::uuid FOR UPDATE",
        request_id, user_id,
    )
    usage = await tx.fetchrow("SELECT * FROM inference_usage WHERE request_id=$1::uuid", request_id)
    charge = source["final_charge_v"]
    public = _decode(source["response_body_text"], MAX_ASSISTANT_BYTES + 4096)
    if (charge is None or charge < 0 or not usage or not preauth
            or preauth["status"] != ("settled" if charge > 0 else "refunded")
            or preauth["settled_v"] != charge or usage["v_cost"] != charge
            or usage["actual_cost_usd"] != source["final_provider_cost_usd"]
            or type(public) is not dict or usage["input_tokens"] != public.get("input_tokens")
            or usage["output_tokens"] != public.get("output_tokens")):
        _fail()
    for row in (preauth, usage):
        if (row["provider"] != provider or row["model_id"] != model
                or str(row["user_id"]) != user_id or row["account_id"] != "user:" + user_id):
            _fail()
    return usage


def validate_capture(req, result, *, request_hash: str) -> NativeEnvelope:
    if (getattr(req, "_native_handoff_binding", None) is not None
            or getattr(req, "_native_handoff_continuation_binding", None) is not None
            or (type(getattr(req, "_native_history_inputs", None)) is NativeHistoryInputs
                and req._native_history_inputs.values().get("incoming_handoff") is not None)):
        from server.services.native_handoff_guard import payload_sha256, validate_bound_request
        binding = validate_bound_request(req)
        from server.services.native_handoff_attachments import _hash
        if (type(req._native_envelope_capture) is not NativeEnvelope
                or _hash(req._native_envelope_capture.request_payload()) != payload_sha256(binding)):
            _fail()
    envelope = req._native_envelope_capture
    from openvegas.gateway.inference import AIGateway

    if (type(envelope) is not NativeEnvelope or req.provider != "openrouter"
            or envelope.request_hash != request_hash or AIGateway._payload_hash(req) != request_hash
            or envelope.public_binding != _public_binding(result)
            or type(req._native_history_inputs) is not NativeHistoryInputs
            or envelope.history_inputs_json != req._native_history_inputs._json):
        _fail()
    return envelope


async def persist_native_envelope_tx(tx, *, req, request_id: str, envelope: NativeEnvelope) -> None:
    """Called after success/usage updates in the SAME wallet settlement transaction."""
    claim = req._native_generation_claim
    if type(claim) is not NativeGenerationClaim or type(envelope) is not NativeEnvelope:
        _fail()
    _, route, source = await _owned_source_tx(
        tx, user_id=claim.user_id, run_id=claim.scope.run_id,
        runtime_session_id=claim.scope.runtime_session_id, request_id=request_id,
    )
    await _settled_source_tx(tx, source=source, user_id=claim.user_id, provider=req.provider, model=req.model)
    if (str(route["id"]) != claim.route_command_id or source["provider_request_id"] != envelope.provider_request_id
            or source["payload_hash"] != envelope.request_hash):
        _fail()
    await _insert_private_tx(tx,
        """INSERT INTO native_generation_envelopes
           (request_id,user_id,run_id,runtime_session_id,route_command_id,provider,model_id,
            provider_request_id,response_model,finish_reason,assistant_message_json,assistant_sha256,
            request_payload_json,request_sha256,history_inputs_json,inputs_sha256,public_binding)
           VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::uuid,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)""",
        request_id, claim.user_id, claim.scope.run_id, claim.scope.runtime_session_id, claim.route_command_id,
        req.provider, req.model, envelope.provider_request_id, envelope.response_model, envelope.finish_reason,
        envelope.assistant_message_json, _digest(envelope.assistant_message_json),
        envelope.request_payload_json, _digest(envelope.request_payload_json),
        envelope.history_inputs_json, _digest(envelope.history_inputs_json), envelope.public_binding,
    )


async def _insert_private_tx(tx, query, *args):
    try:
        await tx.execute(query, *args)
    except Exception:  # noqa: BLE001 - SQL diagnostics can include the private failing row.
        _fail()


async def load_native_envelope_tx(tx, *, user_id: str, run_id: str, runtime_session_id: str,
                                  request_id: str, provider: str, model: str,
                                  expected_route_command_id: str | None = None) -> NativeEnvelope:
    """Server-internal only. Caller owns transaction; never return via public API."""
    if provider != "openrouter":
        _fail()
    _, route, source = await _owned_source_tx(tx, user_id=user_id, run_id=run_id,
        runtime_session_id=runtime_session_id, request_id=request_id,
        expected_route_command_id=expected_route_command_id)
    await _settled_source_tx(tx, source=source, user_id=user_id, provider=provider, model=model)
    row = await tx.fetchrow("SELECT * FROM native_generation_envelopes WHERE request_id=$1::uuid", request_id)
    if not row or any(str(row[key]) != value for key, value in {
        "user_id": user_id, "run_id": run_id, "runtime_session_id": runtime_session_id,
        "route_command_id": str(route["id"]), "provider": provider, "model_id": model,
        "provider_request_id": source["provider_request_id"],
    }.items()):
        _fail()
    for data, digest, bound in (("assistant_message_json", "assistant_sha256", MAX_ASSISTANT_BYTES),
                               ("request_payload_json", "request_sha256", MAX_PAYLOAD_BYTES),
                               ("history_inputs_json", "inputs_sha256", MAX_INPUTS_BYTES)):
        _decode(row[data], bound)
        if _digest(row[data]) != row[digest]:
            _fail()
    # Public replay contains normalized calls only; validate it without adding private fields.
    public = json.loads(source["response_body_text"])
    if not public.get("tool_calls"):
        public["tool_calls"] = None
    if _public_binding(public) != row["public_binding"]:
        _fail()
    return NativeEnvelope(row["assistant_message_json"], row["request_payload_json"], row["history_inputs_json"],
                          row["provider_request_id"], row["response_model"], row["finish_reason"],
                          source["payload_hash"], row["public_binding"])
