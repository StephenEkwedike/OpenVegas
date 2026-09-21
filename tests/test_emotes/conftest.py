from __future__ import annotations

import hashlib
import json

import pytest
from PIL import Image

from openvegas.emotes import load_pack


@pytest.fixture
def pack_dir(tmp_path):
    root = tmp_path / "fixture"
    root.mkdir()
    sheet = Image.new("RGBA", (8, 3))
    for index, color in enumerate(
        ((255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 128), (0, 0, 0, 0))
    ):
        for x in range(index * 2, index * 2 + 2):
            for y in range(3):
                sheet.putpixel((x, y), color)
    sheet.save(root / "sheet.png")
    raw = {
        "schema_version": 1,
        "pack_id": "fixture.pack",
        "version": "1.0.0",
        "display_name": "Test Fixture Only",
        "license_id": "synthetic-test-data",
        "sheet": "sheet.png",
        "sha256": hashlib.sha256((root / "sheet.png").read_bytes()).hexdigest(),
        "frame": {"width": 2, "height": 3, "anchor": [1, 2]},
        "animations": {
            "idle": {"frames": [0], "frame_ms": 200, "loop": True},
            "waiting": {"frames": [0, 1, 2], "frame_ms": 100, "loop": True},
            "complete": {"frames": [1, 2, 3], "frame_ms": 1600, "loop": False},
        },
        "reduced_motion_frame": 0,
        "tags": ["test-only"],
    }
    (root / "manifest.json").write_text(json.dumps(raw))
    return root


@pytest.fixture
def pack(pack_dir):
    return load_pack(pack_dir)


@pytest.fixture
def clock():
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

        def advance(self, seconds):
            self.now += seconds

    return Clock()
