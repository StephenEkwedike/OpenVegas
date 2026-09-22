"""Private owned-file references, not embedded file bytes or provider upload IDs."""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

from openvegas.contracts.errors import APIErrorCode, ContractError

MAX_FILES = 8
KEY = "attachment_refs"


def validate_refs(refs: Any) -> list[dict[str, str]]:
    def fail():
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION,
            "Retained attachment references are invalid; start a fresh conversation.",
        )

    if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_FILES:
        fail()
    seen = set()
    out = []
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"file_id", "sha256"}:
            fail()
        ident, digest = ref["file_id"], ref["sha256"]
        try:
            if not isinstance(ident, str) or str(uuid.UUID(ident)) != ident or ident in seen:
                fail()
        except (ValueError, AttributeError):
            fail()
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            fail()
        seen.add(ident)
        out.append(dict(ref))
    return out


def references(attachments: list[dict]) -> list[dict[str, str]]:
    refs = [
        {"file_id": item["file_id"], "sha256": hashlib.sha256(item["content_bytes"]).hexdigest()}
        for item in attachments
    ]
    return validate_refs(refs)


async def resolve_retained(refs: list[dict], *, user_id: str, file_service: Any) -> list[dict]:
    refs = validate_refs(refs)
    resolved = await file_service.resolve_uploaded_for_inference(
        user_id=user_id, file_ids=[ref["file_id"] for ref in refs]
    )
    if references(resolved) != refs:
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION,
            "A retained attachment changed; no substituted file was sent.",
        )
    return resolved
