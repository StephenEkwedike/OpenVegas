import hashlib
import json
import os
import zipfile

import pytest

from openvegas.emotes.manifest import (
    PackError,
    load_pack,
    parse_json,
)
from openvegas.emotes.resources import PackRepository


def mutate(root, function):
    path = root / "manifest.json"
    raw = json.loads(path.read_text())
    function(raw)
    path.write_text(json.dumps(raw))


def symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != 1314:
            raise
        if os.environ.get("EMOTE_REQUIRE_WINDOWS_SYMLINKS") == "1":
            pytest.fail("Windows security gate requires symlink creation privilege")
        pytest.skip("Windows symlink privilege unavailable (native coverage not run)")


def test_load_configurable_geometry_and_copy(pack):
    assert (pack.manifest.width, pack.manifest.height, pack.manifest.anchor) == (
        2,
        3,
        (1, 2),
    )
    assert pack.manifest.animations["complete"].duration == 4.8
    frame = pack.frame(0)
    frame.putpixel((0, 0), (0, 0, 0, 0))
    assert pack.frame(0).getpixel((0, 0)) == (255, 0, 0, 255)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("pack_id", "../../x"),
        ("display_name", "evil\x1b[2J"),
        ("license_id", "a\nsecret"),
        ("sha256", "REPLACE_WITH_COMPUTED_SHA256"),
        ("sheet", "../sheet.png"),
        ("sheet", "/tmp/sheet.png"),
        ("sheet", "https://a/sheet.png"),
        ("sheet", "a\\sheet.png"),
        ("sheet", "./sheet.png"),
        ("sheet", "sheet.svg"),
        ("reduced_motion_frame", 4),
        ("tags", "bad"),
        ("unknown", "script"),
    ],
)
def test_reject_metadata(pack_dir, field, value):
    mutate(pack_dir, lambda raw: raw.update({field: value}))
    with pytest.raises(PackError):
        load_pack(pack_dir)


@pytest.mark.parametrize(
    "field,value",
    [
        ("frame_ms", 0),
        ("frame_ms", -1),
        ("frame_ms", float("inf")),
        ("frame_ms", float("nan")),
        ("frame_ms", True),
        ("frame_ms", 100),
        ("frame_ms", 3000),
        ("loop", True),
        ("loop", "false"),
        ("frames", []),
        ("frames", [-1]),
        ("frames", [999]),
        ("frames", [True]),
    ],
)
def test_invalid_complete(pack_dir, field, value):
    mutate(pack_dir, lambda raw: raw["animations"]["complete"].update({field: value}))
    with pytest.raises(PackError):
        load_pack(pack_dir)


@pytest.mark.parametrize(
    "geometry",
    [{"width": 0}, {"width": 3}, {"height": 513}, {"anchor": [2, 3]}, {"anchor": [0]}],
)
def test_geometry(pack_dir, geometry):
    mutate(pack_dir, lambda raw: raw["frame"].update(geometry))
    with pytest.raises(PackError):
        load_pack(pack_dir)


def test_hash_corrupt_and_not_png(pack_dir):
    (pack_dir / "sheet.png").write_bytes(b"bad")
    with pytest.raises(PackError, match="checksum"):
        load_pack(pack_dir)
    mutate(pack_dir, lambda raw: raw.update(sha256=hashlib.sha256(b"bad").hexdigest()))
    with pytest.raises(PackError, match="PNG"):
        load_pack(pack_dir)


@pytest.mark.parametrize("name", ["sheet.png", "manifest.json"])
def test_no_symlinks(pack_dir, name):
    path = pack_dir / name
    target = pack_dir.parent / name
    path.rename(target)
    symlink(path, target)
    with pytest.raises(PackError):
        load_pack(pack_dir)


def test_no_root_symlink(pack_dir):
    link = pack_dir.parent / "linked"
    symlink(link, pack_dir, directory=True)
    with pytest.raises(PackError):
        load_pack(link)


def test_nested_symlink(pack_dir):
    outside = pack_dir.parent / "outside"
    outside.mkdir()
    (pack_dir / "sheet.png").rename(outside / "sheet.png")
    symlink(pack_dir / "nested", outside, directory=True)
    mutate(pack_dir, lambda raw: raw.update(sheet="nested/sheet.png"))
    with pytest.raises(PackError):
        load_pack(pack_dir)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO test; not a Windows pass")
def test_fifo(pack_dir):
    os.mkfifo(pack_dir / "pipe.png")
    mutate(pack_dir, lambda raw: raw.update(sheet="pipe.png"))
    with pytest.raises(PackError):
        load_pack(pack_dir)


def test_json_duplicates_and_bounds():
    for data in (b'{"schema_version":1,"schema_version":1}', b"[1]", b"{" * 70000):
        with pytest.raises(PackError):
            parse_json(data)


def test_decoded_limits_before_loading(pack_dir, monkeypatch):
    from openvegas.emotes import manifest

    monkeypatch.setattr(manifest, "MAX_PIXELS", 20)
    with pytest.raises(PackError, match="pixel"):
        load_pack(pack_dir)


def test_installed_zip_resources(pack_dir, tmp_path):
    archive = tmp_path / "resources.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for path in pack_dir.iterdir():
            z.write(path, "assets/fixture/" + path.name)
    with zipfile.ZipFile(archive) as z:
        repository = PackRepository(zipfile.Path(z, "assets/"))
        assert repository.names() == ["fixture"]
        assert repository.load("fixture").manifest.pack_id == "fixture.pack"
