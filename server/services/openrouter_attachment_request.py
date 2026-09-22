"""Server-only assembly of current and retained, reauthorized upload content."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.providers import get_model_review
from server.services.attachment_history import references, validate_refs
from server.services.openrouter_attachments import (
    MAX_TOTAL_BYTES,
    AttachmentError,
    PreparedAttachments,
    calculate_request_token_bound,
    prepare_owned_attachment_blocks,
    validate_attachment_review,
)


@dataclass(frozen=True)
class AttachmentRequestContext:
    prepared: PreparedAttachments

    def validate(self, req: Any, model_config: dict) -> dict:
        if (
            req.provider != "openrouter"
            or req.account_id != "user:" + self.prepared.user_id
            or req.model != self.prepared.review.model_id
        ):
            raise ContractError(
                APIErrorCode.INVALID_TRANSITION, "Attachment request scope changed."
            )
        try:
            fresh = validate_attachment_review(
                model_id=req.model,
                model_config=model_config,
                model_review=get_model_review("openrouter", req.model),
            )
        except AttachmentError as exc:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, exc.detail) from None
        if fresh != self.prepared.review:
            raise ContractError(
                APIErrorCode.INVALID_TRANSITION,
                "Attachment review changed; prepare the request again.",
            )
        return self.prepared.request_options

    def token_bound(self, req: Any, tools: list[dict] | None) -> int:
        try:
            bound = calculate_request_token_bound(
                req.messages, prepared=self.prepared, max_output_tokens=req.max_tokens, tools=tools
            )
        except AttachmentError as exc:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, exc.detail) from None
        return bound + (
            len(json.dumps({"reasoning_effort": req.reasoning_effort}).encode())
            if getattr(req, "reasoning_effort", None) is not None
            else 0
        )


class _CheckedResolver:
    def __init__(self, service: Any, expected: list[dict] | None = None):
        self.service, self.expected, self.refs = service, expected, None

    async def resolve_uploaded_for_inference(
        self, *, user_id: str, file_ids: list[str]
    ) -> list[dict]:
        rows = await self.service.resolve_uploaded_for_inference(user_id=user_id, file_ids=file_ids)
        if (
            not isinstance(rows, list)
            or len(rows) != len(file_ids)
            or any(
                not isinstance(row, dict)
                or row.get("file_id") != ident
                or not isinstance(row.get("content_bytes"), (bytes, bytearray, memoryview))
                for ident, row in zip(file_ids, rows)
            )
        ):
            raise AttachmentError(
                "invalid_attachment_content", "Attachment content is invalid or unsafe."
            )
        if sum(memoryview(row["content_bytes"]).nbytes for row in rows) > MAX_TOTAL_BYTES:
            raise AttachmentError(
                "attachment_too_large", "Attachment content exceeds the request limit.", 413
            )
        self.refs = references(rows)
        if self.expected is not None and self.refs != validate_refs(self.expected):
            raise ContractError(
                APIErrorCode.INVALID_TRANSITION,
                "A retained attachment changed; no substituted file was sent.",
            )
        return rows


async def prepare_attachment_request(
    *,
    history: list[dict],
    prompt: str,
    file_ids: list[str],
    user_id: str,
    model_id: str,
    model_config: dict,
    upload_service: Any,
) -> tuple[list[dict], AttachmentRequestContext, list[dict] | None]:
    batches, outbound = [], []
    current_refs = None
    if (
        not isinstance(file_ids, list)
        or not isinstance(history, list)
        or len(history) > 199
        or not isinstance(prompt, str)
    ):
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Invalid attachment conversation.")
    for message in history:
        if (
            not isinstance(message, dict)
            or set(message) - {"role", "content", "attachment_refs"}
            or message.get("role") not in {"system", "user", "assistant"}
            or not isinstance(message.get("content"), str)
        ):
            raise ContractError(
                APIErrorCode.INVALID_TRANSITION, "Invalid retained attachment message."
            )
        if "attachment_refs" in message:
            validate_refs(message["attachment_refs"])
    # Bound all work before loading bytes, including retained uploads.
    count = len(file_ids) + sum(len(m.get("attachment_refs", [])) for m in history)
    if not 1 <= count <= 12:
        raise AttachmentError(
            "attachment_history_limit",
            "Attachment history exceeds twelve files; start a fresh conversation.",
        )
    review = get_model_review("openrouter", model_id)
    for index, message in enumerate([*history, {"role": "user", "content": prompt}]):
        current = index == len(history)
        expected = None if current else message.get("attachment_refs")
        ids = (
            file_ids
            if current
            else [ref["file_id"] for ref in validate_refs(expected)]
            if expected is not None
            else []
        )
        content = message["content"]
        if ids:
            if message["role"] != "user" or not isinstance(content, str):
                raise ContractError(
                    APIErrorCode.INVALID_TRANSITION, "Invalid retained attachment message."
                )
            resolver = _CheckedResolver(upload_service, expected)
            prepared = await prepare_owned_attachment_blocks(
                user_id=user_id,
                file_ids=ids,
                model_id=model_id,
                model_config=model_config,
                model_review=review,
                upload_service=resolver,
            )
            batches.append(prepared)
            content = [{"type": "text", "text": content}, *prepared.blocks]
            if current:
                current_refs = resolver.refs
        outbound.append({"role": message["role"], "content": content})
    if not batches:
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION, "Attachment request has no prepared content."
        )
    first = batches[0]
    if any(p.user_id != first.user_id or p.review != first.review for p in batches):
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Attachment history review changed.")
    combined = PreparedAttachments(
        first.user_id,
        tuple(i for p in batches for i in p.file_ids),
        first.review,
        sum(p.media_tokens for p in batches),
        json.dumps(
            [b for p in batches for b in p.blocks], ensure_ascii=False, separators=(",", ":")
        ),
    )
    return outbound, AttachmentRequestContext(combined), current_refs
