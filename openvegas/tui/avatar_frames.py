"""Frame catalog for CLI dealer/avatar rendering."""

from __future__ import annotations

import os
from typing import Sequence

UNICODE_FRAMES: dict[str, Sequence[str]] = {
    "idle": ("(•‿•)", "(•ᴗ•)"),
    "typing": ("(•⌨•)", "(•⌨･)", "(･⌨•)"),
    "reading": ("(◔_◔)", "(◕_◕)", "(◔_◔)"),
    "waiting": ("(•…•)", "(• ..•)", "(• …•)"),
    "success": ("(•‿✓)", "(✓‿•)"),
    "error": ("(x‿x)", "(•!•)"),
    "walk": ("(•›•)", "(•‹•)"),
}

ASCII_FRAMES: dict[str, Sequence[str]] = {
    "idle": (":)", ":|"),
    "typing": ("[kbd]", "[KBD]"),
    "reading": ("[read]", "[READ]"),
    "waiting": ("[...]", "[ ..]", "[. .]"),
    "success": ("[ok]", "[OK]"),
    "error": ("[err]", "[ERR]"),
    "walk": ("/o", "o\\"),
}


def _unicode_allowed() -> bool:
    if str(os.getenv("OPENVEGAS_CLI_ASCII_SAFE", "0")).strip().lower() in {"1", "true", "yes", "on"}:
        return False
    return True


def frame_for_state(state: str, tick: int) -> str:
    token = str(state or "idle").strip().lower() or "idle"
    catalogs = UNICODE_FRAMES if _unicode_allowed() else ASCII_FRAMES
    frames = catalogs.get(token) or catalogs["idle"]
    if not frames:
        return "[dealer]"
    return str(frames[int(tick) % len(frames)])
