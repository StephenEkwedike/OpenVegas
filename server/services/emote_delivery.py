"""Bounded private pack delivery from an operator-owned, non-public directory."""

from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from openvegas.emotes import manifest

MAX_PRIVATE_SHEET_BYTES = 2 * 1024 * 1024
MAX_PRIVATE_MANIFEST_BYTES = 64 * 1024


class EmoteDeliveryUnavailable(Exception):
    """Public error intentionally contains no path, metadata, or OS exception."""

    def __init__(self):
        super().__init__("COSMETIC_DELIVERY_UNAVAILABLE")


def _private_location(resource: object) -> Path:
    slug = manifest.safe_token(resource)
    configured = os.environ.get("OPENVEGAS_EMOTE_PACK_ROOT", "").strip()
    if not configured:
        raise EmoteDeliveryUnavailable()
    root = Path(configured)
    if not root.is_absolute() or ".." in root.parts:
        raise EmoteDeliveryUnavailable()
    target = root / slug
    parts = tuple(part.casefold() for part in target.parts)
    # Public UI trees and bundled previews must never become a private source.
    if (
        "ui" in parts
        or "public" in parts
        or any(parts[i : i + 3] == ("openvegas", "emotes", "assets") for i in range(len(parts) - 2))
    ):
        raise EmoteDeliveryUnavailable()
    return target


@contextmanager
def _directory(path: Path):
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in os.supports_dir_fd
    ):
        raise EmoteDeliveryUnavailable()
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(path.anchor, flags)
        descriptors.append(fd)
        # Pin every ancestor; a renamed directory cannot redirect subsequent reads.
        for part in path.parts[1:]:
            fd = os.open(part, flags, dir_fd=fd)
            descriptors.append(fd)
        yield fd
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _read(fd: int, relative: str, limit: int) -> bytes:
    descriptors = []
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=fd,
            )
            descriptors.append(fd)
        fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=fd,
        )
        descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise EmoteDeliveryUnavailable()
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if not 0 < len(data) <= limit:
            raise EmoteDeliveryUnavailable()
        return data
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def load_delivery_pack(item_id: str, asset: dict) -> dict:
    """Use server-approved assets; downloads additionally require caller ownership checks."""
    try:
        with _directory(_private_location(asset.get("delivery_resource"))) as fd:
            raw = manifest.parse_json(_read(fd, "manifest.json", MAX_PRIVATE_MANIFEST_BYTES))
            metadata = manifest.validate_manifest(raw)
            if metadata.pack_id != asset.get("pack_id") or metadata.version != asset.get("version"):
                raise EmoteDeliveryUnavailable()
            sheet = _read(fd, metadata.sheet, MAX_PRIVATE_SHEET_BYTES)

        # Validate the exact bounded bytes we will return, not a second read of
        # operator files that could have changed after the size/identity checks.
        with tempfile.TemporaryDirectory(prefix="openvegas-private-pack-") as temporary:
            snapshot = Path(temporary)
            sheet_path = snapshot / metadata.sheet
            sheet_path.parent.mkdir(parents=True, exist_ok=True)
            sheet_path.write_bytes(sheet)
            (snapshot / "manifest.json").write_text(
                json.dumps(raw, separators=(",", ":")),
                encoding="utf-8",
            )
            manifest.load_pack(snapshot)

        return {
            "schema_version": 1,
            "item_id": item_id,
            "pack_id": metadata.pack_id,
            "version": metadata.version,
            "manifest": raw,
            "sheet_base64": base64.b64encode(sheet).decode("ascii"),
        }
    except (
        EmoteDeliveryUnavailable,
        manifest.PackError,
        OSError,
        ValueError,
        TypeError,
    ):
        raise EmoteDeliveryUnavailable() from None
