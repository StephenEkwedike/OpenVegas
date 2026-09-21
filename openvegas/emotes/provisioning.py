"""Offline, fail-closed provisioning of six separately authored private candidates.

No configuration, account, catalog approval, network, or deployment is changed.
Outputs belong outside the checkout and any public/static/package tree. The
operator must additionally ensure an otherwise named destination is not served
by a web server. This is not DRM, artwork approval, or native UX certification.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .manifest import (
    MAX_MANIFEST_BYTES,
    PackError,
    decode_pack,
    parse_json,
    safe_token,
    validate_manifest,
)

REPO_ROOT = Path(__file__).absolute().parents[2]
PACK_SLOTS = {
    "openvegas.pixel-courier": "companion",
    "openvegas.beat-maker": "companion",
    "openvegas.visor-explorer": "companion",
    "openvegas.skyline-dunk": "completion",
    "openvegas.bicycle-finish": "completion",
    "openvegas.three-point-glow": "completion",
}
MAX_PRIVATE_SHEET_BYTES = 2 * 1024 * 1024
BLOCKED_PARTS = frozenset(
    {
        "ui",
        "public",
        "static",
        "www",
        "htdocs",
        "assets",
        "creatives",
        "node_modules",
        "site-packages",
        "dist",
        "build",
        ".git",
    }
)
RELEASE_FILE = "release-manifest.json"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()


def _path(value: object, *, private: bool = False) -> Path:
    if not isinstance(value, (str, Path)):
        raise PackError("An explicit canonical absolute local path is required")
    text = str(value)
    path = Path(text)
    if (
        not path.is_absolute()
        or str(path) != text
        or len(path.parts) < 3
        or ".." in path.parts
        or "\\" in text
        or not text.isprintable()
        or text.startswith("//")
    ):
        raise PackError("An explicit canonical absolute local path is required")
    if private and (
        path == REPO_ROOT
        or REPO_ROOT in path.parents
        or any(part.casefold() in BLOCKED_PARTS for part in path.parts)
    ):
        raise PackError("Private packs must remain outside checkout, public and package trees")
    return path


def _platform() -> None:
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
    ):
        raise PackError("Private provisioning requires POSIX no-follow directory handles")


def _flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK


@contextmanager
def _directory(path: Path, *, outside_checkout: bool = False):
    _platform()
    descriptors = []
    try:
        fd = os.open(path.anchor, _flags())
        descriptors.append(fd)
        for part in path.parts[1:]:
            fd = os.open(part, _flags(), dir_fd=fd)
            descriptors.append(fd)
            if outside_checkout:
                try:
                    os.stat(".git", dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise PackError("Private packs must not live inside a Git checkout")
        yield fd
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _owned(info: os.stat_result, *, sealed: int | None = None) -> None:
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PackError("Private input/output directories require owner-only permissions")
    if sealed is not None and stat.S_IMODE(info.st_mode) != sealed:
        raise PackError("Existing bundle is not sealed; refusing to modify it")


def _stamp(info: os.stat_result) -> tuple:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read(fd: int, relative: str, limit: int, *, sealed: bool = False) -> bytes:
    descriptors = []
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            fd = os.open(part, _flags(), dir_fd=fd)
            descriptors.append(fd)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        descriptors.append(fd)
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= limit
        ):
            raise PackError("Resource must be a bounded, single-link regular file")
        if sealed:
            _owned(before, sealed=0o400)
        chunks, remaining = [], limit + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != before.st_size or _stamp(before) != _stamp(os.fstat(fd)):
            raise PackError("Resource changed during validation")
        return data
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _pixels(sheet: bytes) -> str:
    # Called only after bounded PNG decoding. Re-encoding a public preview must
    # not turn the same artwork into a purported private premium deliverable.
    with Image.open(io.BytesIO(sheet)) as image:
        rgba = image.convert("RGBA")
        try:
            return _digest(str(rgba.size).encode() + rgba.tobytes())
        finally:
            rgba.close()


def _validated(raw: bytes, sheet: bytes):
    metadata = validate_manifest(parse_json(raw))
    decoded = decode_pack(metadata, sheet)
    for frame in decoded._frames.values():
        frame.close()
    return metadata


def _public_previews() -> tuple[set[str], set[tuple[str, str]]]:
    fingerprints, versions = set(), set()
    for pack_id in sorted(PACK_SLOTS):
        slug = pack_id.removeprefix("openvegas.")
        paths = [REPO_ROOT / "openvegas/emotes/assets" / slug]
        if PACK_SLOTS[pack_id] == "completion":
            paths.append(REPO_ROOT / "ui/assets/emotes/previews" / slug)
        else:
            paths.append(REPO_ROOT / "ui/assets/emotes" / slug)
        for path in paths:
            with _directory(path) as fd:
                raw = _read(fd, "manifest.json", MAX_MANIFEST_BYTES)
                metadata = validate_manifest(parse_json(raw))
                sheet = _read(fd, metadata.sheet, MAX_PRIVATE_SHEET_BYTES)
            _validated(raw, sheet)
            if metadata.pack_id != pack_id:
                raise PackError("Public preview baseline identity mismatch")
            fingerprints.add(_pixels(sheet))
            versions.add((pack_id, metadata.version))
    return fingerprints, versions


@dataclass(frozen=True)
class Bundle:
    """Validated byte snapshot, independent of later changes to input files."""

    files: tuple[tuple[str, bytes], ...]
    release: bytes

    @property
    def sha256(self) -> str:
        return _digest(self.release)


def prepare(plan_path: str | Path) -> Bundle:
    """Read an explicitly pinned six-pack plan without writing any files."""
    path = _path(plan_path)
    with _directory(path.parent) as fd:
        plan = parse_json(_read(fd, path.name, MAX_MANIFEST_BYTES))
    if (
        set(plan) != {"schema_version", "release_id", "packs"}
        or type(plan["schema_version"]) is not int
        or plan["schema_version"] != 1
    ):
        raise PackError("Invalid provisioning plan schema")
    release_id = safe_token(plan["release_id"])
    entries = plan["packs"]
    if not isinstance(entries, list) or len(entries) != len(PACK_SLOTS):
        raise PackError("Plan must contain exactly the six companion/completion packs")
    seen, sources = set(), set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "pack_id",
            "version",
            "source_directory",
            "manifest_sha256",
            "sheet_sha256",
            "provenance_sha256",
        }:
            raise PackError("Invalid provisioning pack fields")
        pack_id = safe_token(entry["pack_id"])
        if pack_id not in PACK_SLOTS or pack_id in seen:
            raise PackError("Missing, duplicate or unsupported pack identity")
        seen.add(pack_id)
        version = safe_token(entry["version"])
        if len(version) > 64:
            raise PackError("Pack version exceeds backend catalog bounds")
        safe_token(f"{pack_id}--{version}")
        for key in ("manifest_sha256", "sheet_sha256", "provenance_sha256"):
            if not isinstance(entry[key], str) or not re.fullmatch(r"[0-9a-f]{64}", entry[key]):
                raise PackError("Pin exact lowercase SHA-256 hashes for every pack file")
        source = _path(entry["source_directory"], private=True)
        if source in sources:
            raise PackError("Each pack requires a distinct private source directory")
        sources.add(source)

    public_pixels, public_versions = _public_previews()
    files, packs = [], []
    for entry in sorted(entries, key=lambda item: item["pack_id"]):
        with _directory(Path(entry["source_directory"]), outside_checkout=True) as fd:
            _owned(os.fstat(fd))
            raw = _read(fd, "manifest.json", MAX_MANIFEST_BYTES)
            metadata = validate_manifest(parse_json(raw))
            sheet = _read(fd, metadata.sheet, MAX_PRIVATE_SHEET_BYTES)
            provenance_bytes = _read(fd, "provenance.json", MAX_MANIFEST_BYTES)
        if (
            _digest(raw) != entry["manifest_sha256"]
            or _digest(sheet) != entry["sheet_sha256"]
            or _digest(provenance_bytes) != entry["provenance_sha256"]
        ):
            raise PackError("Pinned manifest/sheet/provenance checksum mismatch")
        provenance = parse_json(provenance_bytes)
        source_hash = provenance.get("source_sha256")
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise PackError("Provenance must identify the exact authored source SHA-256")
        if "version" in provenance and provenance["version"] != metadata.version:
            raise PackError("Provenance version does not match the manifest")
        _validated(raw, sheet)
        if (metadata.pack_id, metadata.version) != (entry["pack_id"], entry["version"]):
            raise PackError("Pack identity/version does not match the release plan")
        if PACK_SLOTS[metadata.pack_id] not in metadata.tags:
            raise PackError("Pack slot tag does not match the catalog")
        if (metadata.pack_id, metadata.version) in public_versions or _pixels(
            sheet
        ) in public_pixels:
            raise PackError("Public preview art/version cannot become a private premium release")
        resource = f"{metadata.pack_id}--{metadata.version}"
        pack_files = [
            ("manifest.json", raw),
            (metadata.sheet, sheet),
            ("provenance.json", provenance_bytes),
        ]
        files.extend((f"{resource}/{name}", data) for name, data in pack_files)
        packs.append(
            {
                "pack_id": metadata.pack_id,
                "version": metadata.version,
                "slot": PACK_SLOTS[metadata.pack_id],
                "delivery_resource": resource,
                "artwork_fingerprint": _digest(
                    _json_bytes(
                        {
                            "manifest_sha256": _digest(raw),
                            "sheet_sha256": _digest(sheet),
                            "provenance_sha256": _digest(provenance_bytes),
                        }
                    )
                ),
                "pixels_sha256": _pixels(sheet),
                "license_id": metadata.license_id,
                "source_sha256": source_hash,
                "files": {
                    name: {"sha256": _digest(data), "bytes": len(data)} for name, data in pack_files
                },
            }
        )
    release = _json_bytes(
        {
            "schema_version": 1,
            "release_id": release_id,
            "kind": "private-emote-candidate",
            "sale_enabled": False,
            "release_approval_required": True,
            "native_compatibility_verified": False,
            "packs": packs,
        }
    )
    return Bundle(tuple(sorted(files)), release)


def _tree(bundle: Bundle) -> dict:
    root = {}
    for relative, data in (*bundle.files, (RELEASE_FILE, bundle.release)):
        node = root
        parts = relative.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = data
    return root


def _verify_tree(fd: int, tree: dict) -> None:
    _owned(os.fstat(fd), sealed=0o500)
    # Stop after one excess entry rather than materializing an unbounded folder.
    names = set()
    with os.scandir(fd) as entries:
        for entry in entries:
            names.add(entry.name)
            if len(names) > len(tree):
                raise PackError("Existing bundle differs; no files were overwritten")
    if names != tree.keys():
        raise PackError("Existing bundle differs; no files were overwritten")
    for name, value in tree.items():
        if isinstance(value, dict):
            child = os.open(name, _flags(), dir_fd=fd)
            try:
                _verify_tree(child, value)
            finally:
                os.close(child)
        elif _read(fd, name, len(value), sealed=True) != value:
            raise PackError("Existing bundle differs; no files were overwritten")


def _write_tree(fd: int, tree: dict) -> None:
    # The release manifest is the last root file, not an early success marker.
    for name in sorted(tree, key=lambda value: (value == RELEASE_FILE, value)):
        value = tree[name]
        if isinstance(value, dict):
            os.mkdir(name, mode=0o700, dir_fd=fd)
            child = os.open(name, _flags(), dir_fd=fd)
            try:
                _write_tree(child, value)
            finally:
                os.close(child)
        else:
            child = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
            )
            try:
                remaining = memoryview(value)
                while remaining:
                    written = os.write(child, remaining)
                    if not written:
                        raise OSError("Unable to finish private file")
                    remaining = remaining[written:]
                os.fchmod(child, 0o400)
                os.fsync(child)
            finally:
                os.close(child)
    os.fchmod(fd, 0o500)
    os.fsync(fd)


def provision(plan_path: str | Path, destination: str | Path, *, apply: bool = False) -> dict:
    """Dry-run by default; exact re-runs never write, conflicts never overwrite.

    The destination parent must already exist and be owner-only. A write failure
    can leave a partial, owner-only directory; it is intentionally NOT deleted
    or reused automatically. Inspect it and choose a fresh destination. Never
    point a live backend at an in-progress destination.
    """
    target = _path(destination, private=True)
    bundle = prepare(plan_path)
    tree = _tree(bundle)
    with _directory(target.parent, outside_checkout=True) as parent:
        _owned(os.fstat(parent))
        try:
            fd = os.open(target.name, _flags(), dir_fd=parent)
        except FileNotFoundError:
            fd = None
        if fd is not None:
            try:
                _verify_tree(fd, tree)
            finally:
                os.close(fd)
            status = "already_provisioned"
        elif not apply:
            status = "dry_run"
        else:
            os.mkdir(target.name, mode=0o700, dir_fd=parent)
            fd = os.open(target.name, _flags(), dir_fd=parent)
            try:
                _owned(os.fstat(fd))
                _write_tree(fd, tree)
                _verify_tree(fd, tree)
                os.fsync(parent)
            finally:
                os.close(fd)
            status = "provisioned"
    return {
        "status": status,
        "manifest_sha256": bundle.sha256,
        "file_count": len(bundle.files) + 1,
        "release": parse_json(bundle.release),
        "note": "Local candidate only. Sales and approvals unchanged; no deployment performed.",
    }
