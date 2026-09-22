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
    "skyline-dunk": "172daebf30ecb07831e74552fe6ffba6c3e1c7e9aaad754ff0b60ab8e120789b",
    "bicycle-finish": "023c0fe0b44b9003df54fda8c7ba0e0724df3b25da886f0f77ba16f2d479dc44",
    "three-point-glow": "b8255945370c8118dd942c6dedf8efff8ffa60a2b2621885d1c2a4db172d4204",
}
SKYLINE_REFERENCE_SHA256 = "98b21bfb40778893d830b1b41f1d4be04623a8ec95f0011970543936faeb4e04"
SKYLINE_RGBA_SHA256 = "c4edd8ac0fc4bcaca38511a163aeb67a1bbee150c65a60ae98cd1712c415a30a"
SKYLINE_PROMPT = (
    "Final condensed authoring brief: Blue-and-amber human dunker versus a generic "
    "green-uniform human defender. Dribble, gather, leap, dunk, land and celebrate. "
    "Twelve original chronological pixel-art poses, 4x3 equal grid, stable side "
    "camera, no real athlete/team/brand marks. User correction: all opponents are "
    "generic HUMAN players; no robots. Full edit requests are recorded in task history."
)
BICYCLE_MATTE_SEEDS = {
    0: [(130, 310)],
    1: [(140, 315)],
    7: [(245, 320)],
    8: [(148, 320)],
    9: [(173, 303)],
    11: [(144, 318)],
}


def prepare(source: Path, slug: str) -> Image.Image:
    if slug == "skyline-dunk":
        raise ValueError("Skyline uses the reviewed highlight-reference rebuild, not preparation")
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


def rebuild_skyline(source_dir: Path, output: Path) -> dict:
    """Encoding-only successor to the exact owner-approved v0.1.0 artwork."""
    raw, reference = source_dir / "source.png", source_dir / "detail-reference.png"
    for path, expected in (
        (raw, REVIEWED_HASHES["skyline-dunk"]),
        (reference, SKYLINE_REFERENCE_SHA256),
    ):
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Artwork changed: review Skyline source and highlight reference")
    provenance = build(
        raw, output, pack_id="openvegas.skyline-dunk", name="Skyline Dunk",
        prompt=SKYLINE_PROMPT, clean_matte=True, detail_reference=reference,
    )
    with Image.open(output / "sheet.png") as sheet:
        rgba = hashlib.sha256(sheet.convert("RGBA").tobytes()).hexdigest()
    if rgba != SKYLINE_RGBA_SHA256:
        raise ValueError("Skyline RGBA changed: encoding-only revision must preserve every pixel")
    provenance["highlight_reference"] = (
        "Creative source detail-reference.png: transparent white highlights only; no opponent figures."
    )
    provenance["revision"] = "0.1.1"
    provenance["cleanup_recipe"] = "scripts/rebuild_sports_revision.py"
    provenance["encoding_lineage"] = {
        "change": "PNG encoding and metadata only; authored RGBA and animation timelines unchanged",
        "previous_version": "0.1.0",
        "previous_sheet_sha256": "b25b5a67cb2b1700ada1539934c9e6b2b54dd5e0c9fbb52feac16c156fb0551a",
        "previous_manifest_sha256": "6071a5053d98faa8d5eed9bec176a21fc77e108afd950087e830d975fa21b1d0",
        "previous_provenance_sha256": "3f5056368d3dee059fe047af295f78d4c2b708f79e31b92d12310ebb33368ea9",
        "previous_marketing_provenance_sha256": "4120fec1f3964f9132304ce1a01ea1e6ee8611d1238934aa3473bd6a1e84eeb0",
        "rgba_sha256": rgba,
        "approval_record": "evidence/emotes/phase7-owner-decisions.json",
        "approval_scope": "Prior version artwork only; new encoded bytes are not directly named in that record",
        "native_compatibility_approved": False,
        "sales_activation_authorized": False,
    }
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = provenance["revision"]
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8", newline="\n")
    shutil.copyfile(reference, output / "detail-reference.png")
    return provenance


def rebuild(source_dir: Path, output: Path, slug: str) -> dict:
    if slug == "skyline-dunk":
        return rebuild_skyline(source_dir, output)
    raw = source_dir / "source.png"
    prompts = json.loads((source_dir / "prompt-set.json").read_text(encoding="utf-8"))
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
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = "0.1.2"
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8", newline="\n")
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", choices=sorted(REVIEWED_HASHES))
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rebuild(args.source_dir, args.output, args.slug)
