"""Strict, bounded v1 authored PNG packs. Metadata never grants ownership."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from PIL import Image

MAX_MANIFEST_BYTES = 64 * 1024
MAX_SHEET_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 4 * 1024 * 1024
MAX_FRAMES = 4096
TOKEN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}\Z")


class PackError(ValueError):
    """Invalid, unsafe, or unavailable pack; safe to display without raw input."""


def safe_token(value: object) -> str:
    if not isinstance(value, str) or not TOKEN.fullmatch(value) or ".." in value:
        raise PackError("Invalid pack identifier")
    return value


def _label(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or not value.isprintable()
        or any(ord(c) > 126 for c in value)
    ):
        raise PackError("Metadata labels must be printable ASCII, 1-128 characters")
    return value


def _int(value: object, lo: int, hi: int) -> int:
    if type(value) is not int or not lo <= value <= hi:
        raise PackError("Integer field outside allowed bounds")
    return value


def _object(
    value: object, required: set[str], optional: set[str] = frozenset()
) -> dict:
    if not isinstance(value, dict) or not required <= value.keys():
        raise PackError("Missing required manifest fields")
    if value.keys() - required - optional:
        raise PackError("Unsupported manifest fields")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackError("Duplicate JSON key")
        result[key] = value
    return result


def parse_json(data: bytes, limit: int = MAX_MANIFEST_BYTES) -> dict:
    if not isinstance(data, bytes) or not 0 < len(data) <= limit:
        raise PackError("JSON size outside allowed bounds")
    try:
        result = json.loads(data, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PackError("Invalid JSON") from exc
    if not isinstance(result, dict):
        raise PackError("JSON object required")
    return result


def safe_relative(value: object) -> str:
    if not isinstance(value, str) or len(value) > 200 or "\\" in value:
        raise PackError("Unsafe asset path")
    parts = value.split("/")
    if (
        not parts
        or len(parts) > 8
        or any(p in {"", ".", ".."} or not TOKEN.fullmatch(p) for p in parts)
        or PurePosixPath(value).suffix.lower() != ".png"
    ):
        raise PackError("Only safe relative PNG paths are accepted")
    return value


def read_local(root: Path, relative: str, limit: int) -> bytes:
    """Read through retained no-follow descriptors/handles, never resolved paths."""
    if not isinstance(relative, str) or len(relative) > 200:
        raise PackError("Unsafe resource path")
    parts = relative.split("/")
    if len(parts) > 8 or any(p in {"", ".", ".."} or not TOKEN.fullmatch(p) for p in parts):
        raise PackError("Unsafe resource path")
    if type(limit) is not int or limit < 0:
        raise PackError("Invalid resource size limit")
    if os.name == "nt":
        from ._windows_resources import read_windows

        try:
            return read_windows(root, parts, limit)
        except OSError as exc:
            raise PackError("Pack resource missing or unsafe") from exc
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise PackError("Secure local pack loading is unavailable on this platform")
    descriptors = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise PackError("Resource must be a bounded regular file")
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise PackError("Resource exceeds size limit")
        return data
    except OSError as exc:
        raise PackError("Pack resource missing or unsafe") from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


@dataclass(frozen=True)
class Animation:
    frames: tuple[int, ...]
    frame_ms: float
    loop: bool

    @property
    def duration(self) -> float:
        return len(self.frames) * self.frame_ms / 1000

    def frame_at(self, elapsed: float) -> int:
        slot = int(max(0.0, elapsed) * 1000 / self.frame_ms)
        return self.frames[
            slot % len(self.frames) if self.loop else min(slot, len(self.frames) - 1)
        ]


@dataclass(frozen=True)
class Manifest:
    pack_id: str
    version: str
    display_name: str
    license_id: str
    sheet: str
    sha256: str
    width: int
    height: int
    anchor: tuple[int, int]
    animations: Mapping[str, Animation]
    reduced_motion_frame: int
    tags: tuple[str, ...]
    schema_version: int = 1


def validate_manifest(raw: dict) -> Manifest:
    _object(
        raw,
        {
            "schema_version",
            "pack_id",
            "version",
            "display_name",
            "license_id",
            "sheet",
            "sha256",
            "frame",
            "animations",
            "reduced_motion_frame",
            "tags",
        },
    )
    _int(raw["schema_version"], 1, 1)
    if not isinstance(raw["sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", raw["sha256"]
    ):
        raise PackError("A computed lowercase SHA-256 is required")
    geometry = _object(raw["frame"], {"width", "height", "anchor"})
    width, height = (_int(geometry[k], 1, 512) for k in ("width", "height"))
    anchor = geometry["anchor"]
    if not isinstance(anchor, list) or len(anchor) != 2:
        raise PackError("Frame anchor must contain x and y")
    anchor = (_int(anchor[0], 0, width - 1), _int(anchor[1], 0, height - 1))
    clips = raw["animations"]
    if (
        not isinstance(clips, dict)
        or not {"idle", "waiting", "complete"} <= clips.keys()
    ):
        raise PackError("idle, waiting, and complete animations are required")
    if len(clips) > 32:
        raise PackError("Too many animations")
    animations = {}
    for name, clip in clips.items():
        safe_token(name)
        _object(clip, {"frames", "frame_ms", "loop"})
        frames, frame_ms, loop = (clip[k] for k in ("frames", "frame_ms", "loop"))
        if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
            raise PackError("Clip frame count outside allowed bounds")
        frames = tuple(_int(i, 0, MAX_FRAMES - 1) for i in frames)
        if (
            type(frame_ms) not in {int, float}
            or not 1 <= frame_ms <= 60000
            or not math.isfinite(frame_ms)
            or type(loop) is not bool
        ):
            raise PackError("Invalid clip timing or loop flag")
        animation = Animation(frames, float(frame_ms), loop)
        if animation.duration > 300:
            raise PackError("Animation exceeds five minutes")
        if name == "complete" and (loop or not 4 <= animation.duration <= 7):
            raise PackError("Completion must be non-looping and last 4-7 seconds")
        if name in {"idle", "waiting"} and not loop:
            raise PackError("idle and waiting must loop")
        animations[name] = animation
    tags = raw["tags"]
    if not isinstance(tags, list) or len(tags) > 24:
        raise PackError("Invalid tags")
    return Manifest(
        safe_token(raw["pack_id"]),
        safe_token(raw["version"]),
        _label(raw["display_name"]),
        _label(raw["license_id"]),
        safe_relative(raw["sheet"]),
        raw["sha256"],
        width,
        height,
        anchor,
        MappingProxyType(animations),
        _int(raw["reduced_motion_frame"], 0, MAX_FRAMES - 1),
        tuple(safe_token(tag) for tag in tags),
    )


@dataclass(frozen=True)
class LoadedPack:
    manifest: Manifest
    _frames: Mapping[int, Image.Image]

    def frame(self, index: int) -> Image.Image:
        """Return a private copy; renderers cannot corrupt cached frames."""
        return self._frames[index].copy()


def decode_pack(manifest: Manifest, data: bytes) -> LoadedPack:
    if not 0 < len(data) <= MAX_SHEET_BYTES:
        raise PackError("PNG size outside allowed bounds")
    if hashlib.sha256(data).hexdigest() != manifest.sha256:
        raise PackError("PNG checksum mismatch")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format != "PNG" or getattr(source, "n_frames", 1) != 1:
                    raise PackError("Only single-image PNG sheets are supported")
                w, h = source.size
                if (
                    w * h > MAX_PIXELS
                    or w % manifest.width
                    or h % manifest.height
                    or w < manifest.width
                    or h < manifest.height
                ):
                    raise PackError(
                        "Invalid sheet dimensions or decoded pixel limit exceeded"
                    )
                count = (w // manifest.width) * (h // manifest.height)
                if count > MAX_FRAMES:
                    raise PackError("Too many sheet frames")
                used = {manifest.reduced_motion_frame}
                used.update(
                    i for clip in manifest.animations.values() for i in clip.frames
                )
                if any(i >= count for i in used):
                    raise PackError("Animation references a frame outside the sheet")
                source.verify()
            with Image.open(io.BytesIO(data)) as source:
                rgba = source.convert("RGBA")
                frames = {}
                columns = w // manifest.width
                for i in sorted(used):
                    x, y = (
                        (i % columns) * manifest.width,
                        (i // columns) * manifest.height,
                    )
                    frames[i] = rgba.crop(
                        (x, y, x + manifest.width, y + manifest.height)
                    )
                rgba.close()
        return LoadedPack(manifest, MappingProxyType(frames))
    except PackError:
        raise
    except (
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise PackError("Invalid or oversized PNG") from exc


def load_pack(directory: str | Path) -> LoadedPack:
    root = Path(directory)
    manifest = validate_manifest(
        parse_json(read_local(root, "manifest.json", MAX_MANIFEST_BYTES))
    )
    return decode_pack(manifest, read_local(root, manifest.sheet, MAX_SHEET_BYTES))
