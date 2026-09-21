"""Deterministic prompt-toolkit cell previews, NOT native terminal screenshots.

Explicit public-art preview with synthetic history/input/lifecycle. No backend,
microphone, credentials, paid calls, global settings, or customer transcripts.
Run from the repo: .venv/bin/python tests/test_emotes/compositor_sizing_preview.py
Writes four labeled PNGs and dimensions.json beneath --output (default /tmp).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image, ImageDraw, ImageFont
from prompt_toolkit import PromptSession
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import ANSI_COLORS_TO_RGB, Vt100_Output
from rich.console import Console

from openvegas.emotes.bridge import ChatEmoteBridge
from openvegas.emotes.compositor import OwnedChatCompositor
from openvegas.emotes.render import fit_frame
from openvegas.emotes.resources import PackRepository


def _font():
    for path in (
        "/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "C:/Windows/Fonts/consola.ttf",
    ):
        if Path(path).is_file():
            return ImageFont.truetype(path, 16)
    raise RuntimeError("No known monospace font installed; do not substitute a misleading font")


def _color(value, default):
    if value in ANSI_COLORS_TO_RGB:
        return ANSI_COLORS_TO_RGB[value]
    return "#" + value if value and len(value) == 6 else default


def _save_cells(owner, destination):
    size = owner.app.output.get_size()
    cell_w, cell_h, heading = 10, 20, 40
    canvas = Image.new("RGB", (size.columns * cell_w, size.rows * cell_h + heading), "#111318")
    draw, font = ImageDraw.Draw(canvas), _font()
    draw.text((10, 10), f"SOFTWARE CELL PREVIEW {size.columns}x{size.rows} | NOT A NATIVE SCREENSHOT", font=font, fill="#edc861")
    screen = owner.app.renderer.last_rendered_screen
    for y in range(size.rows):
        for x in range(size.columns):
            cell = screen.data_buffer[y][x]
            attrs = owner.app._merged_style.get_attrs_for_style_str(cell.style)
            fg, bg = _color(attrs.color, "#d0d5dc"), _color(attrs.bgcolor, "#111318")
            if attrs.reverse:
                fg, bg = bg, fg
            left, top = x * cell_w, heading + y * cell_h
            draw.rectangle((left, top, left + cell_w - 1, top + cell_h - 1), fill=bg)
            if cell.char in {"\u2580", "\u2584"}:
                offset = 0 if cell.char == "\u2580" else cell_h // 2
                draw.rectangle((left, top + offset, left + cell_w - 1, top + offset + cell_h // 2 - 1), fill=fg)
            elif cell.char.strip():
                draw.text((left, top + 1), cell.char, font=font, fill=fg)
    canvas.save(destination)


async def capture(output):
    output.mkdir(parents=True, exist_ok=True)
    repository = PackRepository()
    report = []
    for columns, rows in [(80, 24), (160, 48)]:
        with create_pipe_input() as pipe:
            terminal = Vt100_Output(io.StringIO(), lambda rows=rows, columns=columns: Size(rows=rows, columns=columns), enable_cpr=False)
            clock = [0.0]
            owner = OwnedChatCompositor(
                PromptSession(input=pipe, output=terminal), Console(file=io.StringIO()),
                session_id="sizing-preview", pack=repository.load("pixel-courier"),
                completion_pack=repository.load("skyline-dunk"), access_guard=lambda: True,
                reduced_motion=False, clock=lambda clock=clock: clock[0], replay_on_close=False,
            )
            async with owner:
                owner.append_output("".join(f"Offline history row {i:02d}: task output remains selectable.\n" for i in range(60)))
                prompt = asyncio.create_task(owner.prompt_async(
                    "chat: ", default="My typed draft stays here. " * (columns // 7),
                    rprompt="1 attachment (fixture)",
                    bottom_toolbar=[("class:bottom-toolbar.voice-chip-active", " mic Listening [fixture] "), ("", "  + Actions")],
                ))
                bridge = ChatEmoteBridge("sizing-preview", publish=owner.publish)
                token = bridge.begin()
                clock[0] = 0.12
                for phase in ("working", "complete"):
                    if phase == "complete":
                        bridge.finish(success=True, turn=token)
                        clock[0] += 1.5
                    owner.tick()
                    owner.app.invalidate()
                    await asyncio.sleep(0.08)
                    path = output / f"dock-{columns}x{rows}-{phase}.png"
                    _save_cells(owner, path)
                    positions = owner.app.renderer.last_rendered_screen.visible_windows_to_write_positions
                    frame = fit_frame(owner.controller.current_frame(), max_columns=columns - 2, max_rows=owner.dock_height)
                    report.append({
                        "kind": "software-cell-preview-not-native", "viewport": [columns, rows],
                        "phase": phase, "dock_rows": owner.dock_height, "fitted_pixels": list(frame.size),
                        "history_rows": positions[owner.history_window].height,
                        "input_rows": positions[owner.input_window].height, "path": str(path),
                    })
                owner.request_command("/exit")
                await prompt
                bridge.close()
    (output / "dimensions.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/openvegas-dock-review"))
    args = parser.parse_args()
    asyncio.run(capture(args.output))
