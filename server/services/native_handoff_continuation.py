"""Private consumed-handoff continuation preflight, NOT gateway authorization.

Enter verification before any run/route/gateway locks; provenance prelocks the
graph and its routes. The processing route must already exist. No reservation,
consumption, settlement, provider call or public activation occurs here. Gateway
integration must latch this distinct binding and validate it after awaits and at
transport/settlement; the revision-zero binding is deliberately not populated.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from openvegas.agent import native_handoff_store as store
from openvegas.agent.native_envelope import NativeHistoryInputs, load_native_envelope_tx
from openvegas.agent.native_generation import (
    NativeGenerationClaim,
    registration,
    require_fresh_projection_tx,
    scope_document,
    stored_scope,
)
from openvegas.agent.native_handoff_document import MAX_GENERATIONS
from openvegas.agent.native_history import load_native_tool_results_tx
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.flags import features
from openvegas.gateway.catalog import validate_catalog_entry
from openvegas.gateway.inference import InferenceRequest
from openvegas.gateway.openrouter import MAX_REQUEST_BYTES, build_payload
from openvegas.gateway.providers import (
    get_model_review,
    model_capabilities,
    model_switch_enabled,
    validate_reasoning_effort,
)
from server.services.attachment_history import references, validate_refs
from server.services.file_uploads import FileUploadService
from server.services.inference_replay import _envelope
from server.services.native_handoff_attachments import _hash
from server.services.native_handoff_context import _combine
from server.services.native_handoff_dispatch import _claim_hash, _media_gates, _request_hash
from server.services.native_handoff_provenance import verify_consumed_handoff_tx
from server.services.native_handoff_service import _ReviewUploads
from server.services.openrouter_attachments import prepare_owned_attachment_blocks

_CONFIG = ("provider", "model_id", "enabled", "max_tokens", "cost_input_per_1m",
           "cost_output_per_1m", "v_price_input_per_1m", "v_price_output_per_1m")
_SETTINGS = ("provider", "model", "enable_tools", "enable_web_search", "reasoning_effort", "max_tokens")


def _fail():
    raise ContractError(APIErrorCode.HANDOFF_BLOCKED,
        "Retained handoff continuation could not be verified; nothing was sent or changed.") from None


def _wire(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
        _fail()
    return raw


@dataclass(frozen=True, repr=False)
class HandoffContinuationBinding:
    handoff_id: str
    handoff_sha256: str
    document_sha256: str
    claim_sha256: str
    request_sha256: str
    payload_json: str = field(repr=False)
    input_sha256: str
    review_sha256: str
    file_ids: tuple[str, ...]
    attachments: Any = field(repr=False)
    expires_at: datetime

    def __repr__(self):
        return "<HandoffContinuationBinding private>"

    @property
    def payload_sha256(self):
        """Same canonical object hash as first-dispatch envelope capture."""
        return _hash(json.loads(self.payload_json))

    def provenance(self):
        return {"handoff_id": self.handoff_id, "handoff_sha256": self.handoff_sha256,
                "document_sha256": self.document_sha256}


@dataclass(frozen=True, repr=False)
class PreparedHandoffContinuation:
    request: InferenceRequest = field(repr=False)
    binding: HandoffContinuationBinding = field(repr=False)

    def __repr__(self):
        return "<PreparedHandoffContinuation private>"


def _claim(req):
    claim = req._native_generation_claim
    if (type(req) is not InferenceRequest or type(claim) is not NativeGenerationClaim
            or type(claim.history_revision) is not int or not 1 <= claim.history_revision < MAX_GENERATIONS
            or type(claim.continuation_payload_json) is not str or type(claim.history_inputs_json) is not str
            or req.account_id != "user:" + claim.user_id or req.idempotency_key != claim.gateway_key
            or req.provider != "openrouter" or req.enable_tools is not True
            or type(req.enable_web_search) is not bool or type(req.max_tokens) is not int
            or req._native_handoff_binding is not None):
        _fail()
    for ident in (claim.user_id, claim.route_command_id, claim.previous_request_id, claim.owner_token):
        store._uuid(ident)
    store._scope(claim.scope)
    return claim


def _review(req):
    store._enabled()
    if not features().get("global_enabled", False) or not model_switch_enabled():
        _fail()
    config = req._managed_model_config
    if (type(config) is not dict or config.get("provider") != req.provider
            or config.get("model_id") != req.model):
        _fail()
    validate_catalog_entry(req.provider, req.model, config)
    caps, review = model_capabilities(req.provider, req.model), get_model_review(req.provider, req.model)
    if caps.get("tools") is not True:
        _fail()
    validate_reasoning_effort(req.provider, req.model, req.reasoning_effort)
    _media_gates(req)
    return (_hash([review, caps, {key: config.get(key) for key in _CONFIG}]),
            store._date(datetime.fromisoformat(review["expires_at"])), caps)


def validate_continuation(request, *, expected, payload=None, wire_bytes=None):
    """Identity and exact payload check. Not a substitute for tx reauthorization."""
    try:
        claim = _claim(request)
        if (type(expected) is not HandoffContinuationBinding
                or getattr(request, "_native_handoff_continuation_binding", None) is not expected
                or _claim_hash(claim) != expected.claim_sha256
                or _request_hash(request) != expected.request_sha256
                or request._native_history_required is not True
                or type(request._native_history_inputs) is not NativeHistoryInputs
                or request._native_history_inputs._json != claim.history_inputs_json
                or store._sha(claim.history_inputs_json) != expected.input_sha256
                or request._native_history_inputs.values().get("incoming_handoff") != expected.provenance()
                or claim.continuation_payload_json != expected.payload_json
                or request._managed_attachment_context is not expected.attachments
                or (payload is not None and _wire(payload) != expected.payload_json)
                or (wire_bytes is not None and
                    (type(wire_bytes) is not bytes or wire_bytes != expected.payload_json.encode("utf-8")))):
            _fail()
        return expected
    except Exception:  # noqa: BLE001 - no private parser/request values in diagnostics.
        _fail()


def validate_continuation_deadline(request, *, expected):
    """Call unconditionally after awaits and immediately before transport."""
    try:
        validate_continuation(request, expected=expected)
        if datetime.now(UTC) >= expected.expires_at:
            _fail()
        digest, expiry, caps = _review(request)
        if digest != expected.review_sha256 or expected.expires_at > expiry:
            _fail()
        validate_continuation(request, expected=expected,
            payload=build_payload(request, request._managed_model_config, caps))
        return expected
    except Exception:  # noqa: BLE001
        _fail()


async def _history(tx, req):
    claim = _claim(req)
    incoming = await verify_consumed_handoff_tx(tx, user_id=claim.user_id, scope=claim.scope)
    run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid AND user_id=$2::uuid",
                            claim.scope.run_id, claim.user_id)
    if (not run or str(run.get("native_handoff_id")) != incoming.handoff_id
            or str(run.get("native_generation_claim_id")) != claim.route_command_id
            or run.get("native_history_revision") != claim.history_revision
            or scope_document(claim.scope, run) != claim.scope_json):
        _fail()
    await require_fresh_projection_tx(tx, run=run, scope=claim.scope, continuing=True)
    # Provenance already locked every route under these run locks. Do not acquire
    # a caller-selected route outside that graph after its gateway locks.
    routes = await tx.fetch("SELECT * FROM inference_route_commands WHERE native_run_id=$1::uuid "
        "ORDER BY native_history_revision,id LIMIT $2", claim.scope.run_id, MAX_GENERATIONS + 1)
    if len(routes) != claim.history_revision + 1:
        _fail()
    previous_id = None
    for ordinal, route in enumerate(routes):
        ownership = stored_scope(route)
        parent = str(route["previous_native_request_id"]) if route["previous_native_request_id"] else None
        if (str(route["user_id"]) != claim.user_id or str(route["native_run_id"]) != claim.scope.run_id
                or route["native_history_revision"] != ordinal or parent != previous_id
                or ownership["registration"] != registration(run)
                or ownership["scope"]["run_id"] != claim.scope.run_id
                or ownership["scope"]["runtime_session_id"] != claim.scope.runtime_session_id):
            _fail()
        body = _envelope(route, user_id=claim.user_id, key=route["idempotency_key"], digest=route["payload_hash"])
        if ordinal < claim.history_revision:
            if body["state"] != "completed" or body["gateway_request_id"] != str(route["gateway_request_id"]):
                _fail()
            previous_id = str(route["gateway_request_id"])
            if ordinal == 0 and previous_id != incoming.first_request_id:
                _fail()
        elif (str(route["id"]) != claim.route_command_id or previous_id != claim.previous_request_id
                or body["state"] != "processing" or route["gateway_request_id"] is not None
                or route["payload_hash"] != claim.command_hash or body["owner_token"] != claim.owner_token
                or body["gateway_key"] != claim.gateway_key or ownership != json.loads(claim.scope_json)):
            _fail()
    envelope = await load_native_envelope_tx(tx, user_id=claim.user_id, run_id=claim.scope.run_id,
        runtime_session_id=claim.scope.runtime_session_id, request_id=claim.previous_request_id,
        provider=req.provider, model=req.model, expected_route_command_id=str(routes[-2]["id"]))
    if envelope.finish_reason != "tool_calls" or not envelope.continuation_safe:
        _fail()
    first = envelope if claim.previous_request_id == incoming.first_request_id else await load_native_envelope_tx(
        tx, user_id=claim.user_id, run_id=claim.scope.run_id, runtime_session_id=claim.scope.runtime_session_id,
        request_id=incoming.first_request_id, provider=req.provider, model=req.model,
        expected_route_command_id=str(routes[0]["id"]))
    inputs = envelope.history_inputs()
    if (envelope.history_inputs_json != first.history_inputs_json
            or claim.history_inputs_json != envelope.history_inputs_json
            or type(req._native_history_inputs) is not NativeHistoryInputs
            or req._native_history_inputs._json != claim.history_inputs_json
            or set(inputs) != {"settings", "attachment_refs", "incoming_handoff"}
            or inputs["incoming_handoff"] != incoming.provenance()
            or any(type(inputs["settings"].get(key)) is not type(getattr(req, key))
                   or inputs["settings"].get(key) != getattr(req, key) for key in _SETTINGS)):
        _fail()
    refs = validate_refs(inputs["attachment_refs"]) if inputs["attachment_refs"] else []
    if inputs["settings"].get("attachments") != [ref["file_id"] for ref in refs]:
        _fail()
    source = await tx.fetchrow("SELECT * FROM inference_requests WHERE id=$1::uuid AND user_id=$2::uuid",
                               claim.previous_request_id, claim.user_id)
    assistant = envelope.assistant_message()
    results = await load_native_tool_results_tx(tx, run=run, source=source,
        request_id=claim.previous_request_id, assistant_message=assistant)
    if not results or len(results) != len(assistant["tool_calls"]):
        _fail()
    payload = envelope.request_payload()
    payload["messages"] = [*payload["messages"], assistant, *results]
    if (len(payload["messages"]) > 200 or _wire(payload) != claim.continuation_payload_json
            or _wire(req.messages) != _wire(payload["messages"])):
        _fail()
    return incoming, inputs


def _uploads_hash(uploads):
    return _hash([{**references([row])[0], **{key: row.get(key) for key in
        ("filename", "mime_type", "size_bytes")}} for _, row in sorted(uploads.rows.items())])


async def _assemble(tx, req, *, deadline):
    incoming, inputs = await _history(tx, req)
    claim = _claim(req)
    req._native_history_required = True
    req._managed_attachment_context = req._managed_web_context = req._managed_openrouter_dispatch = None
    uploads = _ReviewUploads(tx, FileUploadService(None))
    await uploads.load(user_id=claim.user_id, document=incoming.document, current_refs=inputs["attachment_refs"])
    uploaded = _uploads_hash(uploads)
    config = await tx.fetchrow("SELECT * FROM provider_catalog WHERE provider=$1 AND model_id=$2 FOR SHARE",
                               req.provider, req.model)
    req._managed_model_config = dict(config) if config else None
    reviewed, review_expiry, caps = _review(req)
    review = copy.deepcopy(get_model_review(req.provider, req.model))
    groups = [task["attachment_refs"] for task in incoming.document.values()["tasks"]]
    groups.append(inputs["attachment_refs"])
    batches = []
    for refs in groups:
        if not refs:
            continue
        ids = [ref["file_id"] for ref in refs]
        rows = await uploads.resolve_uploaded_for_inference(user_id=claim.user_id, file_ids=ids)
        if references(rows) != refs:
            _fail()
        batches.append(await prepare_owned_attachment_blocks(user_id=claim.user_id, file_ids=ids,
            model_id=req.model, model_config=req._managed_model_config, model_review=review, upload_service=uploads))
    req._managed_attachment_context = _combine(batches)
    payload = build_payload(req, req._managed_model_config, caps)
    if _wire(payload) != claim.continuation_payload_json:
        _fail()
    if req.enable_web_search:
        web = req._managed_web_context.prepared.snapshot
        review_expiry = min(review_expiry, web.prices.expires_at, web.execution.expires_at)
    await uploads.load(user_id=claim.user_id, document=incoming.document, current_refs=inputs["attachment_refs"])
    if uploaded != _uploads_hash(uploads):
        _fail()
    file_expiry = await tx.fetchval("SELECT min(expires_at) FROM chat_file_uploads "
        "WHERE user_id=$1::uuid AND id=ANY($2::uuid[])", claim.user_id, sorted(uploads.rows)) if uploads.rows else None
    if uploads.rows and file_expiry is None:
        _fail()
    expires = min(deadline, review_expiry, *([store._date(file_expiry)] if file_expiry is not None else []))
    binding = HandoffContinuationBinding(**incoming.provenance(), claim_sha256=_claim_hash(claim),
        request_sha256=_request_hash(req), payload_json=claim.continuation_payload_json,
        input_sha256=store._sha(claim.history_inputs_json), review_sha256=reviewed,
        file_ids=tuple(ref["file_id"] for refs in groups for ref in refs),
        attachments=req._managed_attachment_context, expires_at=expires)
    req._native_handoff_continuation_binding = binding
    validate_continuation_deadline(req, expected=binding)
    return PreparedHandoffContinuation(req, binding)


async def prepare_continuation(db, *, request):
    """Snapshot before awaiting; authenticate and prepare without dispatch."""
    try:
        if getattr(request, "_native_handoff_continuation_binding", None) is not None:
            _fail()
        request = copy.deepcopy(request)
        _claim(request)
        deadline = datetime.now(UTC) + timedelta(minutes=10)
        async with db.transaction() as tx:
            prepared = await _assemble(tx, request, deadline=deadline)
        validate_continuation_deadline(prepared.request, expected=prepared.binding)
        return prepared
    except Exception:  # noqa: BLE001 - transaction/parser exceptions may contain private history.
        _fail()


async def verify_continuation_tx(tx, request, *, expected):
    """Reauthorize before linkage/wallet in the caller's reservation transaction.

    Does not replace or extend the latched binding. Caller must roll back on any
    exception and perform final deadline/payload checks after later awaits.
    """
    try:
        validate_continuation_deadline(request, expected=expected)
        snapshot = copy.deepcopy(request)
        fresh = await _assemble(tx, snapshot, deadline=expected.expires_at)
        if fresh.binding != expected:
            _fail()
        validate_continuation_deadline(request, expected=expected)
        return expected
    except Exception:  # noqa: BLE001
        _fail()
