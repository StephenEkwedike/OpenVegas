"""Derived previews must show the exact current authored pack, not an older export."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageSequence

from openvegas.emotes.resources import PackRepository

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", ["pixel-courier", "beat-maker", "visor-explorer"])
@pytest.mark.parametrize("theme,background", [("light", "#f6f7fb"), ("dark", "#11151c")])
@pytest.mark.parametrize("surface", ["openvegas/emotes/assets", "ui/assets/emotes"])
def test_companion_contact_and_gif_match_current_sheet(name, theme, background, surface):
    pack = PackRepository().load(name)
    clip = pack.manifest.animations["waiting"]
    frames = [pack.frame(index) for index in clip.frames]
    width, height = frames[0].size
    board = Image.new("RGB", (width * 4, height * 2), background)
    previews = []
    for index, frame in enumerate(frames):
        board.paste(frame, ((index % 4) * width, (index // 4) * height), frame)
        canvas = Image.new("RGB", frame.size, background)
        canvas.paste(frame, mask=frame.getchannel("A"))
        previews.append(canvas.resize((width * 4, height * 4), Image.Resampling.NEAREST))
    directory = ROOT / surface / name
    with Image.open(directory / f"contact-{theme}.png") as contact:
        expected = board.resize((board.width * 3, board.height * 3), Image.Resampling.NEAREST)
        assert contact.size == expected.size
        assert contact.convert("RGB").tobytes() == expected.tobytes()

    # GIF quantization is lossy; compare decoded pixels against an export of the
    # current sheet using the same authoring recipe, not against unquantized RGB.
    stream = io.BytesIO()
    previews[0].save(stream, format="GIF", save_all=True, append_images=previews[1:],
                     duration=clip.frame_ms, loop=0, disposal=2)
    stream.seek(0)
    with Image.open(stream) as expected, Image.open(directory / f"dance-{theme}.gif") as actual:
        assert actual.size == expected.size
        assert actual.n_frames == expected.n_frames == len(frames)
        assert actual.info["loop"] == 0
        for got, want in zip(ImageSequence.Iterator(actual), ImageSequence.Iterator(expected)):
            assert got.info["duration"] == want.info["duration"] == clip.frame_ms
            assert got.convert("RGB").tobytes() == want.convert("RGB").tobytes()
