"""Owned preview/confirmation, without inference or client-supplied history.

This internal coordinator is not yet exposed by a route. A committed destination
is not dispatch authority: inference must still bind and consume it atomically
with its exact first request. Do not enable a caller until that integration exists.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from functools import wraps
from typing import Any

from openvegas.agent import native_handoff_store as store
from openvegas.agent.native_handoff_source import assemble_task_tx
from openvegas.capabilities import resolve_capability
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope
from openvegas.flags import features
from openvegas.gateway.catalog import validate_catalog_entry
from openvegas.gateway.inference import InferenceRequest
from openvegas.gateway.openrouter import build_payload
from openvegas.gateway.providers import (
    get_model_review,
    model_capabilities,
    model_switch_enabled,
    resolve_provider_api_key,
    validate_reasoning_effort,
)
from server.services.file_uploads import FileUploadService
from server.services.native_handoff_attachments import _hash, _resolve
from server.services.native_handoff_context import render_history
from server.services.openrouter_attachments import MAX_TOTAL_BYTES


def _fail():
    raise ContractError(APIErrorCode.HANDOFF_BLOCKED,
                        "Model handoff could not be verified; the current selection was not changed.") from None


def _private(fn):
    @wraps(fn)
    async def guarded(*args, **kwargs):
        store._enabled()
        try:
            return await fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 - SQL/parser/configuration errors can contain private values.
            _fail()
    return guarded


@dataclass(frozen=True)
class HandoffSelection:
    model: str
    enable_web_search: bool = False
    reasoning_effort: str | None = None
    max_tokens: int = 4096

    def __post_init__(self):
        # Reuse the storage contract's strict setting validation; these dummy
        # hashes are never stored or treated as a review.
        store.HandoffTarget("openrouter", self.model, True, self.enable_web_search,
                            self.reasoning_effort, self.max_tokens, "0" * 64,
                            "0" * 64, "0" * 64, datetime.fromisoformat("2000-01-01T00:00:00+00:00"))


@dataclass(frozen=True)
class HandoffPreview:
    handoff_id: str
    handoff_sha256: str
    selection: HandoffSelection
    expires_at: datetime
    task_count: int
    file_count: int
    unique_file_count: int
    observation_count: int
    destination_scope: NativeInferenceScope | None = None


def _selection(record):
    target = record.target
    return HandoffSelection(target.model, target.enable_web_search,
                            target.reasoning_effort, target.max_tokens)


def _public(record):
    tasks = record.document.values()["tasks"]
    files = [ref["file_id"] for task in tasks for ref in task["attachment_refs"]]
    return HandoffPreview(record.handoff_id, record.handoff_sha256, _selection(record),
                          record.expires_at, len(tasks), len(files), len(set(files)),
                          sum(len(g["observations"]) for task in tasks for g in task["generations"]),
                          record.destination_scope)


class _ReviewUploads:
    """One-transaction, sorted locks, with task-local order restored on lookup."""
    def __init__(self, tx, service):
        self.tx, self.service, self.rows = tx, service, {}

    async def load(self, *, user_id, document, current_refs=None):
        occurrences = [ref["file_id"] for task in document.values()["tasks"] for ref in task["attachment_refs"]]
        if current_refs:
            from server.services.attachment_history import validate_refs
            occurrences.extend(ref["file_id"] for ref in validate_refs(current_refs))
        if len(occurrences) > 12:
            _fail()
        self.rows = {}
        total = 0
        for ident in sorted(set(occurrences)):
            rows = await self.service.resolve_uploaded_for_inference(user_id=user_id, file_ids=[ident], tx=self.tx)
            if len(rows) != 1 or rows[0]["file_id"] != ident:
                _fail()
            total += len(rows[0]["content_bytes"]) * occurrences.count(ident)
            if total > MAX_TOTAL_BYTES:
                _fail()
            self.rows[ident] = rows[0]
        self.user_id = user_id

    async def resolve_uploaded_for_inference(self, *, user_id, file_ids):
        if user_id != self.user_id:
            _fail()
        return [self.rows[ident] for ident in file_ids]


async def _review_target(tx, *, user_id, document, selection, upload_service):
    if type(selection) is not HandoffSelection or not model_switch_enabled():
        _fail()
    selection.__post_init__()
    # Keep the exact catalog row stable until this transaction commits. No
    # provider credential or reviewed target is accepted from the customer.
    row = await tx.fetchrow("SELECT * FROM provider_catalog WHERE provider=$1 AND model_id=$2 FOR SHARE",
                            "openrouter", selection.model)
    config = dict(row) if row else None
    validate_catalog_entry("openrouter", selection.model, config)
    await resolve_provider_api_key(tx, "openrouter")
    review = get_model_review("openrouter", selection.model)
    review_hash = _hash(review)
    caps = model_capabilities("openrouter", selection.model)
    if caps.get("tools") is not True or not features().get("global_enabled", False):
        _fail()
    required = []
    if selection.enable_web_search:
        required.append("web_search")
    if selection.reasoning_effort is not None:
        required.append("reasoning_controls")
    if any(task["attachment_refs"] for task in document.values()["tasks"]):
        from server.services.dependencies import current_flags
        if not current_flags().files_enabled:
            _fail()
        required.append("file_upload")
    if any(caps.get(name) is not True or not resolve_capability(
            "openrouter", selection.model, name, user_id=user_id) for name in required):
        _fail()
    validate_reasoning_effort("openrouter", selection.model, selection.reasoning_effort)
    uploads = _ReviewUploads(tx, upload_service)
    await uploads.load(user_id=user_id, document=document)
    content = await _resolve(document=document, user_id=user_id, model_id=selection.model,
                             model_config=config, upload_service=uploads)
    if any(block.get("type") == "image_url" for prepared in content.by_task if prepared
           for block in prepared.blocks):
        required.append("image_input")
        if caps.get("image_input") is not True or not resolve_capability(
                "openrouter", selection.model, "image_input", user_id=user_id):
            _fail()
    messages, attachment_context = render_history(document, content)
    # This only bounds retained context. The actual next user input and current
    # server instructions must be checked again before any wallet reservation.
    request = InferenceRequest(account_id="user:" + user_id, provider="openrouter",
        model=selection.model, messages=messages, max_tokens=selection.max_tokens,
        enable_tools=True, enable_web_search=selection.enable_web_search,
        reasoning_effort=selection.reasoning_effort,
        idempotency_key="handoff-preflight:" + document.sha256)
    request._managed_attachment_context = attachment_context
    payload = build_payload(request, config, caps)
    # Parsing may await. Reread expiry under the same locks, not a pooled
    # connection or a cached authorization; no writes/provider dispatch happen.
    await uploads.load(user_id=user_id, document=document)
    if (_hash(get_model_review("openrouter", selection.model)) != review_hash
            or not model_switch_enabled() or not features().get("global_enabled", False)
            or any(not resolve_capability("openrouter", selection.model, name, user_id=user_id)
                   for name in required)):
        _fail()
    return store.HandoffTarget(
        "openrouter", selection.model, True, selection.enable_web_search,
        selection.reasoning_effort, selection.max_tokens, _hash(payload["tools"]),
        _hash(asdict(content.receipt)),
        _hash({"review": review, "capabilities": caps, "catalog": {
            name: config.get(name) for name in ("provider", "model_id", "enabled", "max_tokens",
                "cost_input_per_1m", "cost_output_per_1m", "v_price_input_per_1m", "v_price_output_per_1m")}}),
        datetime.fromisoformat(review["expires_at"]),
    )


class NativeHandoffService:
    def __init__(self, db: Any):
        self.db, self.upload_service = db, FileUploadService(db)

    @_private
    async def prepare(self, *, user_id: str, source_scope: NativeInferenceScope,
                      source_ref: NativeContinuationRef, selection: HandoffSelection,
                      idempotency_key: str) -> HandoffPreview:
        async with self.db.transaction() as tx:
            user_id, key = store._uuid(user_id), store._key(idempotency_key)
            store._scope(source_scope)
            store._ref(source_ref)
            if type(selection) is not HandoffSelection:
                _fail()
            await store._runs(tx, user_id, source_scope)
            existing = await tx.fetchrow("SELECT id FROM native_task_handoffs WHERE user_id=$1::uuid AND prepare_key=$2",
                                          user_id, key)
            if existing:
                record = await store.load_handoff_tx(tx, user_id=user_id, handoff_id=str(existing["id"]))
                if (record.source_scope != source_scope or record.source_ref != source_ref
                        or _selection(record) != selection):
                    _fail()
                # Storage revalidates original ownership/registration. Replaying
                # the old preview never renews its review or expiry.
                record = await store.create_handoff_tx(tx, user_id=user_id, source_scope=source_scope,
                    source_ref=source_ref, target=record.target, idempotency_key=key)
                return _public(record)
            source = await assemble_task_tx(tx, user_id=user_id, scope=source_scope, source_ref=source_ref)
            target = await _review_target(tx, user_id=user_id, document=source.document,
                                          selection=selection, upload_service=self.upload_service)
            record = await store.create_handoff_tx(tx, user_id=user_id, source_scope=source_scope,
                source_ref=source_ref, target=target, idempotency_key=idempotency_key)
            return _public(record)

    @_private
    async def confirm(self, *, user_id: str, handoff_id: str, handoff_sha256: str,
                      destination_scope: NativeInferenceScope, idempotency_key: str) -> HandoffPreview:
        async with self.db.transaction() as tx:
            record = await store.load_handoff_tx(tx, user_id=user_id, handoff_id=handoff_id)
            if record.handoff_sha256 != handoff_sha256:
                _fail()
            await store._runs(tx, user_id, record.source_scope, destination_scope)
            record = await store.load_handoff_tx(tx, user_id=user_id, handoff_id=handoff_id)
            # A successful commit ACK can be recovered after expiry/review changes.
            # It replays the same destination only; it grants no new inference.
            target = record.target if record.destination_scope else await _review_target(
                tx, user_id=user_id, document=record.document, selection=_selection(record),
                upload_service=self.upload_service)
            bound = await store.bind_destination_tx(tx, user_id=user_id, handoff_id=handoff_id,
                handoff_sha256=handoff_sha256, target=target, destination_scope=destination_scope,
                idempotency_key=idempotency_key)
            return _public(bound)
