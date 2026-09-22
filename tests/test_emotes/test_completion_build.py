from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from openvegas.emotes.manifest import load_pack
from scripts.build_completion_pack import TIMELINE, build
from scripts.deterministic_png import ENCODING, encode_rgba_png


def authored_source(path: Path):
    source = Image.new("RGBA", (160, 120))
    draw = ImageDraw.Draw(source)
    for index in range(12):
        x, y = (index % 4) * 40, (index // 4) * 40
        draw.rectangle(
            (x + 10, y + 8, x + 28, y + 36), fill=(20 + index * 18, 80, 100, 255)
        )
    source.save(path)


def test_completion_builder_produces_importable_once_only_pack(tmp_path):
    source = tmp_path / "authored.png"
    authored_source(source)
    output = tmp_path / "pack"
    result = build(source, output, pack_id="openvegas.fixture", name="Fixture")
    pack = load_pack(output)
    assert len(TIMELINE) == 45
    assert result["purchasable"] is False
    assert pack.manifest.animations["complete"].duration == pytest.approx(5.4)
    assert len({pack.frame(i).tobytes() for i in range(12)}) == 12
    with pytest.raises(ValueError, match="never overwrite"):
        build(source, output, pack_id="openvegas.fixture", name="Fixture")


def test_completion_builder_rejects_mismatched_reference(tmp_path):
    source, reference = tmp_path / "authored.png", tmp_path / "reference.png"
    authored_source(source)
    Image.new("RGBA", (20, 20)).save(reference)
    with pytest.raises(ValueError, match="match"):
        build(
            source,
            tmp_path / "pack",
            pack_id="openvegas.fixture",
            name="Fixture",
            detail_reference=reference,
        )


@pytest.mark.parametrize("theme,background", [("light", "#f6f7fb"), ("dark", "#11151c")])
def test_contact_preview_uses_canonical_rgba_without_pixel_changes(tmp_path, theme, background):
    source = tmp_path / "authored.png"
    authored_source(source)
    output = tmp_path / "pack"
    provenance = build(source, output, pack_id="openvegas.fixture", name="Fixture")
    pack = load_pack(output)
    width, height = pack.manifest.width, pack.manifest.height
    expected = Image.new("RGB", (width * 4, height * 3), background)
    for index in range(12):
        frame = pack.frame(index)
        cell = Image.new("RGB", frame.size, background)
        cell.paste(frame, mask=frame.getchannel("A"))
        expected.paste(cell, ((index % 4) * width, (index // 4) * height))
    expected = expected.resize(
        (expected.width * 2, expected.height * 2), Image.Resampling.NEAREST
    ).convert("RGBA")
    path = output / f"contact-{theme}.png"
    assert provenance["contact_preview_encoding"] == ENCODING
    assert path.read_bytes() == encode_rgba_png(expected)
    with Image.open(path) as actual:
        assert actual.mode == "RGBA"
        assert actual.size == expected.size
        assert actual.tobytes() == expected.tobytes()


def test_completion_builder_does_not_invent_motion_from_one_pose(tmp_path):
    source = tmp_path / "repeated.png"
    image = Image.new("RGBA", (160, 120))
    draw = ImageDraw.Draw(image)
    for row in range(3):
        for column in range(4):
            x, y = column * 40, row * 40
            draw.rectangle((x + 10, y + 8, x + 28, y + 36), fill="blue")
    image.save(source)
    with pytest.raises(ValueError, match="distinct"):
        build(source, tmp_path / "pack", pack_id="openvegas.fixture", name="Fixture")


def test_authored_json_is_utf8_lf_on_every_platform(tmp_path):
    source = tmp_path / "authored.png"
    authored_source(source)
    output = tmp_path / "pack"
    build(source, output, pack_id="openvegas.fixture", name="Fixture", prompt="Caf\u00e9\nNext")
    for filename in ("manifest.json", "provenance.json"):
        data = (output / filename).read_bytes()
        assert data.endswith(b"\n")
        assert b"\r" not in data
        assert data.decode("utf-8")
