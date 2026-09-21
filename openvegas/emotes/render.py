"""Pure frame conversion; only the caller's compositor may write the result."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping

from PIL import Image
from rich.text import Text


def fit_frame(
    frame: Image.Image | None, *, max_columns: int, max_rows: int
) -> Image.Image | None:
    """Fit an owned cell region using one integer pixel divisor, never stretching.

    Each terminal cell holds two vertical pixels. Integer nearest-neighbor
    downsampling trades detail for a bounded region; art approval must use the
    actual terminal size. The original cached image remains untouched.
    """
    if frame is None or max_columns < 1 or max_rows < 1:
        return None
    divisor = max(
        1,
        math.ceil(frame.width / max_columns),
        math.ceil(frame.height / (2 * max_rows)),
    )
    return frame.resize(
        (max(1, frame.width // divisor), max(1, frame.height // divisor)),
        Image.Resampling.NEAREST,
    )


def motion_allowed(*, is_tty: bool, environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (
        is_tty
        and "NO_COLOR" not in env
        and env.get("TERM", "") != "dumb"
        and env.get("OPENVEGAS_REDUCED_MOTION", "").lower() not in {"1", "true", "yes"}
    )


def _cells(frame: Image.Image, *, scale: int = 1, background=(0, 0, 0)):
    if type(scale) is not int or not 1 <= scale <= 4:
        raise ValueError("Scale must be an integer from 1 to 4")
    if len(background) != 3 or any(
        type(v) is not int or not 0 <= v <= 255 for v in background
    ):
        raise ValueError("Background must be an RGB tuple")
    if frame.width * scale > 512 or frame.height * scale > 512:
        raise ValueError("Frame exceeds terminal render limits")
    rgba = frame.convert("RGBA")
    if scale != 1:
        rgba = rgba.resize(
            (rgba.width * scale, rgba.height * scale), Image.Resampling.NEAREST
        )

    def color(pixel):
        r, g, b, a = pixel
        if not a:
            return None
        channels = tuple(
            (c * a + bg * (255 - a) + 127) // 255
            for c, bg in zip((r, g, b), background)
        )
        return "#" + "".join(f"{channel:02x}" for channel in channels)

    for y in range(0, rgba.height, 2):
        for x in range(rgba.width):
            top = color(rgba.getpixel((x, y)))
            bottom = color(rgba.getpixel((x, y + 1))) if y + 1 < rgba.height else None
            if top and bottom:
                yield "\u2580", top, bottom
            elif top:
                yield "\u2580", top, None
            elif bottom:
                yield "\u2584", bottom, None
            else:
                yield " ", None, None
        if y + 2 < rgba.height:
            yield "\n", None, None


def rich_frame(
    frame: Image.Image | None, *, scale: int = 1, background=(0, 0, 0)
) -> Text:
    result = Text(no_wrap=True, overflow="crop")
    if frame is not None:
        for glyph, fg, bg in _cells(frame, scale=scale, background=background):
            result.append(glyph, style=(fg or "") + (" on " + bg if bg else ""))
    return result


def prompt_toolkit_fragments(
    frame: Image.Image | None, *, scale: int = 1, background=(0, 0, 0)
) -> list[tuple[str, str]]:
    if frame is None:
        return []
    return [
        (
            " ".join(
                filter(None, ("fg:" + fg if fg else "", "bg:" + bg if bg else ""))
            ),
            glyph,
        )
        for glyph, fg, bg in _cells(frame, scale=scale, background=background)
    ]
