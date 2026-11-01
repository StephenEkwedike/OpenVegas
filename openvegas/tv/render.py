"""Shared timed-vector evaluation and deterministic local terminal rasterization."""

from __future__ import annotations

import math
from itertools import groupby

from PIL import Image, ImageColor, ImageDraw, ImageFont
from rich.style import Style
from rich.text import Text

from .schema import validate_episode


def _frame(episode: dict, seconds: float) -> dict:
    if type(seconds) not in (int, float) or seconds < 0 or (
        type(seconds) is float and not math.isfinite(seconds)
    ):
        raise ValueError("seconds must be a finite, nonnegative number")
    duration = episode["duration_seconds"]
    at = float(min(seconds, duration))
    ended = at >= duration
    scene = next((scene for scene in episode["scenes"] if scene["start"] <= at < scene["end"]),
                 episode["scenes"][-1])
    local = at - scene["start"]
    shapes = []
    for shape in scene["shapes"]:
        resolved = {key: value for key, value in shape.items() if key != "keyframes"}
        keys = shape["keyframes"]
        if keys:
            left = keys[0]
            right = keys[-1]
            for key in keys[1:]:
                if key["at"] >= local:
                    right = key
                    break
                left = key
            span = right["at"] - left["at"]
            ratio = max(0, min(1, (local - left["at"]) / span)) if span else 0
            for field in ("x", "y", "opacity"):
                resolved[field] = left[field] + (right[field] - left[field]) * ratio
        shapes.append(resolved)
    caption = next((c["text"] for c in episode["captions"] if c["start"] <= at < c["end"]), "")
    return {
        "episode_id": episode["id"], "channel_id": episode["channel_id"],
        "time": at, "duration_seconds": duration, "ended": ended,
        "canvas": dict(episode["canvas"]),
        "scene": {**{k: v for k, v in scene.items() if k != "shapes"}, "shapes": shapes},
        "scene_time": local, "caption": caption,
    }


def frame_at(episode: dict, seconds: float) -> dict:
    """Validate and evaluate an episode. The returned frame contains no keyframes.

    At duration the last scene is held, caption is empty, and ended is true.
    Web clients can use the same interpolation on catalog storyboards.
    """
    return _frame(validate_episode(episode), seconds)


class StoryboardRenderer:
    """Validate once for repeated playback; keep a private detached storyboard."""

    def __init__(self, episode: dict):
        self._episode = validate_episode(episode)

    def frame_at(self, seconds: float) -> dict:
        return _frame(self._episode, seconds)

    def image_at(self, seconds: float, width: int = 640, height: int = 360) -> Image.Image:
        if type(width) is not int or type(height) is not int or not (
            1 <= width <= 1920 and 1 <= height <= 1080
        ):
            raise ValueError("image dimensions must fit within 1920x1080")
        frame = self.frame_at(seconds)
        scene = frame["scene"]
        image = Image.new("RGBA", (640, 360), scene["background"])
        for shape in scene["shapes"]:
            layer = Image.new("RGBA", image.size)
            draw = ImageDraw.Draw(layer)
            alpha = round(shape["opacity"] * 255)
            fill = (*ImageColor.getrgb(shape["fill"]), alpha)
            stroke = (*ImageColor.getrgb(shape["stroke"]), alpha)
            x, y, w, h = (shape[field] * 2 for field in ("x", "y", "width", "height"))
            box = (x, y, x + w, y + h)
            if shape["kind"] == "text":
                try:
                    font = ImageFont.load_default(size=round(shape["font_size"] * 2))
                except TypeError:  # Pillow 10.0 has only the fixed-size bundled font.
                    font = ImageFont.load_default()
                draw.text((x, y), shape["text"], fill=fill, font=font, anchor="lt")
                # Text uses a bounded top-left box, matching the web contract.
                clipped = Image.new("RGBA", image.size)
                crop_box = tuple(round(v) for v in box)
                clipped.paste(layer.crop(crop_box), crop_box[:2])
                layer = clipped
            else:
                method = draw.rectangle if shape["kind"] == "rect" else draw.ellipse
                method(box, fill=fill, outline=stroke if shape["stroke_width"] else None,
                       width=max(1, round(shape["stroke_width"] * 2)))
            image = Image.alpha_composite(image, layer)
        # Letterbox rather than distort the scene when terminal cells are non-square.
        scale = min(width / image.width, height / image.height)
        fitted = image.convert("RGB").resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
        result = Image.new("RGB", (width, height), scene["background"])
        result.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
        return result

    def terminal_at(self, seconds: float, columns: int, rows: int) -> Text:
        """A Rich half-block image bounded to the supplied terminal geometry."""
        columns, rows = max(1, min(240, columns)), max(1, min(100, rows))
        image = self.image_at(seconds, columns, rows * 2)
        pixels = image.load()
        text = Text(no_wrap=True, overflow="crop")
        for y in range(rows):
            if y:
                text.append("\n")
            # Large flat vector regions need one span, not one style per pixel.
            for (top, bottom), cells in groupby(
                range(columns), key=lambda x: (pixels[x, y * 2], pixels[x, y * 2 + 1])
            ):
                text.append("\u2580" * sum(1 for _ in cells),
                            Style(color=f"rgb{top}", bgcolor=f"rgb{bottom}"))
        return text
