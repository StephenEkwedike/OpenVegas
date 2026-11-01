"""Visual system — colors, borders, spacing, terminal compatibility."""

from __future__ import annotations

import os


def ascii_safe_mode() -> bool:
    """True if terminal can't handle wide Unicode/emoji.
    Set OPENVEGAS_ASCII=1 to force ASCII mode."""
    if os.getenv("OPENVEGAS_ASCII", "0") == "1":
        return True
    lang = os.getenv("LANG", "") + os.getenv("LC_ALL", "")
    if "UTF" not in lang.upper() and "utf" not in lang:
        return True
    return False


def terminal_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def render_mode() -> str:
    """compact (<80), standard (80-119), cinematic (120+)."""
    w = terminal_width()
    if w < 80:
        return "compact"
    if w < 120:
        return "standard"
    return "cinematic"


# Color tokens
COLORS = {
    "win": "bold green",
    "loss": "red",
    "push": "yellow",
    "accent": "bold cyan",
    "muted": "dim",
    "danger": "bold red",
    "gold": "bold yellow",
}

# Border styles
BORDER_HEAVY = {"tl": "╔", "tr": "╗", "bl": "╚", "br": "╝", "h": "═", "v": "║"}
BORDER_LIGHT = {"tl": "┌", "tr": "┐", "bl": "└", "br": "┘", "h": "─", "v": "│"}
BORDER_ASCII = {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"}

# Animation cadence (seconds)
ANIM = {
    "intro_pause": 0.3,
    "frame_delay": 0.05,
    "resolve_pause": 0.5,
    "win_flash_count": 3,
    "win_flash_delay": 0.15,
}

# Themes
THEMES = {
    "retro_ascii": {
        "horse_glyph": "H>",
        "trail": "=",
        "empty": ".",
        "finish": "|",
        "cursor": "V",
        "card_border": BORDER_ASCII,
    },
    "classic_casino": {
        "horse_glyph": "🐎>",
        "trail": "█",
        "empty": "░",
        "finish": "║",
        "cursor": "▼",
        "card_border": BORDER_LIGHT,
    },
    "neon_arcade": {
        "horse_glyph": "𓃗>",
        "trail": "█",
        "empty": "░",
        "finish": "║",
        "cursor": "▼",
        "card_border": BORDER_HEAVY,
    },
}


def get_theme() -> dict:
    """Load active theme. Falls back to retro_ascii in ASCII-safe mode."""
    if ascii_safe_mode():
        return THEMES["retro_ascii"]
    try:
        from openvegas.config import load_config
        config = load_config()
        name = config.get("theme", "classic_casino")
    except Exception:
        name = "classic_casino"
    return THEMES.get(name, THEMES["classic_casino"])
