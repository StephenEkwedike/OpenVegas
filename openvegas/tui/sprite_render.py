"""Terminal sprite renderer using half-block truecolor output."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from rich.text import Text

FRAME_W = 16
FRAME_H = 32
STATE_ROWS: dict[str, int] = {
    "idle": 0,
    "walk": 1,
    "typing": 2,
    "reading": 3,
    "waiting": 4,
    "success": 5,
    "error": 6,
}


def supports_truecolor() -> bool:
    colorterm = str(os.getenv("COLORTERM", "")).strip().lower()
    return colorterm in {"truecolor", "24bit"}


def _direction_row_from_state(state: str, rows: int) -> int:
    token = str(state or "idle").strip().lower()
    if token == "walk":
        return min(1, max(0, rows - 1))
    if token in {"typing", "reading", "waiting"}:
        return min(2, max(0, rows - 1))
    return 0


class TerminalSpriteRenderer:
    def __init__(self, sheet_path: str | Path):
        self._enabled = False
        self._sheet: Any | None = None
        self._reason = ""
        self._sheet_path = Path(sheet_path).expanduser()
        self._rows = 0
        self._cols = 0
        self._model = "none"  # none | state_rows | direction_rows | single_row

        if not supports_truecolor():
            self._reason = "terminal_no_truecolor"
            return

        try:
            from PIL import Image  # type: ignore
        except Exception:
            self._reason = "pillow_missing"
            return

        if not self._sheet_path.exists():
            self._reason = "sheet_missing"
            return

        try:
            sheet = Image.open(self._sheet_path).convert("RGBA")
        except Exception:
            self._reason = "sheet_load_failed"
            return

        cols = int(getattr(sheet, "width", 0) // FRAME_W)
        rows = int(getattr(sheet, "height", 0) // FRAME_H)
        if cols < 1 or rows < 1:
            self._reason = "sheet_dimensions_invalid"
            return

        if rows >= 7:
            model = "state_rows"
        elif rows >= 3:
            model = "direction_rows"
        else:
            model = "single_row"

        self._sheet = sheet
        self._rows = rows
        self._cols = cols
        self._model = model
        self._enabled = True

    def enabled(self) -> bool:
        return bool(self._enabled and self._sheet is not None)

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def path(self) -> str:
        return str(self._sheet_path)

    def _extract_frame(self, state: str, tick: int):
        if self._sheet is None:
            return None
        token = str(state or "idle").strip().lower()
        col = int(tick) % max(1, self._cols)
        if self._model == "state_rows":
            row = min(STATE_ROWS.get(token, 0), self._rows - 1)
        elif self._model == "direction_rows":
            row = _direction_row_from_state(token, self._rows)
        else:
            row = 0
        x = col * FRAME_W
        y = row * FRAME_H
        return self._sheet.crop((x, y, x + FRAME_W, y + FRAME_H))

    @staticmethod
    def _rgb(r: int, g: int, b: int) -> str:
        return f"rgb({r},{g},{b})"

    def render(self, state: str = "idle", tick: int = 0) -> Text:
        frame = self._extract_frame(state, tick)
        if frame is None:
            return Text("")

        out = Text()
        for char_row in range(0, FRAME_H, 2):
            line = Text()
            for x in range(FRAME_W):
                top_r, top_g, top_b, top_a = frame.getpixel((x, char_row))
                bot_r, bot_g, bot_b, bot_a = frame.getpixel((x, char_row + 1))
                top_vis = int(top_a) > 40
                bot_vis = int(bot_a) > 40
                if top_vis and bot_vis:
                    line.append(
                        "▀",
                        style=f"{self._rgb(top_r, top_g, top_b)} on {self._rgb(bot_r, bot_g, bot_b)}",
                    )
                elif top_vis:
                    line.append("▀", style=self._rgb(top_r, top_g, top_b))
                elif bot_vis:
                    line.append("▄", style=self._rgb(bot_r, bot_g, bot_b))
                else:
                    line.append(" ")
            out.append(line)
            if char_row < (FRAME_H - 2):
                out.append("\n")
        return out

