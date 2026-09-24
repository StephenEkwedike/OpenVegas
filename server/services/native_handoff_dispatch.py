"""Private first-dispatch preparation; not a public model-switch endpoint.

A stored preview/confirmation alone is never a dispatch grant. The gateway
reconstructs and verifies the complete payload in its reservation transaction,
then records consumption before the provider transport can run. The public
route remains closed until continuation and CLI integration are complete.
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from openvegas.agent import native_handoff_store as store
from openvegas.agent.native_envelope import NativeHistoryInputs, history_inputs
from openvegas.agent.native_generation import NativeGenerationClaim
from openvegas.capabilities import resolve_capability
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.flags import features
from openvegas.gateway.openrouter import build_payload
from openvegas.gateway.providers import get_model_review, model_capabilities, model_switch_enabled
from server.services.attachment_history import references
from server.services.file_uploads import FileUploadService
from server.services.native_handoff_attachments import _hash, _resolve
from server.services.native_handoff_context import prepare_destination_request
from server.services.native_handoff_service import _review_target, _ReviewUploads, _selection
from server.services.openrouter_attachment_request import prepare_attachment_request


def _fail():
    raise ContractError(APIErrorCode.HANDOFF_BLOCKED,
        "The confirmed model handoff no longer matches this request; nothing was sent.") from None


@dataclass(frozen=True, repr=False)
class HandoffDispatchBinding:
    handoff_id: str
    handoff_sha256: str
    document_sha256: str
    target_json: str = field(repr=False)
    claim_sha256: str
    request_sha256: str
    payload_sha256: str
    input_sha256: str
    inherited_file_ids: tuple[str, ...]
    base_messages_json: str = field(repr=False)
    base_inputs_json: str = field(repr=False)
    base_attachments: Any = field(repr=False)
    expires_at: datetime

    def __repr__(self):
        return "<HandoffDispatchBinding private>"

    def provenance(self):
        return {"handoff_id": self.handoff_id, "handoff_sha256": self.handoff_sha256,
                "document_sha256": self.document_sha256}

    def proof(self):
        return {"version": 1, **self.provenance(), "target_sha256": store._sha(self.target_json),
                "claim_sha256": self.claim_sha256, "request_sha256": self.request_sha256,
                "payload_sha256": self.payload_sha256, "input_sha256": self.input_sha256,
                "authorization_expires_at": self.expires_at.isoformat()}


def _claim(req):
    claim = req._native_generation_claim
    if (type(claim) is not NativeGenerationClaim or claim.history_revision != 0
            or claim.previous_request_id is not None or claim.continuation_payload_json is not None
            or req.account_id != "user:" + claim.user_id or req.idempotency_key != claim.gateway_key
            or req.provider != "openrouter" or req.enable_tools is not True):
        _fail()
    return claim


def _request_hash(req):
    from openvegas.gateway.inference import AIGateway
    return AIGateway._payload_hash(req)


def _claim_hash(claim):
    return _hash({**asdict(claim), "scope": claim.scope.model_dump(mode="json")})


def validate_bound_request(req, *, payload=None, expected=None):
    binding = getattr(req, "_native_handoff_binding", None)
    if type(binding) is not HandoffDispatchBinding:
        _fail()
    if expected is not None and binding is not expected:
        _fail()
    claim = _claim(req)
    if (_claim_hash(claim) != binding.claim_sha256
            or _request_hash(req) != binding.request_sha256
            or type(req._native_history_inputs) is not NativeHistoryInputs
            or store._sha(req._native_history_inputs._json) != binding.input_sha256
            or req._native_history_inputs.values().get("incoming_handoff") != binding.provenance()
            or (payload is not None and _hash(payload) != binding.payload_sha256)):
        _fail()
    return binding


def validate_dispatch_deadline(req, *, expected=None):
    store._enabled()
    binding = validate_bound_request(req, expected=expected)
    if datetime.now(UTC) >= binding.expires_at:
        _fail()
    if not features().get("global_enabled", False) or not model_switch_enabled():
        _fail()
    caps = model_capabilities(req.provider, req.model)
    config = req._managed_model_config
    if (type(config) is not dict or _hash({"review": get_model_review(req.provider, req.model),
            "capabilities": caps, "catalog": {name: config.get(name) for name in (
                "provider", "model_id", "enabled", "max_tokens", "cost_input_per_1m",
                "cost_output_per_1m", "v_price_input_per_1m", "v_price_output_per_1m")}})
            != store._target(binding.target_json).review_fingerprint):
        _fail()
    _media_gates(req)


def _media_gates(req):
    context = req._managed_attachment_context
    names = []
    if req.enable_web_search:
        names.append("web_search")
    if req.reasoning_effort is not None:
        names.append("reasoning_controls")
    if context is not None:
        from server.services.dependencies import current_flags
        if not current_flags().files_enabled:
            _fail()
        names.append("file_upload")
        if any(block.get("type") == "image_url" for block in context.prepared.blocks):
            names.append("image_input")
    caps = model_capabilities(req.provider, req.model)
    if any(caps.get(name) is not True or not resolve_capability(
            req.provider, req.model, name, user_id=req._native_generation_claim.user_id) for name in names):
        _fail()


def _validate_intent(req, inputs):
    from server.services.inference_replay import command_fingerprint
    claim = _claim(req)
    settings = inputs["settings"]
    command = {name: settings.get(name) for name in (
        "prompt", "provider", "model", "enable_tools", "enable_web_search", "reasoning_effort",
        "attachments", "native_user_text")}
    command.update(native_history=True, native_scope=claim.scope.model_dump(mode="json"),
                   persist_context=False, thread_id=None, conversation_mode="ephemeral")
    if settings.get("native_handoff") is not None:
        command.update(native_handoff=settings["native_handoff"], max_tokens=settings["max_tokens"])
    if command_fingerprint(command) != claim.command_hash:
        _fail()
    content = req.messages[-1]["content"]
    actual = content[0].get("text") if isinstance(content, list) and content else content
    if actual != settings["prompt"] or settings["attachments"] != [ref["file_id"] for ref in inputs["attachment_refs"]]:
        _fail()


async def _assemble(tx, *, req, handoff_id, handoff_sha256):
    claim = _claim(req)
    record = await store.lock_handoff_runs_tx(tx, user_id=claim.user_id, handoff_id=handoff_id)
    if (record.handoff_sha256 != handoff_sha256 or record.destination_scope != claim.scope
            or record.first_request_id is not None):
        _fail()
    # Reserve/lock the destination route before source assembly takes gateway
    # locks. This transaction must begin here, before any gateway/wallet locks.
    await tx.fetchrow("SELECT id FROM inference_route_commands WHERE id=$1::uuid FOR UPDATE",
                      claim.route_command_id)
    runs = await store._runs(tx, claim.user_id, record.source_scope, claim.scope)
    await store._fresh_source(tx, record, runs)
    await store._now_unexpired(tx, record)
    if type(req._native_history_inputs) is not NativeHistoryInputs:
        _fail()
    inputs = req._native_history_inputs.values()
    if set(inputs) != {"attachment_refs", "settings"}:
        _fail()
    settings = inputs["settings"]
    _validate_intent(req, inputs)
    if settings.get("native_handoff") is not None and settings["native_handoff"] != {
            "handoff_id": record.handoff_id, "handoff_sha256": record.handoff_sha256}:
        _fail()
    for key in ("provider", "model", "enable_tools", "enable_web_search", "reasoning_effort", "max_tokens"):
        if settings.get(key) != getattr(req, key):
            _fail()
    target = record.target
    if any(getattr(target, key) != getattr(req, key) for key in (
            "provider", "model", "enable_tools", "enable_web_search", "reasoning_effort", "max_tokens")):
        _fail()
    # Lock *all* requested files in one global order, and bound raw aggregate
    # bytes before parsing. Historical and current file association stays intact.
    service = FileUploadService(None)
    uploads = _ReviewUploads(tx, service)
    await uploads.load(user_id=claim.user_id, document=record.document, current_refs=inputs["attachment_refs"])
    reviewed = await _review_target(tx, user_id=claim.user_id, document=record.document,
                                    selection=_selection(record), upload_service=service)
    store._compare(record, reviewed, handoff_sha256)
    config = dict(await tx.fetchrow("SELECT * FROM provider_catalog WHERE provider=$1 AND model_id=$2 FOR SHARE",
                                    req.provider, req.model))
    current = copy.copy(req)
    current._managed_web_context = None
    current.messages = copy.deepcopy(req.messages)
    refs = inputs["attachment_refs"]
    if refs:
        ids = [ref["file_id"] for ref in refs]
        rows = await uploads.resolve_uploaded_for_inference(user_id=claim.user_id, file_ids=ids)
        if references(rows) != refs:
            _fail()
        messages, ctx, current_refs = await prepare_attachment_request(
            history=current.messages[:-1], prompt=settings["prompt"], file_ids=ids,
            user_id=claim.user_id, model_id=req.model, model_config=config, upload_service=uploads)
        if messages != current.messages or current_refs != refs:
            _fail()
        current._managed_attachment_context = ctx
    elif current._managed_attachment_context is not None:
        _fail()
    _media_gates(current)
    retained = await _resolve(document=record.document, user_id=claim.user_id,
        model_id=req.model, model_config=config, upload_service=uploads)
    if _hash(asdict(retained.receipt)) != target.attachment_review_sha256:
        _fail()
    composed = await prepare_destination_request(request=current, document=record.document,
        preview=retained.receipt, user_id=claim.user_id, model_config=config,
        capabilities=model_capabilities(req.provider, req.model), upload_service=uploads)
    # Expiry can pass while parsing; recheck under the same locks before return.
    await uploads.load(user_id=claim.user_id, document=record.document, current_refs=refs)
    await store._now_unexpired(tx, record)
    all_ids = sorted(uploads.rows)
    file_expiry = await tx.fetchval("SELECT min(expires_at) FROM chat_file_uploads "
        "WHERE user_id=$1::uuid AND id=ANY($2::uuid[])", claim.user_id, all_ids) if all_ids else None
    expires = min(record.expires_at, record.target.review_expires_at,
                  *([file_expiry] if file_expiry is not None else []))
    if expires <= datetime.now(UTC):
        _fail()
    provenance = {"handoff_id": record.handoff_id, "handoff_sha256": record.handoff_sha256,
                  "document_sha256": record.document.sha256}
    composed._native_history_inputs = history_inputs(**inputs, incoming_handoff=provenance)
    payload = build_payload(composed, config, model_capabilities(req.provider, req.model))
    if _hash(payload["tools"]) != target.tool_definitions_sha256:
        _fail()
    binding = HandoffDispatchBinding(record.handoff_id, record.handoff_sha256, record.document.sha256,
        target.to_json(), _claim_hash(claim), _request_hash(composed), _hash(payload),
        store._sha(composed._native_history_inputs._json),
        tuple(ref["file_id"] for task in record.document.values()["tasks"] for ref in task["attachment_refs"]),
        json.dumps(req.messages, ensure_ascii=False, separators=(",", ":")),
        req._native_history_inputs._json, req._managed_attachment_context, expires)
    composed._native_handoff_binding = binding
    return composed


async def prepare_first_dispatch(db, *, request, handoff_id, handoff_sha256):
    """Internal-only preflight. Returns a snapshot, never reserves or sends."""
    store._enabled()
    request = copy.copy(request)
    request.messages = copy.deepcopy(request.messages)
    if getattr(request, "_native_handoff_binding", None) is not None:
        _fail()
    try:
        async with db.transaction() as tx:
            return await _assemble(tx, req=request, handoff_id=handoff_id, handoff_sha256=handoff_sha256)
    except Exception:  # noqa: BLE001 - private SQL/parser errors may contain customer input.
        _fail()


async def verify_first_dispatch_tx(tx, req, *, expected):
    """Before any wallet reservation, reauthorize source, files and exact payload."""
    store._enabled()
    try:
        original = validate_bound_request(req, expected=expected)
        base = copy.copy(req)
        base.messages = json.loads(original.base_messages_json)
        base._native_history_inputs = NativeHistoryInputs(original.base_inputs_json)
        base._managed_attachment_context = original.base_attachments
        base._managed_web_context = base._native_handoff_binding = None
        fresh = await _assemble(tx, req=base, handoff_id=original.handoff_id,
                                handoff_sha256=original.handoff_sha256)
        if fresh._native_handoff_binding != original:
            _fail()
        validate_bound_request(req, payload=build_payload(req, req._managed_model_config,
                                       model_capabilities(req.provider, req.model)), expected=expected)
    except Exception:  # noqa: BLE001 - private SQL/parser errors may contain customer input.
        _fail()


async def consume_first_dispatch_tx(tx, req, request_id, *, expected):
    """Must share the gateway's original reservation transaction, before commit."""
    binding = validate_bound_request(req, expected=expected)
    claim = req._native_generation_claim
    proof = {**binding.proof(), "user_id": claim.user_id, "destination_run_id": claim.scope.run_id,
             "destination_runtime_session_id": claim.scope.runtime_session_id,
             "route_command_id": claim.route_command_id, "request_id": request_id}
    await store.consume_first_generation_tx(tx, user_id=claim.user_id,
        handoff_id=binding.handoff_id, handoff_sha256=binding.handoff_sha256,
        target=store._target(binding.target_json), route_command_id=req._native_generation_claim.route_command_id,
        request_id=request_id, dispatch_proof=proof)
