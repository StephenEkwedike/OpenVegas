"""Version 1 data-only storyboard schema. No code, media paths, or remote assets."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import unicodedata
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 1024 * 1024
MAX_DURATION_SECONDS = 1800
CHANNEL_IDS = tuple(f"{n:02d}" for n in range(1, 13))
CANVAS = {"width": 320, "height": 180}
_IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_URL = re.compile(
    r"[a-z][a-z0-9+.-]*://|(?:https?|file|data|javascript|ftp|mailto):|www\."
    r"|(?:^|\s)//[a-z0-9]|\b[a-z0-9][a-z0-9.-]*\.[a-z]{2,}\b"
    r"|\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b",
    re.IGNORECASE,
)


class EpisodeValidationError(ValueError):
    """Invalid creator data; messages never echo untrusted input."""


def _fail(where: str, reason: str) -> None:
    raise EpisodeValidationError(f"{where}: {reason}")


def _object(value: Any, where: str, required: set[str], optional: set[str] | None = None):
    if type(value) is not dict:
        _fail(where, "must be an object")
    if set(value) - required - (optional or set()):
        _fail(where, "unknown fields are not allowed")
    if required - set(value):
        _fail(where, "required fields are missing")
    return value


def _text(value: Any, where: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or not (0 if empty else 1) <= len(value) <= maximum:
        _fail(where, f"must be plain text of at most {maximum} characters")
    if (not empty and not value.strip()) or any(
        unicodedata.category(ch).startswith("C") or ch in "<>" for ch in value
    ):
        _fail(where, "control characters and markup are not allowed")
    if _URL.search(value):
        _fail(where, "URLs are not allowed")
    cells = sum(0 if unicodedata.combining(ch) else
                2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in value)
    if cells > maximum:
        _fail(where, f"must fit within {maximum} display cells")
    return value


def _number(value: Any, where: str, low: float, high: float) -> float:
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        _fail(where, f"must be a finite number between {low} and {high}")
    return float(value)


def _list(value: Any, where: str, low: int, high: int) -> list:
    if type(value) is not list or not low <= len(value) <= high:
        _fail(where, f"must be an array with {low} to {high} entries")
    return value


def _id(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        _fail(where, "must be a lowercase identifier")
    return value


def _color(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _COLOR.fullmatch(value):
        _fail(where, "must be a six-digit hex color")
    return value.lower()


def _shape(value: Any, where: str, duration: float) -> dict:
    value = _object(value, where, {"id", "kind", "x", "y", "width", "height", "fill"}, {
        "stroke", "stroke_width", "opacity", "text", "font_size", "keyframes",
    })
    kind = value["kind"]
    if kind not in ("rect", "ellipse", "text"):
        _fail(where, "kind must be rect, ellipse, or text")
    result = {
        "id": _id(value["id"], where + ".id"),
        "kind": kind,
        "x": _number(value["x"], where + ".x", -320, 640),
        "y": _number(value["y"], where + ".y", -180, 360),
        "width": _number(value["width"], where + ".width", 1, 320),
        "height": _number(value["height"], where + ".height", 1, 180),
        "fill": _color(value["fill"], where + ".fill"),
        "stroke": _color(value.get("stroke", value["fill"]), where + ".stroke"),
        "stroke_width": _number(value.get("stroke_width", 0), where + ".stroke_width", 0, 8),
        "opacity": _number(value.get("opacity", 1), where + ".opacity", 0, 1),
        "text": _text(value.get("text", ""), where + ".text", 64, empty=kind != "text"),
        "font_size": _number(value.get("font_size", 12), where + ".font_size", 6, 36),
        "keyframes": [],
    }
    if kind != "text" and result["text"]:
        _fail(where, "only text shapes may contain text")
    previous = -1.0
    for i, key in enumerate(_list(value.get("keyframes", []), where + ".keyframes", 0, 32)):
        point = f"{where}.keyframes[{i}]"
        key = _object(key, point, {"at", "x", "y", "opacity"})
        at = _number(key["at"], point + ".at", 0, duration)
        if at <= previous or (i == 0 and at != 0):
            _fail(point, "keyframes must start at zero and increase strictly")
        result["keyframes"].append({
            "at": at,
            "x": _number(key["x"], point + ".x", -320, 640),
            "y": _number(key["y"], point + ".y", -180, 360),
            "opacity": _number(key["opacity"], point + ".opacity", 0, 1),
        })
        previous = at
    return result


def validate_episode(data: dict) -> dict:
    """Return a detached, normalized JSON dict or raise EpisodeValidationError.

    Coordinates use a 320x180 canvas. Scenes tile the entire timeline. Captions
    and scenes are start-inclusive/end-exclusive. Shape keyframes are relative
    to their scene, linearly interpolated, then held after the last keyframe.
    """
    try:
        encoded = json.dumps(data, ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError, OverflowError):
        _fail("episode", "must contain only finite JSON values")
    if len(encoded) > MAX_FILE_BYTES:
        _fail("episode", "exceeds 1 MiB")
    data = _object(data, "episode", {
        "schema_version", "id", "channel_id", "title", "description", "duration_seconds",
        "canvas", "scenes", "captions", "credits",
    })
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        _fail("schema_version", "only version 1 is supported")
    if data["channel_id"] not in CHANNEL_IDS:
        _fail("channel_id", "must be a channel from 01 to 12")
    canvas = _object(data["canvas"], "canvas", {"width", "height"})
    if any(type(canvas[key]) is not int or canvas[key] != val for key, val in CANVAS.items()):
        _fail("canvas", "must be 320 by 180")
    duration = _number(data["duration_seconds"], "duration_seconds", 0.1, MAX_DURATION_SECONDS)
    result = {
        "schema_version": 1,
        "id": _id(data["id"], "id"),
        "channel_id": data["channel_id"],
        "title": _text(data["title"], "title", 120),
        "description": _text(data["description"], "description", 500),
        "duration_seconds": duration,
        "canvas": dict(CANVAS),
        "scenes": [],
        "captions": [],
        "credits": _text(data["credits"], "credits", 500),
    }
    previous = 0.0
    seen = set()
    total_shapes = total_keyframes = 0
    for i, scene in enumerate(_list(data["scenes"], "scenes", 1, 64)):
        where = f"scenes[{i}]"
        scene = _object(scene, where, {
            "id", "title", "description", "start", "end", "background", "shapes",
        })
        scene_id = _id(scene["id"], where + ".id")
        if scene_id in seen:
            _fail(where, "duplicate scene id")
        seen.add(scene_id)
        start = _number(scene["start"], where + ".start", 0, duration)
        end = _number(scene["end"], where + ".end", 0, duration)
        if start != previous or end <= start:
            _fail(where, "scenes must tile the timeline without gaps or overlaps")
        shapes = [_shape(item, where + f".shapes[{j}]", end - start) for j, item in enumerate(
            _list(scene["shapes"], where + ".shapes", 1, 64)
        )]
        if len({shape["id"] for shape in shapes}) != len(shapes):
            _fail(where, "duplicate shape id")
        total_shapes += len(shapes)
        total_keyframes += sum(len(shape["keyframes"]) for shape in shapes)
        if total_shapes > 1024 or total_keyframes > 4096:
            _fail("scenes", "too many shapes or keyframes")
        result["scenes"].append({
            "id": scene_id, "start": start, "end": end,
            "title": _text(scene["title"], where + ".title", 80),
            "description": _text(scene["description"], where + ".description", 500),
            "background": _color(scene["background"], where + ".background"),
            "shapes": shapes,
        })
        previous = end
    if previous != duration:
        _fail("scenes", "must cover the complete episode duration")
    previous = 0.0
    for i, caption in enumerate(_list(data["captions"], "captions", 1, 512)):
        where = f"captions[{i}]"
        caption = _object(caption, where, {"start", "end", "text"})
        start = _number(caption["start"], where + ".start", 0, duration)
        end = _number(caption["end"], where + ".end", 0, duration)
        if start < previous or end <= start:
            _fail(where, "captions must be ordered, non-overlapping, and have positive duration")
        result["captions"].append({
            "start": start, "end": end, "text": _text(caption["text"], where + ".text", 160),
        })
        previous = end
    if len(json.dumps(result, ensure_ascii=True).encode("utf-8")) > MAX_FILE_BYTES:
        _fail("episode", "normalized storyboard exceeds 1 MiB")
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("JSON", "duplicate object fields are not allowed")
        result[key] = value
    return result


def parse_episode(payload: bytes | str) -> dict:
    """Decode bounded UTF-8 JSON, rejecting duplicate fields and nonfinite values."""
    if not isinstance(payload, (bytes, str)):
        _fail("JSON", "must be UTF-8 text")
    try:
        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        if len(raw) > MAX_FILE_BYTES:
            _fail("JSON", "exceeds 1 MiB")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=lambda _: _fail("JSON", "nonfinite number"))
    except EpisodeValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        _fail("JSON", "invalid UTF-8 JSON")
    return validate_episode(data)


def load_episode(path: str | Path) -> dict:
    """Read at most 1 MiB from a regular local file; never follow a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            _fail("file", "must be a regular file")
        return parse_episode(source.read(MAX_FILE_BYTES + 1))
