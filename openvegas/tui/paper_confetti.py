"""Seekable, antialiased paper animation for the owned terminal game canvas."""
from __future__ import annotations

import math
import random
from functools import lru_cache

from PIL import Image, ImageDraw

from .confetti import FALLING_CONFETTI_SECONDS

COLORS = ((244, 106, 104), (246, 204, 99), (85, 206, 174),
          (107, 174, 237), (218, 132, 195), (249, 236, 205))


@lru_cache(maxsize=16)
def pieces(seed):
    rng = random.Random(seed)
    return tuple((rng.random(), rng.uniform(0, .65), rng.uniform(.7, 1.05),
                  rng.uniform(-1, 1), rng.uniform(0, math.tau), rng.uniform(5, 11))
                 for _ in range(72))


def paper_frame(width, height, elapsed, seed=''):
    """Bounded pixels, continuous flutter and perspective; no network or game state."""
    width, height = max(1, min(768, int(width))), max(1, min(512, int(height)))
    scale = 2
    image = Image.new('RGB', (width * scale, height * scale), '#153a35')
    draw = ImageDraw.Draw(image)
    if math.isfinite(elapsed) and 0 <= elapsed < FALLING_CONFETTI_SECONDS:
        for i, (x, delay, speed, drift, phase, size) in enumerate(pieces(str(seed))):
            age = elapsed - delay
            if age < 0:
                continue
            travel = age / (FALLING_CONFETTI_SECONDS - .7)
            cy = -18 + (height + 50) * travel * speed
            cx = x * width + drift * age * 12 + math.sin(phase + age * 3) * 13
            angle = phase + age * (2.4 + speed)
            fold = math.cos(phase + age * 5)
            half_w, half_h = size * .55, size * max(.12, abs(fold)) * .35
            polygon = []
            for px, py in ((-half_w,-half_h),(half_w,-half_h),(half_w,half_h),(-half_w,half_h)):
                polygon.append(((cx + px * math.cos(angle) - py * math.sin(angle)) * scale,
                                (cy + px * math.sin(angle) + py * math.cos(angle)) * scale))
            shade = .68 + .32 * abs(fold)
            color = tuple(round(c * shade) for c in COLORS[i % len(COLORS)])
            draw.polygon([(px + 2, py + 3) for px, py in polygon], fill='#10302b')
            draw.polygon(polygon, fill=color)
            draw.line(polygon[:2], fill=tuple(min(255, c + 25) for c in color), width=1)
    return image.resize((width, height), Image.Resampling.LANCZOS)
