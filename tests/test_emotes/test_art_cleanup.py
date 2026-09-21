import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "builder", ROOT / "scripts/build_emote_pack.py"
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_flood_preserves_enclosed_white_and_colored_pixels():
    original = Image.new("RGBA", (20, 20), (125, 127, 128, 255))
    draw = ImageDraw.Draw(original)
    draw.rectangle((5, 4, 14, 15), fill=(8, 10, 18, 255))
    draw.rectangle((6, 6, 13, 10), fill="white")
    draw.rectangle((7, 11, 12, 13), fill=(16, 190, 213, 255))
    cleaned = builder.remove_connected_matte(original)
    assert cleaned.getpixel((0, 0))[3] == 0
    assert cleaned.getpixel((8, 8)) == (255, 255, 255, 255)
    assert cleaned.getpixel((8, 12)) == original.getpixel((8, 12))
    assert original.getpixel((0, 0))[3] == 255


def test_tiny_island_removed_nearby_detail_and_accessory_retained():
    image = Image.new("RGBA", (64, 80))
    draw = ImageDraw.Draw(image)
    draw.rectangle((15, 10, 45, 70), fill=(12, 30, 80, 255))
    draw.rectangle((53, 30, 57, 33), fill="red")
    draw.point((13, 13), fill="white")
    draw.rectangle((2, 30, 2, 33), fill="black")
    cleaned = builder.remove_tiny_islands(image)
    assert cleaned.getpixel((2, 30))[3] == 0
    assert cleaned.getpixel((13, 13))[3] == 255
    assert cleaned.getpixel((55, 32))[3] == 255
    assert image.getpixel((2, 30))[3] == 255


def test_diagonal_details_stay_connected():
    image = Image.new("RGBA", (40, 40))
    draw = ImageDraw.Draw(image)
    draw.rectangle((15, 15, 35, 35), fill="black")
    for i in range(8, 15):
        draw.point((i, i), fill="black")
    assert builder.remove_tiny_islands(image).tobytes() == image.tobytes()


def test_bright_legacy_detail_restored_without_new_rgb():
    old = Image.new("RGBA", (4, 4))
    old.putpixel((1, 1), (251, 248, 240, 255))
    old.putpixel((2, 1), (140, 140, 140, 255))
    clean = Image.new("RGBA", (4, 4))
    restored = builder.preserve_bright_details(clean, old)
    assert restored.getpixel((1, 1)) == old.getpixel((1, 1))
    assert restored.getpixel((2, 1))[3] == 0


@pytest.mark.parametrize("name", ["pixel-courier", "beat-maker", "visor-explorer"])
def test_pack_geometry_pose_pixels_highlights_and_checksum(name):
    directory = ROOT / "openvegas/emotes/assets" / name
    raw = json.loads((directory / "manifest.json").read_text())
    source = directory / "sheet.png"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == raw["sha256"]
    assert raw["version"] == "0.1.1"
    assert raw["frame"] == {"width": 64, "height": 80, "anchor": [32, 77]}
    assert len(raw["animations"]["complete"]["frames"]) * 150 == 4800
    image = Image.open(source).convert("RGBA")
    assert image.size == (512, 80)
    frames = [image.crop((i * 64, 0, (i + 1) * 64, 80)).tobytes() for i in range(8)]
    assert len(set(frames)) == 8
    baseline = Image.open(Path(__file__).parent / "fixtures/v0.1.0" / f"{name}.png").convert("RGBA")
    removed = 0
    for old, new in zip(baseline.getdata(), image.getdata()):
        if new[3]:
            assert (
                new == old
            )  # No new color, drawing, translation, scale or alpha invention.
        if old[3] and min(old[:3]) >= 200:
            assert new == old  # Every surviving legacy highlight is protected.
        removed += bool(old[3] and not new[3])
    assert removed > 0
    provenance = json.loads((directory / "provenance.json").read_text())
    assert provenance["purchasable"] is False
    assert (
        provenance["approval"] == "concept-approved; terminal-animation-review-pending"
    )


def test_reported_visor_island_removed():
    review = json.loads((ROOT / "openvegas/emotes/assets/visor-explorer/provenance.json").read_text())
    assert review["frame_cleanup"][5]["island_pixels_removed"] == 5


def test_output_overwrite_refused(tmp_path):
    with pytest.raises(ValueError, match="exists"):
        builder.build(
            tmp_path / "missing.png",
            tmp_path,
            pack_id="openvegas.test",
            name="Test",
            columns=4,
            rows=2,
        )
