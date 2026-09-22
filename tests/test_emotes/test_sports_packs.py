import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from openvegas.emotes.render import fit_frame
from openvegas.emotes.resources import PackRepository

SPORTS = ("skyline-dunk", "bicycle-finish", "three-point-glow")
PACK_ARTIFACTS = ("sheet.png", "manifest.json", "provenance.json")
PREVIEW_ARTIFACTS = (
    "contact-light.png", "contact-dark.png", "complete-light.gif", "complete-dark.gif",
)


def _assert_artifact_bytes_equal(actual_path: Path, expected_path: Path) -> None:
    actual, expected = actual_path.read_bytes(), expected_path.read_bytes()
    # Pytest's binary sequence diff can take minutes for different PNG encodings.
    if actual != expected:
        raise AssertionError(
            f"Artifact bytes differ: {actual_path.name}; "
            f"actual size={len(actual)} sha256={hashlib.sha256(actual).hexdigest()}; "
            f"expected size={len(expected)} sha256={hashlib.sha256(expected).hexdigest()}"
        )


def test_artifact_comparison_reports_bounded_exact_byte_failure(tmp_path):
    actual, expected = tmp_path / "actual.png", tmp_path / "expected.png"
    original = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4096
    actual.write_bytes(original)
    expected.write_bytes(original)
    _assert_artifact_bytes_equal(actual, expected)

    changed = original[:-1] + b"\x00"
    actual.write_bytes(changed)
    with pytest.raises(AssertionError) as failure:
        _assert_artifact_bytes_equal(actual, expected)
    diagnostic = str(failure.value)
    assert len(diagnostic) < 300
    assert hashlib.sha256(original).hexdigest() in diagnostic
    assert hashlib.sha256(changed).hexdigest() in diagnostic
    assert "actual size=" in diagnostic and "expected size=" in diagnostic


@pytest.mark.parametrize("slug", SPORTS)
def test_authored_sports_pack_has_twelve_poses_and_one_bounded_completion(slug):
    pack = PackRepository().load(slug)
    assert (pack.manifest.width, pack.manifest.height) == (160, 120)
    assert "completion" in pack.manifest.tags
    complete = pack.manifest.animations["complete"]
    assert not complete.loop and complete.duration == pytest.approx(5.4)
    assert set(complete.frames) == set(range(12))
    assert len({pack.frame(i).tobytes() for i in range(12)}) == 12
    for index in range(12):
        frame = pack.frame(index)
        assert set(frame.getchannel("A").tobytes()) <= {0, 255}
        assert frame.getchannel("A").getbbox() is not None
        fitted = fit_frame(frame, max_columns=80, max_rows=20)
        assert fitted.width <= 80 and fitted.height <= 40
    root = Path(__file__).resolve().parents[2]
    runtime = root / "openvegas/emotes/assets" / slug
    web = root / "ui/assets/emotes/previews" / slug
    marketing = root / "creatives/emotes/marketing/assets/sports" / slug
    for filename in PACK_ARTIFACTS:
        _assert_artifact_bytes_equal(runtime / filename, web / filename)
        _assert_artifact_bytes_equal(runtime / filename, marketing / filename)
    for filename in PREVIEW_ARTIFACTS:
        _assert_artifact_bytes_equal(web / filename, marketing / filename)
    provenance = json.loads((web / "provenance.json").read_text())
    assert provenance["purchasable"] is False
    assert provenance["authored_frames"] == 12
    for theme in ("light", "dark"):
        with Image.open(web / f"complete-{theme}.gif") as animation:
            assert "loop" not in animation.info
            duration = 0
            for index in range(animation.n_frames):
                animation.seek(index)
                duration += animation.info["duration"]
            assert duration == 5400
    assert (
        hashlib.sha256((web / "sheet.png").read_bytes()).hexdigest()
        == pack.manifest.sha256
    )


@pytest.mark.parametrize("slug", SPORTS)
def test_all_sports_rebuild_every_required_artifact_from_saved_source(slug, tmp_path):
    from scripts.rebuild_sports_revision import rebuild

    root = Path(__file__).resolve().parents[2]
    source = root / "creatives/emotes/sources/sports" / slug
    rebuilt = tmp_path / slug
    rebuild(source, rebuilt, slug)
    provenance = json.loads((rebuilt / "provenance.json").read_text())
    assert provenance["revision"] == ("0.1.1" if slug == "skyline-dunk" else "0.1.2")
    assert provenance["sheet_encoding"] == "rgba8-filter0-stored-deflate-v1"
    assert provenance["contact_preview_encoding"] == "rgba8-filter0-stored-deflate-v1"
    if slug != "skyline-dunk":
        assert provenance["highlight_reference_sha256"] is None
    assert (
        provenance["source_sha256"]
        == hashlib.sha256((source / "source.png").read_bytes()).hexdigest()
    )
    runtime = root / "openvegas/emotes/assets" / slug
    web = root / "ui/assets/emotes/previews" / slug
    marketing = root / "creatives/emotes/marketing/assets/sports" / slug
    source_artifacts = (
        ("source.png", "detail-reference.png") if slug == "skyline-dunk"
        else ("source.png", "prepared-source.png", "prompt-set.json")
    )
    assert {p.name for p in rebuilt.iterdir()} == set(
        PACK_ARTIFACTS + PREVIEW_ARTIFACTS + source_artifacts
    )
    for filename in PACK_ARTIFACTS:
        for destination in (runtime, web, marketing):
            _assert_artifact_bytes_equal(rebuilt / filename, destination / filename)
    for filename in PREVIEW_ARTIFACTS:
        for destination in (web, marketing):
            _assert_artifact_bytes_equal(rebuilt / filename, destination / filename)
    for filename in (*source_artifacts, "provenance.json"):
        _assert_artifact_bytes_equal(rebuilt / filename, source / filename)


