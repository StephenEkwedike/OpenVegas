"""Rebuild the reviewed sports corrections; never invent animation poses.

Source-specific cleanup is guarded by the raw artwork hash. Future artwork must
be reviewed before adding different enclosed-matte seed coordinates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from collections import deque
from pathlib import Path

from PIL import Image

if __package__:
    from .build_completion_pack import build
    from .build_emote_pack import remove_connected_matte
    from .deterministic_png import ENCODING, write_rgba_png
else:
    from build_completion_pack import build
    from build_emote_pack import remove_connected_matte
    from deterministic_png import ENCODING, write_rgba_png


REVIEWED_HASHES = {
    "bicycle-finish": "023c0fe0b44b9003df54fda8c7ba0e0724df3b25da886f0f77ba16f2d479dc44",
    "three-point-glow": "b8255945370c8118dd942c6dedf8efff8ffa60a2b2621885d1c2a4db172d4204",
}
BICYCLE_MATTE_SEEDS = {
    0: [(130, 310)],
    1: [(140, 315)],
    7: [(245, 320)],
    8: [(148, 320)],
    9: [(173, 303)],
    11: [(144, 318)],
}


def prepare(source: Path, slug: str) -> Image.Image:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != REVIEWED_HASHES.get(slug):
        raise ValueError("Artwork changed: review cleanup geometry before rebuilding")
    with Image.open(source) as original:
        image = original.convert("RGBA")
    if image.size != (1448, 1086):
        raise ValueError("Expected the reviewed 4x3, 362-pixel-cell source")
    if slug == "three-point-glow":
        # The court is artwork, not a matte. Keep its white arc and add a
        # transparent two-pixel margin around each intact authored tile.
        out = Image.new("RGBA", (1464, 1098))
        for row in range(3):
            for col in range(4):
                tile = image.crop(
                    (col * 362, row * 362, (col + 1) * 362, (row + 1) * 362)
                )
                out.paste(tile, (col * 366 + 2, row * 366 + 2))
        return out
    out = Image.new("RGBA", image.size)
    for row in range(3):
        for col in range(4):
            tile = image.crop((col * 362, row * 362, (col + 1) * 362, (row + 1) * 362))
            tile = remove_connected_matte(tile, neutral_floor=96, neutral_spread=24)
            pixels = tile.load()
            pending = deque(BICYCLE_MATTE_SEEDS.get(row * 4 + col, []))
            seen = set()
            while pending:
                x, y = pending.popleft()
                if (x, y) in seen or not (0 <= x < 362 and 0 <= y < 362):
                    continue
                seen.add((x, y))
                r, g, b, a = pixels[x, y]
                if not a or min(r, g, b) < 96 or max(r, g, b) - min(r, g, b) > 24:
                    continue
                pixels[x, y] = r, g, b, 0
                pending.extend(((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)))
            alpha = tile.getchannel("A")
            alpha.paste(0, (0, 0, 362, 5))  # Preceding cell's grass sliver.
            tile.putalpha(alpha)
            out.paste(tile, (col * 362, row * 362))
    return out


def rebuild(source_dir: Path, output: Path, slug: str) -> dict:
    raw = source_dir / "source.png"
    prompts = json.loads((source_dir / "prompt-set.json").read_text())
    with tempfile.TemporaryDirectory(prefix="ov-sports-preparation-") as temp:
        prepared = Path(temp) / "prepared.png"
        write_rgba_png(prepare(raw, slug), prepared)
        provenance = build(
            prepared,
            output,
            pack_id="openvegas." + slug,
            name=slug.replace("-", " ").title(),
            prompt=prompts[-1],
        )
        provenance["prepared_source_sha256"] = provenance["source_sha256"]
        provenance["source_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
        provenance["prompt_set"] = prompts
        provenance["revision"] = "0.1.2"
        provenance["prepared_source_encoding"] = ENCODING
        provenance["highlight_reference"] = (
            "Not used; this revision retains authored white details directly."
        )
        provenance["normalization"] = (
            "Reviewed edge flood plus source-specific enclosed matte seeds; no RGB repainting; "
            "five-pixel top grid-sliver removal; fixed 4x3 crop; proportional nearest-neighbor scale."
            if slug == "bicycle-finish"
            else "Authored overhead court retained, including white arc; two-pixel transparent padding "
            "per tile; fixed 4x3 crop; proportional nearest-neighbor scale; no matte removal."
        )
        provenance["revision_requirements"] = (
            [
                "Goal remains right; player faces away at setup.",
                "Airborne head/shoulders remain goal-side of hips; no facing reversal.",
                "Overhead strike, back-first landing and original celebration.",
            ]
            if slug == "bicycle-finish"
            else [
                "Two adult male human players.",
                "Fixed overhead court view with shooter outside the three-point arc before release.",
                "Defender separated from shooter by multiple body widths.",
            ]
        )
        provenance["cleanup_recipe"] = "scripts/rebuild_sports_revision.py"
        (output / "source.png").rename(output / "prepared-source.png")
        shutil.copyfile(raw, output / "source.png")
        shutil.copyfile(source_dir / "prompt-set.json", output / "prompt-set.json")
    manifest = json.loads((output / "manifest.json").read_text())
    manifest["version"] = "0.1.2"
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", choices=sorted(REVIEWED_HASHES))
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rebuild(args.source_dir, args.output, args.slug)
