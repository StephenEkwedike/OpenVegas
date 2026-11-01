"""Roulette wheel result display and dense wheel animation."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from openvegas.tui.theme import ascii_safe_mode

RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
WHEEL_ORDER = [
    0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30,
    8, 23, 10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7,
    28, 12, 35, 3, 26,
]
N_SLOTS = len(WHEEL_ORDER)
SECTOR_ARC = 2 * math.pi / N_SLOTS
SPIN_DURATION_SEC = 8.0
SPIN_TURNS = 1
SPIN_WAIT_SECTORS_PER_SEC = 12.0


@dataclass(frozen=True)
class _Geometry:
    canvas_w: int
    canvas_h: int
    aspect: float
    outer_rim_r: float
    pocket_outer: float
    pocket_inner: float
    inner_rim_r: float
    spoke_r: float
    center_r: float


def _window_for_width(width: int) -> int:
    if width >= 65:
        return 13
    if width >= 45:
        return 9
    return 5


def _geometry_for_window(window: int) -> _Geometry:
    if window == 1:
        return _Geometry(canvas_w=26, canvas_h=10, aspect=2.5,
                         outer_rim_r=4.2, pocket_outer=3.9, pocket_inner=2.5,
                         inner_rim_r=2.3, spoke_r=1.8, center_r=0.6)
    if window >= 13:
        return _Geometry(
            canvas_w=52,
            canvas_h=22,
            aspect=2.1,
            outer_rim_r=9.8,
            pocket_outer=9.1,
            pocket_inner=6.3,
            inner_rim_r=6.0,
            spoke_r=3.9,
            center_r=1.4,
        )
    if window >= 9:
        return _Geometry(
            canvas_w=40,
            canvas_h=18,
            aspect=2.0,
            outer_rim_r=7.8,
            pocket_outer=7.1,
            pocket_inner=5.0,
            inner_rim_r=4.8,
            spoke_r=3.1,
            center_r=1.2,
        )
    return _Geometry(
        canvas_w=30,
        canvas_h=14,
        aspect=1.8,
        outer_rim_r=5.9,
        pocket_outer=5.5,
        pocket_inner=3.8,
        inner_rim_r=3.5,
        spoke_r=2.2,
        center_r=1.0,
    )


def render_result(result: int, bet_type: str, hit: bool, payout_mult: str) -> str:
    """Render roulette result display with dynamic width-safe borders."""
    ascii_safe = ascii_safe_mode()
    hit_mark = "YES" if hit else "NO"
    bet_display = bet_type.replace("bet_", "").upper()
    result_display = str(int(result))

    if ascii_safe:
        rows = [
            f"Result: {result_display}",
            f"Bet: {bet_display}",
            f"Hit: {hit_mark}",
            f"Payout: {payout_mult}x",
        ]
        width = max(len("ROULETTE"), *(len(r) for r in rows))
        top = "+" + "-" * (width + 2) + "+"
        title = "| " + "ROULETTE".center(width) + " |"
        body = "\n".join(f"| {row.ljust(width)} |" for row in rows)
        return f"{top}\n{title}\n{top}\n{body}\n{top}"

    rows = [
        f"Result: {result_display}",
        f"Bet: {bet_display}",
        f"Hit: {hit_mark}",
        f"Payout: {payout_mult}x",
    ]
    width = max(len("◎ ROULETTE ◎"), *(len(r) for r in rows))
    top = "╔" + "═" * (width + 2) + "╗"
    title = "║ " + "◎ ROULETTE ◎".center(width) + " ║"
    sep = "╠" + "═" * (width + 2) + "╣"
    body = "\n".join(f"║ {row.ljust(width)} ║" for row in rows)
    bottom = "╚" + "═" * (width + 2) + "╝"
    return f"{top}\n{title}\n{sep}\n{body}\n{bottom}"


def _number_styles(n: int) -> tuple[str, str]:
    """Return (label_style, pocket_bg_style)."""
    if n == 0:
        return ("bold white on #17805b", "on #17805b")
    if n in RED_NUMBERS:
        return ("bold white on #b62b3e", "on #b62b3e")
    return ("bold white on #192321", "on #192321")


def _cell_dist(x: int, y: int, *, cx: int, cy: int, aspect: float) -> float:
    dx = (x - cx) / aspect
    dy = y - cy
    return math.sqrt(dx * dx + dy * dy)


def _cell_angle(x: int, y: int, *, cx: int, cy: int, aspect: float) -> float:
    dx = (x - cx) / aspect
    dy = y - cy
    return math.atan2(dy, dx)


def _sector_index(angle: float, rotation: int) -> int:
    # Twelve o'clock is the 0-angle for visible wheel index.
    # Screen y grows downwards, matching the clockwise label positions below.
    a = (angle + math.pi / 2) % (2 * math.pi)
    visible_idx = int(a / SECTOR_ARC) % N_SLOTS
    return (visible_idx + rotation) % N_SLOTS


def _label_positions(geom: _Geometry) -> list[tuple[int, int]]:
    positions: list[tuple[int, int]] = []
    cx = geom.canvas_w // 2
    cy = geom.canvas_h // 2
    r = (geom.pocket_outer + geom.pocket_inner) / 2
    for i in range(N_SLOTS):
        angle = math.pi / 2 - (i + 0.5) * SECTOR_ARC
        x = cx + round(r * math.cos(angle) * geom.aspect)
        y = cy - round(r * math.sin(angle))
        positions.append((x, y))
    return positions


def _build_frame(rotation: int, *, ball_sector: int | None, window: int) -> str:
    geom = _geometry_for_window(window)
    cx = geom.canvas_w // 2
    cy = geom.canvas_h // 2

    cell_char = [[" "] * geom.canvas_w for _ in range(geom.canvas_h)]
    cell_style = [[""] * geom.canvas_w for _ in range(geom.canvas_h)]

    for y in range(geom.canvas_h):
        for x in range(geom.canvas_w):
            d = _cell_dist(x, y, cx=cx, cy=cy, aspect=geom.aspect)
            a = _cell_angle(x, y, cx=cx, cy=cy, aspect=geom.aspect)
            si = _sector_index(a, rotation)
            num = WHEEL_ORDER[si]
            _, pocket_bg = _number_styles(num)

            if geom.pocket_outer < d <= geom.outer_rim_r:
                cell_char[y][x] = "▓"
                cell_style[y][x] = "rgb(180,140,60)"
            elif geom.pocket_inner < d <= geom.pocket_outer:
                is_ball = ball_sector is not None and si == ball_sector
                if is_ball:
                    cell_char[y][x] = " "
                    cell_style[y][x] = "on yellow"
                else:
                    cell_char[y][x] = " "
                    cell_style[y][x] = pocket_bg
            elif geom.inner_rim_r < d <= geom.pocket_inner:
                cell_char[y][x] = "░"
                cell_style[y][x] = "rgb(100,100,110)"
            elif geom.center_r < d <= geom.spoke_r:
                spoke_angle = a % (math.pi / 4)
                if spoke_angle < 0.08 or spoke_angle > (math.pi / 4 - 0.08):
                    cell_char[y][x] = "│" if abs(math.cos(a)) < 0.5 else "─"
                    cell_style[y][x] = "rgb(180,160,80)"
                else:
                    cell_char[y][x] = " "
                    cell_style[y][x] = "on rgb(20,35,20)"
            elif d <= geom.center_r:
                cell_char[y][x] = " "
                cell_style[y][x] = "on rgb(30,50,30)"

    # Reserve the ball label first. Rounded terminal cells can put adjacent
    # pockets on the same row; a later label must never turn "19" into "1".
    positions = _label_positions(geom)
    selected = (ball_sector - rotation) % N_SLOTS if ball_sector is not None else None
    order = ([selected] if selected is not None else []) + [i for i in range(N_SLOTS) if i != selected]
    occupied: set[tuple[int, int]] = set()
    for visible_idx in order:
        if window == 1 and visible_idx != selected:
            continue
        lx, ly = positions[visible_idx]
        wheel_idx = (visible_idx + rotation) % N_SLOTS
        number = WHEEL_ORDER[wheel_idx]
        is_ball = ball_sector is not None and wheel_idx == ball_sector
        label = f"●{number:>2}" if is_ball else f"{number:>2}"
        label_style, _ = _number_styles(number)
        style = "bold black on yellow" if is_ball else label_style
        start = lx - len(label) // 2
        if any((x, ly) in occupied for x in range(start, start + len(label))):
            continue
        occupied.update((x, ly) for x in range(start - 1, start + len(label) + 1))
        if is_ball:
            for x in (start - 1, start + len(label)):
                if 0 <= x < geom.canvas_w:
                    cell_char[ly][x] = " "
        for ci, ch in enumerate(label):
            xx = start + ci
            if 0 <= ly < geom.canvas_h and 0 <= xx < geom.canvas_w:
                cell_char[ly][xx] = ch
                cell_style[ly][xx] = style

    # Hub label.
    hub_text = " ◎ OV "
    hx = cx - len(hub_text) // 2
    for i, ch in enumerate(hub_text):
        xx = hx + i
        if 0 <= cy < geom.canvas_h and 0 <= xx < geom.canvas_w:
            cell_char[cy][xx] = ch
            cell_style[cy][xx] = "bold rgb(200,180,80) on rgb(30,50,30)"

    lines: list[str] = []

    for y in range(geom.canvas_h):
        parts: list[str] = []
        for x in range(geom.canvas_w):
            ch = cell_char[y][x]
            st = cell_style[y][x]
            if st:
                parts.append(f"[{st}]{ch}[/{st}]")
            else:
                parts.append(ch)
        lines.append("".join(parts))

    if window == 1:
        lines.append("[bold yellow]Yellow = ball[/bold yellow]")
        return "\n".join(lines)
    lines.append("")
    lines.append(
        "  [bold white on #17805b] 0 [/bold white on #17805b] green   "
        "[bold white on #b62b3e]   [/bold white on #b62b3e] red   "
        "[bold white on #192321]   [/bold white on #192321] black   "
        "[bold black on yellow] ● [/bold black on yellow] ball"
    )
    return "\n".join(lines)


def _spin_frame(ball_index: int, *, window: int) -> Text:
    """Build one wheel frame for a given wheel rotation index."""
    markup = _build_frame(ball_index % N_SLOTS, ball_sector=ball_index % N_SLOTS, window=window)
    return Text.from_markup(markup)


def _phase_label(progress: float) -> str:
    if progress < 0.10:
        return "Croupier spins the wheel..."
    if progress < 0.35:
        return "No more bets!"
    if progress < 0.65:
        return "Ball bouncing..."
    if progress < 0.90:
        return "Ball settling..."
    return "Ball drops into pocket"


def _coast_progress(t: float) -> float:
    # Constant angular deceleration. A quintic front-loaded almost all travel.
    return t * (2.0 - t)


def spin_position(elapsed: float, target: int, *, start: int = 0,
                  duration: float = SPIN_DURATION_SEC) -> float:
    """Unwrapped clockwise travel: decelerate to the server pocket without a jump."""
    progress = max(0.0, min(1.0, elapsed / duration)) if duration > 0 else 1.0
    distance = N_SLOTS * SPIN_TURNS + (target - start) % N_SLOTS
    return start + distance * _coast_progress(progress)


async def animate_spin(
    console: Console,
    *,
    result_number: int,
    frames: int | None = None,
    duration_sec: float | None = None,
    fps: int = 12,
) -> None:
    """Animate roulette wheel with dense circular layout and slower pacing."""
    if ascii_safe_mode():
        with console.status("Spinning wheel...", spinner="line"):
            await asyncio.sleep(0.9 if duration_sec is None else max(0.3, duration_sec * 0.5))
        return

    if duration_sec is None:
        # Default gameplay pacing.
        duration_sec = SPIN_DURATION_SEC if frames is None else max(0.25, frames / 40)

    total_frames = max(1, int(frames) if frames is not None else int(duration_sec * fps))
    frame_delay = max(0.005, duration_sec / max(1, total_frames))
    target_idx = WHEEL_ORDER.index(int(result_number)) if int(result_number) in WHEEL_ORDER else 0
    window = _window_for_width(int(getattr(console, "width", 80) or 80))

    with Live(console=console, refresh_per_second=max(1, fps), transient=False) as live:
        for frame in range(total_frames):
            progress = frame / max(1, total_frames - 1) if total_frames > 1 else 1.0
            current = int(spin_position(progress * duration_sec, target_idx,
                                        duration=duration_sec)) % N_SLOTS

            phase = _phase_label(frame / max(1, total_frames))
            frame_text = _spin_frame(current, window=window)
            live.update(
                Panel(
                    Align.center(frame_text),
                    title="[bold magenta]🎰 Roulette[/bold magenta]",
                    subtitle=f"[bold] {phase} [/bold]",
                    border_style="rgb(180,140,60)",
                    padding=(0, 1),
                )
            )
            await asyncio.sleep(frame_delay)

        # Hold the final pocket briefly in normal runtime (skip for tiny test runs).
        hold = 2.0 if frames is None else 0.0
        if hold > 0:
            color = "green" if result_number == 0 else "red" if result_number in RED_NUMBERS else "white"
            final_text = _spin_frame(target_idx, window=window)
            live.update(
                Panel(
                    Align.center(final_text),
                    title="[bold magenta]🎰 Roulette[/bold magenta]",
                    subtitle=f"[bold {color}] ● Ball lands on {result_number}! [/bold {color}]",
                    border_style="green",
                    padding=(0, 1),
                )
            )
            await asyncio.sleep(hold)
