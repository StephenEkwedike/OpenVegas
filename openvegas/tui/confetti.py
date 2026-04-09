"""Terminal-safe confetti renderer for short win celebrations."""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass

from rich.cells import cell_len
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.segment import Segment
from rich.text import Text

_ASCII_GLYPHS = ("*", "+", "x", ".")
_CONFETTI_COLORS = (
    "bright_red",
    "bright_yellow",
    "bright_green",
    "bright_cyan",
    "bright_magenta",
    "bright_blue",
)
_PANEL_MIN_WIDTH = 24
_VERY_NARROW_FALLBACK_WIDTH = 28


@dataclass
class _FrameLayout:
    panel_lines: list[Text]
    frame_width: int
    left_confetti_width: int
    right_confetti_width: int
    panel_width: int


def render_confetti(console: Console, frames: int = 10, width: int = 52) -> None:
    """Render a bounded confetti burst.

    Keeps animation short to avoid blocking terminal interaction.
    """
    colors = ["red", "yellow", "green", "cyan", "magenta", "blue"]
    glyphs = ["*", "+", "x", "."]
    for _ in range(max(1, frames)):
        line = "".join(
            f"[{random.choice(colors)}]{random.choice(glyphs)}[/]" for _ in range(max(8, width))
        )
        console.print(line)
        time.sleep(0.02)


def _build_panel(content: RenderableType, title: str, panel_width: int | None = None) -> Panel:
    kwargs: dict = {"title": title, "expand": False}
    if panel_width is not None:
        kwargs["width"] = panel_width
    return Panel(content, **kwargs)


def _capture_panel_lines(console: Console, renderable: RenderableType) -> list[Text]:
    """Render panel into styled line objects for frame composition."""
    temp_console = Console(
        width=max(20, console.width),
        record=True,
        force_terminal=True,
        color_system=console.color_system,
        highlight=False,
        legacy_windows=False,
    )
    temp_console.print(renderable)

    lines: list[Text] = []
    segments = list(Segment.filter_control(temp_console._record_buffer))
    for line_segments in Segment.split_lines(segments):
        text_line = Text()
        for segment in line_segments:
            text_line.append(segment.text, style=segment.style)
        lines.append(text_line)

    return lines or [Text("")]


def _text_width(value: Text) -> int:
    return cell_len(value.plain)


def _pad_or_crop_line(value: Text, width: int) -> Text:
    line = value.copy()
    line.truncate(width, overflow="crop", pad=False)
    delta = width - _text_width(line)
    if delta > 0:
        line.append(" " * delta)
    return line


def _target_frame_width(console: Console) -> int:
    widths: list[int] = []
    try:
        private_w = int(getattr(console, "_width", 0) or 0)
        if private_w > 0:
            widths.append(private_w)
    except Exception:
        pass
    try:
        w = int(getattr(console, "width", 0) or 0)
        if w > 0:
            widths.append(w)
    except Exception:
        pass
    try:
        size = getattr(console, "size", None)
        w2 = int(getattr(size, "width", 0) or 0)
        if w2 > 0:
            widths.append(w2)
    except Exception:
        pass
    effective = min(widths) if widths else 80
    return max(20, effective - 2)


def _compute_layout(
    console: Console,
    content: RenderableType,
    title: str,
    panel_width: int | None,
) -> _FrameLayout | None:
    target_frame_width = _target_frame_width(console)
    if target_frame_width < _VERY_NARROW_FALLBACK_WIDTH:
        return None

    requested_panel_width = panel_width
    while True:
        panel_lines = _capture_panel_lines(console, _build_panel(content, title, requested_panel_width))
        panel_line_width = max(_text_width(line) for line in panel_lines)

        if panel_line_width <= target_frame_width:
            side_total = target_frame_width - panel_line_width
            left_width = side_total // 2
            right_width = side_total - left_width
            return _FrameLayout(
                panel_lines=panel_lines,
                frame_width=target_frame_width,
                left_confetti_width=left_width,
                right_confetti_width=right_width,
                panel_width=panel_line_width,
            )

        next_width = (requested_panel_width if requested_panel_width is not None else target_frame_width) - 1
        if next_width < _PANEL_MIN_WIDTH:
            return None
        requested_panel_width = next_width


