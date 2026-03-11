"""Terminal-safe confetti renderer for short win celebrations."""

from __future__ import annotations

import random
import time

from rich.console import Console


def render_confetti(console: Console, frames: int = 10, width: int = 52) -> None:
    """Render a bounded confetti burst.

    Keeps animation short to avoid blocking terminal interaction.
    """
    colors = ["red", "yellow", "green", "cyan", "magenta", "blue"]
    glyphs = ["*", "+", "x", "•"]
    for _ in range(max(1, frames)):
        line = "".join(
            f"[{random.choice(colors)}]{random.choice(glyphs)}[/]" for _ in range(max(8, width))
        )
        console.print(line)
        time.sleep(0.02)
