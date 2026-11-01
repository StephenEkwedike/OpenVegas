"""Chat presentation style tokens and normalization helpers."""

from __future__ import annotations

CHAT_STYLES: dict[str, dict[str, str]] = {
    "codex": {
        "assistant_bullet": "white",
        "tool_bullet": "green",
        "accent": "bright_blue",
        "muted": "grey62",
    },
    "claude": {
        "assistant_bullet": "white",
        "tool_bullet": "green",
        "accent": "cornflower_blue",
        "muted": "grey58",
    },
    "minimal": {
        "assistant_bullet": "white",
        "tool_bullet": "white",
        "accent": "white",
        "muted": "grey58",
    },
    "raw": {
        "assistant_bullet": "white",
        "tool_bullet": "white",
        "accent": "white",
        "muted": "grey58",
    },
}

VALID_CHAT_STYLES = tuple(CHAT_STYLES.keys())
VALID_TOOL_EVENT_DENSITY = ("compact", "verbose")
VALID_APPROVAL_UI = ("menu", "confirm")


def normalize_chat_style(style: str | None) -> str:
    token = str(style or "").strip().lower()
    if token in CHAT_STYLES:
        return token
    return "codex"


def normalize_tool_event_density(density: str | None) -> str:
    token = str(density or "").strip().lower()
    if token in VALID_TOOL_EVENT_DENSITY:
        return token
    return "compact"


def normalize_approval_ui(mode: str | None) -> str:
    token = str(mode or "").strip().lower()
    if token in VALID_APPROVAL_UI:
        return token
    return "menu"

