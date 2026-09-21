"""Build a reviewable sprite pack from an authored grid, never invent animation poses.

Background cleanup is opt-in and preserves enclosed white highlights. The output
is a draft asset, not proof of art approval or permission to sell the source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import deque
from pathlib import Path

from PIL import Image


def remove_connected_matte(
    image: Image.Image, *, neutral_floor: int = 96, neutral_spread: int = 35
) -> Image.Image:
    """Remove exterior neutral matte/shadow, retaining enclosed highlights.

    The floor is intentionally above the authored dark clothing and outlines.
    Connectivity, not a global color replacement, protects enclosed white shoes,
    visor glints and jacket highlights. RGB channels are never repainted.
    """
    if not 0 <= neutral_floor <= 255 or not 0 <= neutral_spread <= 255:
        raise ValueError("Invalid matte thresholds")
    image = image.convert("RGBA")
    width, height = image.size
    pixels = image.load()
    visited = bytearray(width * height)
    pending = deque()
    for x in range(width):
        pending.extend(((x, 0), (x, height - 1)))
    for y in range(height):
        pending.extend(((0, y), (width - 1, y)))
    while pending:
        x, y = pending.popleft()
        if not (0 <= x < width and 0 <= y < height):
            continue
        index = y * width + x
        if visited[index]:
            continue
        visited[index] = 1
        r, g, b, a = pixels[x, y]
        if a > 0 and not (
            min(r, g, b) >= neutral_floor
            and max(r, g, b) - min(r, g, b) <= neutral_spread
        ):
            continue
        pixels[x, y] = (r, g, b, 0)
        pending.extend(((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)))
    return image


def remove_tiny_islands(
    image: Image.Image, *, max_area: int = 6, min_gap: int = 3
) -> Image.Image:
    """Remove only tiny, well-separated alpha components at FINAL resolution.

    Eight-neighbor connectivity keeps diagonal outlines and fingers together.
    Nearby detached details are protected. Never retain only the largest island:
    a real accessory or separated limb can be a substantial second component.
    This is opt-in cleanup for reviewed single-character sheets, not arbitrary art.
    """
    result = image.convert("RGBA")
    width, height = result.size
    pixels = result.load()
    remaining = {(x, y) for y in range(height) for x in range(width) if pixels[x, y][3]}
    components = []
    while remaining:
        start = min(remaining, key=lambda p: (p[1], p[0]))
        remaining.remove(start)
        component, pending = {start}, [start]
        while pending:
            x, y = pending.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbor = x + dx, y + dy
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        component.add(neighbor)
                        pending.append(neighbor)
        components.append(component)
    if not components:
        return result
    largest = max(map(len, components))
    threshold = min(max_area, max(1, largest // 150))
    substantial = set().union(*(c for c in components if len(c) > threshold))
    if not substantial:
        return result
    for component in components:
        if len(component) > threshold:
            continue
        near = any(
            (x + dx, y + dy) in substantial
            for x, y in component
            for dx in range(-min_gap, min_gap + 1)
            for dy in range(-min_gap, min_gap + 1)
        )
        if not near:
            for x, y in component:
                r, g, b, _ = pixels[x, y]
                pixels[x, y] = r, g, b, 0
    return result


def preserve_bright_details(cleaned: Image.Image, baseline: Image.Image) -> Image.Image:
    """Restore original enclosed highlights if final-grid aliasing opens a seam.

    Baseline already had its exterior light matte removed using the old flood.
    Its remaining bright pixels are conservative protected detail, not new paint.
    """
    result = cleaned.copy()
    for y in range(result.height):
        for x in range(result.width):
            pixel = baseline.getpixel((x, y))
            if pixel[3] and min(pixel[:3]) >= 200:
                result.putpixel((x, y), pixel)
    return result


def build(
    source: Path,
    output: Path,
    *,
    pack_id: str,
    name: str,
    columns: int,
    rows: int,
    clean_matte: bool = False,
) -> dict:
    if output.exists():
        raise ValueError("Output already exists; use a new version directory")
    if columns < 1 or rows < 1 or columns * rows > 64:
        raise ValueError("Grid must have between 1 and 64 frames")
    if not pack_id.startswith("openvegas.") or not all(
        c.isalnum() or c in ".-" for c in pack_id
    ):
        raise ValueError("Invalid pack ID")
    with Image.open(source) as original:
        if original.width * original.height > 16_000_000:
            raise ValueError("Source pixel budget exceeded")
        original.load()
        source_image = original.convert("RGBA")
    cells = []
    geometry_cells = []
    cleanup_counts = []
    for row in range(rows):
        for col in range(columns):
            bounds = (
                round(col * source_image.width / columns),
                round(row * source_image.height / rows),
                round((col + 1) * source_image.width / columns),
                round((row + 1) * source_image.height / rows),
            )
            cell = source_image.crop(bounds)
            if clean_matte:
                # Freeze v0.1.0 crop/scale/placement BEFORE stronger cleanup.
                # Only alpha changes; poses never shift or get independently fit.
                geometry = remove_connected_matte(cell, neutral_floor=160)
                cleaned = remove_connected_matte(cell)
            else:
                geometry = cleaned = cell
            box = geometry.getchannel("A").getbbox()
            if not box:
                raise ValueError("Empty authored frame")
            cells.append(cleaned.crop(box))
            geometry_cells.append(geometry.crop(box))
    frame_w, frame_h = 64, 80
    scale = min(
        (frame_w - 8) / max(cell.width for cell in cells),
        (frame_h - 8) / max(cell.height for cell in cells),
    )
    frames = []
    for cell, geometry in zip(cells, geometry_cells):
        resized = cell.resize(
            (max(1, round(cell.width * scale)), max(1, round(cell.height * scale))),
            Image.Resampling.NEAREST,
        )
        frame = Image.new("RGBA", (frame_w, frame_h))
        frame.alpha_composite(
            resized, ((frame_w - resized.width) // 2, frame_h - 3 - resized.height)
        )
        baseline = Image.new("RGBA", (frame_w, frame_h))
        baseline.alpha_composite(
            geometry.resize(resized.size, Image.Resampling.NEAREST),
            ((frame_w - resized.width) // 2, frame_h - 3 - resized.height),
        )
        baseline_count = sum(a > 0 for a in baseline.getchannel("A").tobytes())
        if clean_matte:
            frame = remove_connected_matte(frame)
            frame = preserve_bright_details(frame, baseline)
            before_islands = sum(a > 0 for a in frame.getchannel("A").tobytes())
            frame = remove_tiny_islands(frame)
        else:
            before_islands = baseline_count
        final_count = sum(a > 0 for a in frame.getchannel("A").tobytes())
        cleanup_counts.append(
            {
                "baseline_visible_pixels": baseline_count,
                "matte_pixels_removed": baseline_count - before_islands,
                "island_pixels_removed": before_islands - final_count,
            }
        )
        frames.append(frame)
    output.mkdir(parents=True)
    sheet = Image.new("RGBA", (frame_w * len(frames), frame_h))
    for index, frame in enumerate(frames):
        sheet.alpha_composite(frame, (index * frame_w, 0))
    sheet.save(output / "sheet.png")
    checksum = hashlib.sha256((output / "sheet.png").read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "pack_id": pack_id,
        "version": "0.1.1",
        "display_name": name,
        "license_id": "openvegas-original-concept-review",
        "sheet": "sheet.png",
        "sha256": checksum,
        "frame": {
            "width": frame_w,
            "height": frame_h,
            "anchor": [frame_w // 2, frame_h - 3],
        },
        "animations": {
            "idle": {"frames": [0], "frame_ms": 1000, "loop": True},
            "waiting": {
                "frames": list(range(len(frames))),
                "frame_ms": 140,
                "loop": True,
            },
            "complete": {
                "frames": list(range(len(frames))) * 4,
                "frame_ms": 150,
                "loop": False,
            },
        },
        "reduced_motion_frame": 0,
        "tags": ["companion", "dance", "original"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    review = {
        "approval": "concept-approved; terminal-animation-review-pending",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "grid": [columns, rows],
        "version": "0.1.1",
        "cleanup": "exterior neutral matte/shadow plus distant tiny alpha islands"
        if clean_matte
        else "none",
        "cleanup_parameters": {
            "neutral_floor": 96,
            "neutral_spread": 35,
            "island_max_area": 6,
            "island_min_gap": 3,
            "protected_legacy_highlights_min_channel": 200,
            "island_area_ratio": "at most 1/150 of largest component",
        },
        "geometry": "v0.1.0 crop, shared proportional scale, centering and baseline preserved",
        "frame_cleanup": cleanup_counts,
        "frame_count": len(frames),
        "unique_frames": len({frame.tobytes() for frame in frames}),
        "purchasable": False,
    }
    (output / "provenance.json").write_text(json.dumps(review, indent=2) + "\n")
    for theme, background in (("light", "#f6f7fb"), ("dark", "#11151c")):
        previews = []
        for frame in frames:
            canvas = Image.new("RGB", (frame_w, frame_h), background)
            canvas.paste(frame, mask=frame.getchannel("A"))
            previews.append(
                canvas.resize((frame_w * 4, frame_h * 4), Image.Resampling.NEAREST)
            )
        previews[0].save(
            output / f"dance-{theme}.gif",
            save_all=True,
            append_images=previews[1:],
            duration=140,
            loop=0,
            disposal=2,
        )
        board = Image.new("RGB", (frame_w * columns, frame_h * rows), background)
        for index, frame in enumerate(frames):
            board.paste(
                frame,
                ((index % columns) * frame_w, (index // columns) * frame_h),
                frame,
            )
        board.resize(
            (board.width * 3, board.height * 3), Image.Resampling.NEAREST
        ).save(output / f"contact-{theme}.png")
    return review


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pack-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--rows", type=int, default=2)
    parser.add_argument("--clean-neutral-matte", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            build(
                args.source,
                args.output,
                pack_id=args.pack_id,
                name=args.name,
                columns=args.columns,
                rows=args.rows,
                clean_matte=args.clean_neutral_matte,
            )
        )
    )


if __name__ == "__main__":
    main()