def _random_confetti_text(width: int, rng: random.Random) -> Text:
    row = Text()
    for _ in range(max(0, width)):
        row.append(rng.choice(_ASCII_GLYPHS), style=rng.choice(_CONFETTI_COLORS))
    return row


def _build_confetti_frame(layout: _FrameLayout, pad_y: int, rng: random.Random) -> Group:
    rows: list[Text] = []

    for _ in range(max(0, pad_y)):
        rows.append(_random_confetti_text(layout.frame_width, rng))

    for panel_line in layout.panel_lines:
        row = Text()
        if layout.left_confetti_width > 0:
            row.append_text(_random_confetti_text(layout.left_confetti_width, rng))
        row.append_text(_pad_or_crop_line(panel_line, layout.panel_width))
        if layout.right_confetti_width > 0:
            row.append_text(_random_confetti_text(layout.right_confetti_width, rng))
        rows.append(row)

    for _ in range(max(0, pad_y)):
        rows.append(_random_confetti_text(layout.frame_width, rng))

    return Group(*rows)


def _final_seed(layout: _FrameLayout, pad_y: int) -> int:
    plain_lines = "\n".join(line.plain for line in layout.panel_lines)
    seed_text = (
        f"{layout.frame_width}|{layout.left_confetti_width}|{layout.right_confetti_width}|"
        f"{layout.panel_width}|{pad_y}|{plain_lines}"
    )
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def render_panel_with_confetti(
    console: Console,
    content: RenderableType,
    title: str = "Result",
    *,
    animate: bool = True,
    frames: int = 10,
    frame_delay: float = 0.25,
    confetti_pad_x: int = 4,
    confetti_pad_y: int = 3,
    panel_width: int | None = None,
    persist: bool = True,
) -> None:
    panel = _build_panel(content, title, panel_width)

    force_terminal = bool(getattr(console, "_force_terminal", False))
    if not console.is_terminal or (console.is_dumb_terminal and not force_terminal):
        console.print(panel)
        return

    layout = _compute_layout(console, content, title, panel_width)
    if layout is None:
        console.print(panel)
        return

    # Keep minimum side confetti width preference where possible.
    if (layout.left_confetti_width + layout.right_confetti_width) < (confetti_pad_x * 2):
        available = layout.left_confetti_width + layout.right_confetti_width
        layout.left_confetti_width = available // 2
        layout.right_confetti_width = available - layout.left_confetti_width

    if animate:
        try:
            with Live(Group(Text("")), console=console, transient=True, refresh_per_second=30) as live:
                for _ in range(max(1, frames)):
                    live.update(_build_confetti_frame(layout, confetti_pad_y, random.Random()))
                    time.sleep(max(0.0, frame_delay))
        except KeyboardInterrupt:
            console.print(panel)
            return
        except Exception:
            console.print(panel)
            return

    if persist:
        final_frame = _build_confetti_frame(layout, confetti_pad_y, random.Random(_final_seed(layout, confetti_pad_y)))
        console.print(final_frame)


def render_result_panel(
    console: Console,
    content: RenderableType,
    *,
    is_win: bool,
    animation_enabled: bool,
    title: str = "Result",
) -> None:
    """Render result output through a single styling path.

    Win outcomes get result-centered confetti framing; everything else stays a
    standard panel for readability and consistency.
    """
    if not is_win:
        console.print(Panel(content, title=title))
        return

    render_panel_with_confetti(
        console,
        content,
        title=title,
        animate=bool(animation_enabled),
        persist=True,
    )
