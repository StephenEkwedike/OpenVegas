"""Internal preview/dispatch checks for a server-assembled native handoff.

Receipts retain hashes, not file bytes or a reusable authorization. Dispatch
always calls the owned upload resolver again. This module does not switch a
model, import client history, authorize a handoff, or call a provider.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from openvegas.agent.native_handoff_document import PortableTaskDocument
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.providers import get_model_review
from server.services.attachment_history import references
from server.services.openrouter_attachments import (
    MAX_TOTAL_BYTES,
    PreparedAttachments,
    _uuid,
    prepare_owned_attachment_blocks,
    validate_attachment_review,
)


def _fail() -> None:
    raise ContractError(
        APIErrorCode.HANDOFF_BLOCKED,
        "Retained files or the selected model review changed; review the switch again. "
        "No files were dropped or substituted.",
    ) from None


def _decimal(value):
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    raise ValueError("Invalid review value")


def _hash(value) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":"), default=_decimal)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()


@dataclass(frozen=True, repr=False)
class HandoffAttachmentPreview:
    user_id: str
    document_sha256: str
    model_id: str
    review_sha256: str
    payload_sha256: str
    file_occurrences: int
    unique_files: int

    def __repr__(self):
        return "<HandoffAttachmentPreview private>"


@dataclass(frozen=True, repr=False)
class HandoffAttachmentContent:
    receipt: HandoffAttachmentPreview
    # A slot for each task, including tasks with no attachments. Never flatten
    # ownership or move a previous task's files onto the current user's prompt.
    by_task: tuple[PreparedAttachments | None, ...] = field(repr=False)

    def __repr__(self):
        return "<HandoffAttachmentContent private>"


class _BoundResolver:
    def __init__(self, service: Any):
        self.service = service
        self.expected: list[dict] = []
        self.total = 0
        self.metadata: list[dict] = []

    async def resolve_uploaded_for_inference(self, *, user_id, file_ids):
        rows = await self.service.resolve_uploaded_for_inference(
            user_id=user_id, file_ids=file_ids,
        )
        if (type(rows) is not list or len(rows) != len(self.expected)
                or any(type(row) is not dict or row.get("file_id") != ref["file_id"]
                       or not isinstance(row.get("content_bytes"), (bytes, bytearray, memoryview))
                       for row, ref in zip(rows, self.expected))):
            _fail()
        # Copy mutable buffers before hashing and preparing the exact same bytes.
        rows = [{**row, "content_bytes": bytes(row["content_bytes"])} for row in rows]
        self.total += sum(len(row["content_bytes"]) for row in rows)
        if self.total > MAX_TOTAL_BYTES or references(rows) != self.expected:
            _fail()
        self.metadata.extend(
            {key: row.get(key) for key in ("file_id", "filename", "mime_type", "size_bytes")}
            for row in rows
        )
        return rows


async def _resolve(
    *, document: PortableTaskDocument, user_id: str, model_id: str,
    model_config: dict, upload_service: Any,
) -> HandoffAttachmentContent:
    if type(document) is not PortableTaskDocument:
        _fail()
    _uuid(user_id)
    if (type(model_config) is not dict or model_config.get("provider") != "openrouter"
            or model_config.get("model_id") != model_id or model_config.get("enabled") is not True):
        _fail()
    tasks = document.values()["tasks"]
    all_refs = [ref for task in tasks for ref in task["attachment_refs"]]
    if len(all_refs) > 12:
        _fail()
    review = deepcopy(get_model_review("openrouter", model_id))
    config_fields = ("provider", "model_id", "enabled", "max_tokens", "cost_input_per_1m",
                     "cost_output_per_1m", "v_price_input_per_1m", "v_price_output_per_1m")
    # Bound to the full reviewed capability record, not just the media fields.
    review_hash = _hash([review, {key: model_config.get(key) for key in config_fields}])
    resolver = _BoundResolver(upload_service)
    prepared = []
    for task in tasks:
        refs = task["attachment_refs"]
        if not refs:
            prepared.append(None)
            continue
        resolver.expected = refs
        prepared.append(await prepare_owned_attachment_blocks(
            user_id=user_id, file_ids=[ref["file_id"] for ref in refs], model_id=model_id,
            model_config=model_config, model_review=review, upload_service=resolver,
        ))
    # Upload resolution and parsers await: an operator can revoke the review or
    # its expiry can pass meanwhile. Check again after the last await. Gateway
    # preflight must still check the review immediately before actual dispatch.
    fresh_review = get_model_review("openrouter", model_id)
    if _hash([fresh_review, {key: model_config.get(key) for key in config_fields}]) != review_hash:
        _fail()
    if all_refs:
        fresh = validate_attachment_review(model_id=model_id, model_config=model_config,
                                           model_review=fresh_review)
        if any(item is not None and item.review != fresh for item in prepared):
            _fail()
    payload_hash = _hash([resolver.metadata, [
        None if item is None else {"blocks": item.blocks, "options": item.request_options,
                                   "media_tokens": item.media_tokens}
        for item in prepared
    ]])
    receipt = HandoffAttachmentPreview(
        user_id, document.sha256, model_id, review_hash, payload_hash,
        len(all_refs), len({ref["file_id"] for ref in all_refs}),
    )
    return HandoffAttachmentContent(receipt, tuple(prepared))


async def preview_handoff_attachments(**kwargs) -> HandoffAttachmentPreview:
    """Discard prepared bytes after validating every retained file for review."""
    return (await _resolve(**kwargs)).receipt


async def dispatch_handoff_attachments(
    *, preview: HandoffAttachmentPreview, document: PortableTaskDocument,
    user_id: str, model_id: str, model_config: dict, upload_service: Any,
) -> HandoffAttachmentContent:
    """Reauthorize immediately before outbound construction; never reuse preview bytes.

    The later coordinator must separately validate source/run ownership, destination
    capabilities, gates, complete input bounds and the committed handoff. This is
    internal data preparation, not a public request DTO or execution authority.
    """
    if (type(preview) is not HandoffAttachmentPreview or type(document) is not PortableTaskDocument
            or preview.user_id != user_id or preview.model_id != model_id
            or preview.document_sha256 != document.sha256):
        _fail()
    result = await _resolve(document=document, user_id=user_id, model_id=model_id,
                            model_config=model_config, upload_service=upload_service)
    if result.receipt != preview:
        _fail()
    return result
