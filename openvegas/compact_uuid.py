"""Compact UUID encode/decode helpers for short URL paths."""

from __future__ import annotations

import base64
import uuid


def encode_compact_uuid(raw_uuid: str) -> str | None:
    try:
        val = uuid.UUID(str(raw_uuid))
    except Exception:
        return None
    token = base64.urlsafe_b64encode(val.bytes).decode("ascii").rstrip("=")
    return token


def decode_compact_uuid(token: str) -> str | None:
    raw = str(token or "").strip()
    if not raw:
        return None
    padded = raw + ("=" * ((4 - (len(raw) % 4)) % 4))
    try:
        data = base64.urlsafe_b64decode(padded.encode("ascii"))
        val = uuid.UUID(bytes=data)
    except Exception:
        return None
    return str(val)

