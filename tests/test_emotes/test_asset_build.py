import importlib.util
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from openvegas.emotes.manifest import load_pack
from openvegas.emotes.resources import PackRepository

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("asset_builder", ROOT / "scripts/build_emote_pack.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_matte_removal_preserves_enclosed_white_highlight():
    source = Image.new("RGBA", (10, 10), "white")
    draw = ImageDraw.Draw(source)
    draw.rectangle((2, 2, 7, 7), fill="black")
    draw.point((4, 4), fill="white")
    cleaned = builder.remove_connected_matte(source)
    assert cleaned.getpixel((0, 0))[3] == 0
    assert cleaned.getpixel((2, 2))[3] == 255
    assert cleaned.getpixel((4, 4)) == (255, 255, 255, 255)
    assert source.getpixel((0, 0))[3] == 255


def test_designer_grid_imports_without_runtime_changes(tmp_path):
    source = Image.new("RGBA", (160, 20))
    draw = ImageDraw.Draw(source)
    for i in range(8):
        draw.rectangle((i * 20 + 4, 3, i * 20 + 12, 18), fill=(20 + i * 20, 120, 200, 255))
    file = tmp_path / "authored.png"
    source.save(file)
    result = builder.build(file, tmp_path / "pack", pack_id="openvegas.fixture", name="Fixture", columns=8, rows=1)
    pack = load_pack(tmp_path / "pack")
    assert result["unique_frames"] == 8
    assert pack.manifest.animations["complete"].duration == 4.8
    assert pack.manifest.width == 64
    assert pack.manifest.height == 80
    assert result["purchasable"] is False
    with pytest.raises(ValueError, match="exists"):
        builder.build(file, tmp_path / "pack", pack_id="openvegas.fixture", name="Fixture", columns=8, rows=1)


@pytest.mark.parametrize("name", ["pixel-courier", "beat-maker", "visor-explorer"])
def test_authored_packs_are_transparent_unique_and_bounded(name):
    pack = PackRepository().load(name)
    frames = [pack.frame(index) for index in pack.manifest.animations["waiting"].frames]
    assert len(frames) == len({frame.tobytes() for frame in frames}) == 8
    for frame in frames:
        assert frame.getpixel((0, 0))[3] == 0
        assert frame.getchannel("A").getbbox() is not None
    review = json.loads((ROOT / f"openvegas/emotes/assets/{name}/provenance.json").read_text())
    assert review["purchasable"] is False
