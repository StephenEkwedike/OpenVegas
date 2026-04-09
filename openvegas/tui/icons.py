"""Terminal-safe icon helpers for OpenVegas TUI."""

from __future__ import annotations

import os
import subprocess
import sys

_NERD_FONT_DETECTED: bool | None = None


def _has_nerd_font() -> bool:
    explicit = os.getenv("OPENVEGAS_NERD_FONT", "").strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return False

    term_program = os.getenv("TERM_PROGRAM", "").strip().lower()
    if term_program in {"wezterm", "kitty"}:
        return True

    p10k = os.getenv("POWERLEVEL9K_MODE", "") or os.getenv("P9K_MODE", "")
    if "nerdfont" in p10k.lower():
        return True

    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["fc-list", ":family"],
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            haystack = str(result.stdout or "").lower()
            nerd_indicators = ["nerd", "meslo", "firacode nf", "jetbrains mono nf", "hack nf"]
            if any(ind in haystack for ind in nerd_indicators):
                return True
        except Exception:
            pass

    return False


def nerd_font_available() -> bool:
    global _NERD_FONT_DETECTED
    if _NERD_FONT_DETECTED is None:
        _NERD_FONT_DETECTED = _has_nerd_font()
    return bool(_NERD_FONT_DETECTED)


def mic_icon() -> str:
    if nerd_font_available():
        return "\uf130"  # Font Awesome mic in Nerd Font
    # Non-emoji fallback for terminals without Nerd Fonts
    return "◉"