def test_skyline_encoding_lineage_and_approval_gates():
    from scripts.rebuild_sports_revision import SKYLINE_RGBA_SHA256

    root = Path(__file__).resolve().parents[2]
    pack = root / "openvegas/emotes/assets/skyline-dunk"
    provenance = json.loads((pack / "provenance.json").read_text())
    manifest = json.loads((pack / "manifest.json").read_text())
    lineage = provenance["encoding_lineage"]
    assert manifest["version"] == provenance["revision"] == "0.1.1"
    assert lineage["previous_version"] == "0.1.0"
    assert lineage["previous_sheet_sha256"] == "b25b5a67cb2b1700ada1539934c9e6b2b54dd5e0c9fbb52feac16c156fb0551a"
    assert lineage["previous_manifest_sha256"] == "6071a5053d98faa8d5eed9bec176a21fc77e108afd950087e830d975fa21b1d0"
    with Image.open(pack / "sheet.png") as image:
        assert hashlib.sha256(image.convert("RGBA").tobytes()).hexdigest() == SKYLINE_RGBA_SHA256
    assert lineage["rgba_sha256"] == SKYLINE_RGBA_SHA256
    assert lineage["native_compatibility_approved"] is False
    assert lineage["sales_activation_authorized"] is False
    assert provenance["purchasable"] is False
    assert "native-animation-and-sale-review-pending" in provenance["approval"]


@pytest.mark.parametrize("filename", ["source.png", "detail-reference.png"])
def test_skyline_rebuild_rejects_unreviewed_inputs(filename, tmp_path):
    import shutil

    from scripts.rebuild_sports_revision import rebuild

    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "source"
    shutil.copytree(root / "creatives/emotes/sources/sports/skyline-dunk", source)
    (source / filename).write_bytes(b"changed artwork")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="Artwork changed"):
        rebuild(source, output, "skyline-dunk")
    assert not output.exists()


@pytest.mark.parametrize("slug", SPORTS)
def test_sports_delivery_registry_matches_canonical_artifacts(slug):
    root = Path(__file__).resolve().parents[2]
    marketing = root / "creatives/emotes/marketing"
    registry = json.loads((marketing / "assets/sports/slots.json").read_text())
    slot = registry["slots"][slug]
    assert slot["pack"] == "sports/" + slug
    assert "pending" in slot["status"]
    delivery = json.loads((marketing / "DELIVERY.json").read_text())
    entry = next(item for item in delivery["sports"] if item["slug"] == slug)
    pack = marketing / "assets/sports" / slug
    manifest = json.loads((pack / "manifest.json").read_text())
    assert entry["sha256"] == manifest["sha256"]
    assert entry["provenanceSha256"] == hashlib.sha256((pack / "provenance.json").read_bytes()).hexdigest()
    assert entry["highlightReferenceSha256"] == json.loads((pack / "provenance.json").read_text())["highlight_reference_sha256"]
    assert entry["completionMs"] == 5400


def test_revision_cleanup_rejects_unreviewed_artwork(tmp_path):
    from scripts.rebuild_sports_revision import prepare

    source = tmp_path / "changed.png"
    Image.new("RGBA", (1448, 1086), "white").save(source)
    with pytest.raises(ValueError, match="Artwork changed"):
        prepare(source, "bicycle-finish")


def test_overhead_court_padding_keeps_all_authored_pixels():
    from scripts.rebuild_sports_revision import prepare

    root = Path(__file__).resolve().parents[2]
    source = root / "creatives/emotes/sources/sports/three-point-glow/source.png"
    prepared = prepare(source, "three-point-glow")
    with Image.open(source) as raw:
        image = raw.convert("RGBA")
    for row in range(3):
        for col in range(4):
            expected = image.crop(
                (col * 362, row * 362, (col + 1) * 362, (row + 1) * 362)
            )
            x, y = col * 366 + 2, row * 366 + 2
            assert (
                prepared.crop((x, y, x + 362, y + 362)).tobytes() == expected.tobytes()
            )
