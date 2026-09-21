"""Normalize an authored 4x3 sports sheet without synthesizing poses or motion."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from PIL import Image

if __package__:
    from .build_emote_pack import remove_connected_matte
else:
    from build_emote_pack import remove_connected_matte


TIMELINE = [
    i
    for i, hold in enumerate((4, 3, 2, 2, 3, 2, 2, 2, 2, 3, 5, 15))
    for _ in range(hold)
]


def clean_grid_residue(frame: Image.Image) -> Image.Image:
    """Remove reviewed grid slivers and large two-tone neutral matte pockets.

    The basket/goal region and small enclosed white kit details are protected.
    This is sports-sheet authoring cleanup, never an automatic runtime filter.
    """
    from collections import deque

    frame = frame.copy()
    pixels = frame.load()
    for neutral in (False, True):
        remaining = set()
        for y in range(frame.height):
            for x in range(frame.width):
                r, g, b, a = pixels[x, y]
                if a and (
                    not neutral
                    or (
                        x < frame.width * 0.72
                        and min(r, g, b) >= 96
                        and max(r, g, b) - min(r, g, b) <= 24
                    )
                ):
                    remaining.add((x, y))
        while remaining:
            start = remaining.pop()
            pending, component = deque([start]), [start]
            while pending:
                x, y = pending.popleft()
                for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        pending.append(neighbor)
                        component.append(neighbor)
            xs, ys = zip(*component)
            width, height = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
            levels = [pixels[p][0] for p in component]
            remove = (
                (len(component) >= 10 and min(width, height) <= 1)
                if not neutral
                else (
                    width >= 6
                    and height >= 6
                    and len(component) >= 24
                    and max(levels) - min(levels) >= 40
                )
            )
            if remove:
                for x, y in component:
                    r, g, b, _ = pixels[x, y]
                    pixels[x, y] = r, g, b, 0
    return frame


def build(
    source: Path,
    output: Path,
    *,
    pack_id: str,
    name: str,
    prompt: str = "",
    clean_matte: bool = False,
    detail_reference: Path | None = None,
) -> dict:
    if output.exists():
        raise ValueError("Use a new output directory; never overwrite an approved pack")
    if not pack_id.startswith("openvegas.") or not all(
        c.isalnum() or c in ".-" for c in pack_id
    ):
        raise ValueError("Invalid pack identity")
    if not name.isascii() or not name.isprintable() or len(name) > 128:
        raise ValueError("Invalid display name")
    with Image.open(source) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError("Source exceeds pixel budget")
        image.load()
        image = image.convert("RGBA")
    if image.getchannel("A").getextrema()[0] != 0 and not clean_matte:
        raise ValueError(
            "Author a transparent source; this tool does not guess background colors"
        )
    reference = None
    if detail_reference is not None:
        with Image.open(detail_reference) as original:
            if original.size != image.size:
                raise ValueError(
                    "Highlight reference must match the authored grid exactly"
                )
            reference = original.convert("RGBA")
    width, height = 160, 120
    cell_width, cell_height = image.width / 4, image.height / 3
    scale = min((width - 8) / cell_width, (height - 8) / cell_height)
    frames = []
    for row in range(3):
        for column in range(4):
            bounds = (
                round(column * cell_width),
                round(row * cell_height),
                round((column + 1) * cell_width),
                round((row + 1) * cell_height),
            )
            cell = image.crop(bounds)
            highlights = Image.new("RGBA", cell.size)
            if reference is not None:
                previous = reference.crop(bounds)
                for y in range(cell.height):
                    for x in range(cell.width):
                        a, b = previous.getpixel((x, y)), cell.getpixel((x, y))
                        # The unchanged lead athlete and hoop have an exact alpha
                        # reference. Never restore the former robot's white parts.
                        if (
                            (x < cell.width * 0.6 or y < cell.height * 0.46)
                            and a[3] >= 160
                            and min(a[:3]) >= 205
                            and min(b[:3]) >= 205
                        ):
                            highlights.putpixel((x, y), (*b[:3], 255))
            if clean_matte:
                cell = remove_connected_matte(cell, neutral_floor=96, neutral_spread=24)
                # Thin fragments from the preceding grid cell are not scene art.
                alpha = cell.getchannel("A")
                alpha.paste(0, (0, 0, max(2, round(cell.width * 0.015)), cell.height))
                alpha.paste(0, (0, 0, cell.width, max(2, round(cell.height * 0.015))))
                cell.putalpha(alpha)
            # Drop the generated semitransparent fringe, preserving retained RGB.
            cell.putalpha(
                cell.getchannel("A").point(lambda alpha: 255 if alpha >= 160 else 0)
            )
            size = (round(cell.width * scale), round(cell.height * scale))
            cell = cell.resize(size, Image.Resampling.NEAREST)
            frame = Image.new("RGBA", (width, height))
            frame.alpha_composite(cell, ((width - size[0]) // 2, height - 3 - size[1]))
            if clean_matte:
                frame = clean_grid_residue(frame)
            frame.alpha_composite(
                highlights.resize(size, Image.Resampling.NEAREST),
                ((width - size[0]) // 2, height - 3 - size[1]),
            )
            if frame.getchannel("A").getbbox() is None:
                raise ValueError("Empty authored cell")
            frames.append(frame)
    if len({frame.tobytes() for frame in frames}) < 12:
        raise ValueError("All twelve authored action frames must be distinct")
    output.mkdir(parents=True)
    sheet = Image.new("RGBA", (width * 12, height))
    for index, frame in enumerate(frames):
        sheet.alpha_composite(frame, (width * index, 0))
    sheet.save(output / "sheet.png")
    manifest = {
        "schema_version": 1,
        "pack_id": pack_id,
        "version": "0.1.0",
        "display_name": name,
        "license_id": "openvegas-original-sports-review",
        "sheet": "sheet.png",
        "sha256": hashlib.sha256((output / "sheet.png").read_bytes()).hexdigest(),
        "frame": {"width": width, "height": height, "anchor": [width // 2, height - 3]},
        "animations": {
            "idle": {"frames": [0], "frame_ms": 1000, "loop": True},
            "waiting": {"frames": [0], "frame_ms": 1000, "loop": True},
            "complete": {"frames": TIMELINE, "frame_ms": 120, "loop": False},
        },
        "reduced_motion_frame": 11,
        "tags": ["completion", "sports", "original"],
    }
    provenance = {
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "generation": "built-in image_gen",
        "prompt": prompt,
        "approval": "original-sports-preview; native-animation-and-sale-review-pending",
        "purchasable": False,
        "authored_frames": 12,
        "duration_seconds": len(TIMELINE) * 0.12,
        "normalization": "fixed 4x3 cell crop; one proportional scale; shared baseline; alpha threshold 160; retained RGB unchanged",
        "connected_neutral_matte_cleanup": clean_matte,
        "highlight_reference_sha256": hashlib.sha256(
            detail_reference.read_bytes()
        ).hexdigest()
        if detail_reference
        else None,
        "source_format": "editable raster PNG, not a layered Aseprite master",
        "rights": "Original characters and kits. No athlete, team or league endorsement claimed.",
    }
    for filename, value in (
        ("manifest.json", manifest),
        ("provenance.json", provenance),
    ):
        (output / filename).write_text(json.dumps(value, indent=2) + "\n")
    shutil.copyfile(source, output / "source.png")
    for theme, background in (("light", "#f6f7fb"), ("dark", "#11151c")):
        previews = []
        contact = Image.new("RGB", (width * 4, height * 3), background)
        for index, frame in enumerate(frames):
            cell = Image.new("RGB", frame.size, background)
            cell.paste(frame, mask=frame.getchannel("A"))
            contact.paste(cell, ((index % 4) * width, (index // 4) * height))
            previews.append(
                cell.resize((width * 4, height * 4), Image.Resampling.NEAREST)
            )
        contact.resize(
            (contact.width * 2, contact.height * 2), Image.Resampling.NEAREST
        ).save(output / f"contact-{theme}.png")
        # Export the once-only clip, with final-pose hold rather than a false loop.
        previews[0].save(
            output / f"complete-{theme}.gif",
            save_all=True,
            append_images=[previews[i] for i in TIMELINE[1:]],
            duration=120,
            disposal=2,
        )
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pack-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--clean-neutral-matte", action="store_true")
    parser.add_argument("--detail-reference", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            build(
                args.source,
                args.output,
                pack_id=args.pack_id,
                name=args.name,
                prompt=args.prompt_file.read_text() if args.prompt_file else "",
                clean_matte=args.clean_neutral_matte,
                detail_reference=args.detail_reference,
            )
        )
    )
