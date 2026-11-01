"""Private delivery pinned to an independently configured provisioning digest.

OPENVEGAS_EMOTE_RELEASE_SHA256 must equal the provisioner's manifest_sha256.
The release file and every delivered resource are verified afresh. This binding
does not approve artwork, grant ownership, or enable catalog sales.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from PIL import Image

from openvegas.emotes import manifest

MAX_PRIVATE_SHEET_BYTES = 2 * 1024 * 1024
MAX_PRIVATE_MANIFEST_BYTES = 64 * 1024
MAX_PRIVATE_RELEASE_BYTES = 64 * 1024
RELEASE_FILE = "release-manifest.json"
RELEASE_PIN_ENV = "OPENVEGAS_EMOTE_RELEASE_SHA256"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


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
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= limit:
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
        after = os.fstat(fd)
        if (len(data) != info.st_size or len(data) > limit
                or (info.st_mtime_ns, info.st_ctime_ns, info.st_size)
                != (after.st_mtime_ns, after.st_ctime_ns, after.st_size)):
            raise EmoteDeliveryUnavailable()
        return data
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _digest(value: object) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise EmoteDeliveryUnavailable()
    return value


def _release_entry(raw: bytes, pin: str, asset: dict) -> dict:
    if hashlib.sha256(raw).hexdigest() != _digest(pin):
        raise EmoteDeliveryUnavailable()
    release = manifest.parse_json(raw, MAX_PRIVATE_RELEASE_BYTES)
    if (set(release) != {"schema_version", "release_id", "kind", "sale_enabled",
                         "release_approval_required", "native_compatibility_verified", "packs"}
            or type(release["schema_version"]) is not int or release["schema_version"] != 1
            or release["kind"] != "private-emote-candidate"
            or release["sale_enabled"] is not False or release["release_approval_required"] is not True
            or release["native_compatibility_verified"] is not False
            or type(release["packs"]) is not list or not 1 <= len(release["packs"]) <= 6):
        raise EmoteDeliveryUnavailable()
    manifest.safe_token(release["release_id"])
    identities, resources, selected = set(), set(), None
    for entry in release["packs"]:
        if type(entry) is not dict or set(entry) != {
            "pack_id", "version", "slot", "delivery_resource", "artwork_fingerprint",
            "pixels_sha256", "license_id", "source_sha256", "files",
        }:
            raise EmoteDeliveryUnavailable()
        for key in ("pack_id", "version", "delivery_resource"):
            manifest.safe_token(entry[key])
        if (len(entry["version"]) > 64 or entry["pack_id"] in identities
                or entry["delivery_resource"] in resources
                or entry["slot"] not in ("companion", "completion")
                or type(entry["license_id"]) is not str or not 1 <= len(entry["license_id"]) <= 128
                or not entry["license_id"].isascii() or not entry["license_id"].isprintable()):
            raise EmoteDeliveryUnavailable()
        identities.add(entry["pack_id"])
        resources.add(entry["delivery_resource"])
        for key in ("artwork_fingerprint", "pixels_sha256", "source_sha256"):
            _digest(entry[key])
        files = entry["files"]
        if (type(files) is not dict or len(files) != 3
                or not {"manifest.json", "provenance.json"} <= files.keys()):
            raise EmoteDeliveryUnavailable()
        sheet_name = next(name for name in files if name not in {"manifest.json", "provenance.json"})
        manifest.safe_relative(sheet_name)
        for name, record in files.items():
            limit = MAX_PRIVATE_SHEET_BYTES if name == sheet_name else MAX_PRIVATE_MANIFEST_BYTES
            if (type(record) is not dict or set(record) != {"sha256", "bytes"}
                    or type(record["bytes"]) is not int or not 0 < record["bytes"] <= limit):
                raise EmoteDeliveryUnavailable()
            _digest(record["sha256"])
        fingerprint = (json.dumps({
            "manifest_sha256": files["manifest.json"]["sha256"],
            "sheet_sha256": files[sheet_name]["sha256"],
            "provenance_sha256": files["provenance.json"]["sha256"],
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        if hashlib.sha256(fingerprint).hexdigest() != entry["artwork_fingerprint"]:
            raise EmoteDeliveryUnavailable()
        if entry["delivery_resource"] == asset.get("delivery_resource"):
            if entry["pack_id"] != asset.get("pack_id") or entry["version"] != asset.get("version"):
                raise EmoteDeliveryUnavailable()
            selected = entry
    if selected is None:
        raise EmoteDeliveryUnavailable()
    return selected


def _verified_file(fd: int, name: str, entry: dict) -> bytes:
    record = entry["files"][name]
    data = _read(fd, name, record["bytes"])
    if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise EmoteDeliveryUnavailable()
    return data


def load_delivery_pack(item_id: str, asset: dict) -> dict:
    """Use server-approved assets; downloads additionally require caller ownership checks."""
    try:
        if type(asset) is not dict:
            raise EmoteDeliveryUnavailable()
        asset = {key: asset.get(key) for key in ("pack_id", "version", "delivery_resource")}
        pin = _digest(os.environ.get(RELEASE_PIN_ENV))
        target = _private_location(asset["delivery_resource"])
        # Release and resource share the same pinned root descriptor, including
        # during a directory rename. Never reopen the root by its absolute path.
        with _directory(target.parent) as root_fd:
            entry = _release_entry(_read(root_fd, RELEASE_FILE, MAX_PRIVATE_RELEASE_BYTES), pin, asset)
            fd = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=root_fd)
            try:
                raw = manifest.parse_json(_verified_file(fd, "manifest.json", entry))
                metadata = manifest.validate_manifest(raw)
                if (metadata.pack_id != entry["pack_id"] or metadata.version != entry["version"]
                        or metadata.license_id != entry["license_id"] or entry["slot"] not in metadata.tags
                        or set(entry["files"]) != {"manifest.json", "provenance.json", metadata.sheet}):
                    raise EmoteDeliveryUnavailable()
                sheet = _verified_file(fd, metadata.sheet, entry)
                provenance = manifest.parse_json(_verified_file(fd, "provenance.json", entry))
                if (provenance.get("source_sha256") != entry["source_sha256"]
                        or ("version" in provenance and provenance["version"] != metadata.version)):
                    raise EmoteDeliveryUnavailable()
            finally:
                os.close(fd)

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

        # Match provisioning's dimension-plus-RGBA digest only after bounded
        # geometry validation, using the verified bytes rather than source files.
        with Image.open(io.BytesIO(sheet)) as image, image.convert("RGBA") as rgba:
            pixels_sha256 = hashlib.sha256(str(rgba.size).encode() + rgba.tobytes()).hexdigest()
        if pixels_sha256 != entry["pixels_sha256"]:
            raise EmoteDeliveryUnavailable()

        if os.environ.get(RELEASE_PIN_ENV) != pin or _private_location(asset["delivery_resource"]) != target:
            raise EmoteDeliveryUnavailable()
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
        KeyError,
        RecursionError,
    ):
        raise EmoteDeliveryUnavailable() from None
