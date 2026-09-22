"""Synthetic private candidates only: no config, accounts, network or sales."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from openvegas.emotes import provisioning as p
from openvegas.emotes.manifest import PackError

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX operator provisioning, not native Windows coverage"
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(scope="module")
def public_baseline():
    return p._public_previews()


@pytest.fixture
def candidate(tmp_path, pack_dir, monkeypatch, public_baseline):
    # Cache only in tests; the real command revalidates both public asset copies.
    monkeypatch.setattr(p, "_public_previews", lambda: public_baseline)
    tmp_path = tmp_path.resolve()
    tmp_path.chmod(0o700)
    base = json.loads((pack_dir / "manifest.json").read_bytes())
    entries = []
    for index, (pack_id, slot) in enumerate(sorted(p.PACK_SLOTS.items())):
        source = tmp_path / pack_id
        source.mkdir(mode=0o700)
        sheet = Image.new("RGBA", (8, 3), (index * 31, 81, 17, 255))
        sheet.save(source / "sheet.png")
        raw = copy.deepcopy(base)
        raw.update(
            pack_id=pack_id,
            version="1.0.0",
            tags=[slot],
            sha256=digest((source / "sheet.png").read_bytes()),
        )
        manifest = json.dumps(raw).encode()
        (source / "manifest.json").write_bytes(manifest)
        provenance = json.dumps(
            {"source_sha256": digest(b"synthetic authored test source"), "version": "1.0.0"}
        ).encode()
        (source / "provenance.json").write_bytes(provenance)
        entries.append(
            {
                "pack_id": pack_id,
                "version": "1.0.0",
                "source_directory": str(source),
                "manifest_sha256": digest(manifest),
                "sheet_sha256": raw["sha256"],
                "provenance_sha256": digest(provenance),
            }
        )
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"schema_version": 1, "release_id": "candidate-1", "packs": entries})
    )
    yield plan, tmp_path / "private-release"
    for root, directories, files in os.walk(tmp_path, followlinks=False):
        Path(root).chmod(0o700)
        for name in directories + files:
            path = Path(root) / name
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)


def update_plan(path, update):
    raw = json.loads(path.read_bytes())
    update(raw)
    path.write_text(json.dumps(raw))


def update_manifest(path, update):
    plan = json.loads(path.read_bytes())
    entry = plan["packs"][0]
    target = Path(entry["source_directory"]) / "manifest.json"
    raw = json.loads(target.read_bytes())
    update(raw)
    data = json.dumps(raw).encode()
    target.write_bytes(data)
    entry["manifest_sha256"] = digest(data)
    entry["sheet_sha256"] = raw["sha256"]
    path.write_text(json.dumps(plan))


def test_dry_run_is_default_deterministic_and_contains_no_machine_paths(candidate):
    plan, destination = candidate
    before = sorted(str(path) for path in plan.parent.rglob("*"))
    first = p.provision(plan, destination)
    second = p.provision(plan, destination)
    assert first == second
    assert first["status"] == "dry_run"
    assert first["file_count"] == 19
    assert not destination.exists()
    assert before == sorted(str(path) for path in plan.parent.rglob("*"))
    report = json.dumps(first)
    assert str(plan.parent) not in report and "source_directory" not in report
    assert first["release"]["sale_enabled"] is False
    assert first["release"]["release_approval_required"] is True
    assert first["release"]["native_compatibility_verified"] is False
    assert {entry["pack_id"] for entry in first["release"]["packs"]} == set(p.PACK_SLOTS)


def test_apply_backend_layout_readonly_hashes_and_exact_idempotency(candidate, monkeypatch):
    from server.services.emote_delivery import load_delivery_pack

    plan, destination = candidate
    result = p.provision(plan, destination, apply=True)
    assert result["status"] == "provisioned"
    assert digest((destination / p.RELEASE_FILE).read_bytes()) == result["manifest_sha256"]
    monkeypatch.setenv("OPENVEGAS_EMOTE_PACK_ROOT", str(destination))
    for entry in result["release"]["packs"]:
        bundle = load_delivery_pack(entry["pack_id"], entry)
        assert bundle["pack_id"] == entry["pack_id"]
        assert bundle["version"] == "1.0.0"
        for name, info in entry["files"].items():
            data = (destination / entry["delivery_resource"] / name).read_bytes()
            assert info == {"sha256": digest(data), "bytes": len(data)}
    stats = {}
    for path in (destination, *destination.rglob("*")):
        info = path.stat()
        assert stat.S_IMODE(info.st_mode) == (0o500 if path.is_dir() else 0o400)
        stats[path] = p._stamp(info)
    monkeypatch.setattr(os, "write", lambda *a, **kw: pytest.fail("idempotent run wrote bytes"))
    for apply in (False, True):
        assert p.provision(plan, destination, apply=apply)["status"] == "already_provisioned"
    assert stats == {path: p._stamp(path.stat()) for path in stats}


def test_reordering_plan_and_different_destinations_preserve_hash(candidate):
    plan, destination = candidate
    original = p.provision(plan, destination)
    update_plan(plan, lambda raw: raw["packs"].reverse())
    assert p.provision(plan, destination.with_name("other")) == original


@pytest.mark.parametrize(
    "update",
    [
        lambda raw: raw.update(schema_version=True),
        lambda raw: raw.update(schema_version=2),
        lambda raw: raw.update(extra="ignored?"),
        lambda raw: raw.update(release_id="../bad"),
        lambda raw: raw.update(packs=None),
        lambda raw: raw["packs"].pop(),
        lambda raw: raw["packs"].append(raw["packs"][0]),
        lambda raw: raw["packs"].__setitem__(1, raw["packs"][0]),
        lambda raw: raw["packs"][0].update(pack_id="openvegas.unknown"),
        lambda raw: raw["packs"][0].update(version="x" * 65),
        lambda raw: raw["packs"][0].update(manifest_sha256="A" * 64),
        lambda raw: raw["packs"][0].update(sheet_sha256="not-a-hash"),
        lambda raw: raw["packs"][0].update(sale_enabled=True),
        lambda raw: raw["packs"][0].update(source_directory=raw["packs"][1]["source_directory"]),
    ],
)
def test_reject_invalid_plan_before_writing(candidate, update):
    plan, destination = candidate
    update_plan(plan, update)
    with pytest.raises(PackError):
        p.provision(plan, destination, apply=True)
    assert not destination.exists()


@pytest.mark.parametrize(
    "change",
    [
        lambda raw: raw.update(pack_id="wrong.pack"),
        lambda raw: raw.update(version="1.0.1"),
        lambda raw: raw.update(tags=["wrong-slot"]),
        lambda raw: raw.update(sheet="../secret.png"),
        lambda raw: raw.update(sha256="0" * 64),
        lambda raw: raw["frame"].update(width=511),
        lambda raw: raw["animations"]["complete"].update(loop=True),
    ],
)
def test_reject_identity_integrity_geometry_and_animation(candidate, change):
    plan, destination = candidate
    update_manifest(plan, change)
    with pytest.raises(PackError):
        p.provision(plan, destination, apply=True)
    assert not destination.exists()


@pytest.mark.parametrize("name", ["manifest.json", "sheet.png"])
def test_reject_bytes_modified_after_pinning(candidate, name):
    plan, destination = candidate
    source = Path(json.loads(plan.read_bytes())["packs"][0]["source_directory"])
    with (source / name).open("ab") as handle:
        handle.write(b" ")
    with pytest.raises(PackError, match="checksum"):
        p.provision(plan, destination, apply=True)
    assert not destination.exists()


def test_reject_oversize_file_and_non_png(candidate):
    plan, destination = candidate
    source = Path(json.loads(plan.read_bytes())["packs"][0]["source_directory"])
    sheet = source / "sheet.png"
    with sheet.open("wb") as handle:
        handle.truncate(p.MAX_PRIVATE_SHEET_BYTES + 1)
    with pytest.raises(PackError, match="bounded"):
        p.provision(plan, destination)
    sheet.write_bytes(b"not png")
    update_manifest(plan, lambda raw: raw.update(sha256=digest(b"not png")))
    with pytest.raises(PackError, match="PNG"):
        p.provision(plan, destination)


@pytest.mark.parametrize("reencode", [False, True])
def test_public_preview_cannot_be_relabelled_or_reencoded(candidate, reencode):
    plan, destination = candidate
    entry = json.loads(plan.read_bytes())["packs"][0]
    source = Path(entry["source_directory"])
    public = p.REPO_ROOT / "openvegas/emotes/assets/beat-maker"
    raw = json.loads((public / "manifest.json").read_bytes())
    sheet = (public / "sheet.png").read_bytes()
    (source / "sheet.png").write_bytes(sheet)
    if reencode:
        with Image.open(public / "sheet.png") as image:
            info = PngImagePlugin.PngInfo()
            info.add_text("test", "re-encoded public art must still fail")
            image.save(source / "sheet.png", pnginfo=info)
        assert (source / "sheet.png").read_bytes() != sheet
    raw.update(
        pack_id=entry["pack_id"],
        version="1.0.0",
        sha256=digest((source / "sheet.png").read_bytes()),
    )
    update_manifest(plan, lambda value: (value.clear(), value.update(raw)))
    with pytest.raises(PackError, match="Public preview"):
        p.provision(plan, destination, apply=True)
    assert not destination.exists()


def test_public_version_namespace_cannot_be_reused(candidate, public_baseline):
    plan, destination = candidate
    entry = json.loads(plan.read_bytes())["packs"][0]
    version = next(v for pack, v in public_baseline[1] if pack == entry["pack_id"])
    update_manifest(plan, lambda raw: raw.update(version=version))
    source = Path(entry["source_directory"])
    data = json.dumps({"source_sha256": digest(b"synthetic source"), "version": version}).encode()
    (source / "provenance.json").write_bytes(data)
    update_plan(plan, lambda raw: raw["packs"][0].update(provenance_sha256=digest(data)))
    update_plan(plan, lambda raw: raw["packs"][0].update(version=version))
    with pytest.raises(PackError, match="Public preview"):
        p.provision(plan, destination)


@pytest.mark.parametrize(
    "raw",
    [
        "relative",
        "/tmp/../somewhere",
        "//server/share",
        "/some//path",
        "/some/./path",
        "/some\\path",
        "/tmp/a\nsecret",
    ],
)
def test_reject_noncanonical_paths(candidate, raw):
    plan, _ = candidate
    with pytest.raises(PackError, match="canonical"):
        p.provision(plan, raw)


@pytest.mark.parametrize("part", sorted(p.BLOCKED_PARTS))
def test_reject_public_or_package_destination(candidate, part):
    plan, destination = candidate
    with pytest.raises(PackError, match="public"):
        p.provision(plan, destination.parent / part / "premium")


def test_reject_checkout_even_in_nondistribution_directory(candidate):
    plan, _ = candidate
    with pytest.raises(PackError, match="checkout"):
        p.provision(plan, p.REPO_ROOT / "apparently-private")
    update_plan(
        plan, lambda raw: raw["packs"][0].update(source_directory=str(p.REPO_ROOT / "input"))
    )
    with pytest.raises(PackError, match="checkout"):
        p.prepare(plan)


def test_reject_other_git_checkout_and_world_readable_parent(candidate):
    plan, destination = candidate
    other = plan.parent / "other-checkout"
    other.mkdir(mode=0o700)
    (other / ".git").write_text("gitdir: /elsewhere")
    with pytest.raises(PackError, match="Git"):
        p.provision(plan, other / "premium")
    plan.parent.chmod(0o755)
    try:
        with pytest.raises(PackError, match="owner-only"):
            p.provision(plan, destination)
    finally:
        plan.parent.chmod(0o700)


@pytest.mark.parametrize(
    "location",
    ["plan", "source", "source-parent", "manifest", "sheet", "destination", "destination-parent"],
)
def test_no_symlink_traversal(candidate, location):
    plan, destination = candidate
    entry = json.loads(plan.read_bytes())["packs"][0]
    source = Path(entry["source_directory"])
    if location == "plan":
        link = plan.with_name("linked-plan.json")
        link.symlink_to(plan)
        plan = link
    elif location == "source":
        link = source.with_name("linked-source")
        link.symlink_to(source, target_is_directory=True)
        update_plan(plan, lambda raw: raw["packs"][0].update(source_directory=str(link)))
    elif location == "source-parent":
        link = plan.parent / "linked-parent"
        link.symlink_to(plan.parent, target_is_directory=True)
        update_plan(
            plan, lambda raw: raw["packs"][0].update(source_directory=str(link / source.name))
        )
    elif location in {"manifest", "sheet"}:
        path = source / ("manifest.json" if location == "manifest" else "sheet.png")
        moved = path.with_name("moved")
        path.rename(moved)
        path.symlink_to(moved)
    elif location == "destination":
        destination.symlink_to(source, target_is_directory=True)
    else:
        link = plan.parent / "linked-parent"
        link.symlink_to(plan.parent, target_is_directory=True)
        destination = link / "output"
    with pytest.raises((OSError, PackError)):
        p.provision(plan, destination, apply=True)


@pytest.mark.parametrize("kind", ["hardlink", "fifo"])
def test_reject_nonordinary_source_file_without_blocking(candidate, kind):
    plan, destination = candidate
    source = Path(json.loads(plan.read_bytes())["packs"][0]["source_directory"])
    sheet = source / "sheet.png"
    if kind == "hardlink":
        os.link(sheet, source / "alias.png")
    else:
        sheet.unlink()
        os.mkfifo(sheet)
    with pytest.raises(PackError, match="regular"):
        p.provision(plan, destination, apply=True)
    assert not destination.exists()


def test_nested_sheet_and_source_extras_are_never_copied(candidate):
    plan, destination = candidate
    source = Path(json.loads(plan.read_bytes())["packs"][0]["source_directory"])
    (source / "nested").mkdir()
    (source / "sheet.png").rename(source / "nested/sheet.png")
    (source / "must-not-copy.key").write_text("synthetic-not-a-secret")
    update_manifest(plan, lambda raw: raw.update(sheet="nested/sheet.png"))
    result = p.provision(plan, destination, apply=True)
    resource = result["release"]["packs"][0]["delivery_resource"]
    assert (destination / resource / "nested/sheet.png").is_file()
    assert not list(destination.rglob("*.key"))
    assert not list(destination.rglob(".env"))
    assert p.provision(plan, destination)["status"] == "already_provisioned"


@pytest.mark.parametrize(
    "tamper", ["bytes", "extra", "missing", "symlink", "permissions", "hardlink"]
)
def test_existing_output_conflicts_are_never_overwritten(candidate, tamper):
    plan, destination = candidate
    p.provision(plan, destination, apply=True)
    target = destination / p.RELEASE_FILE
    if tamper == "bytes":
        target.chmod(0o600)
        target.write_bytes(b"evil")
        target.chmod(0o400)
    elif tamper == "permissions":
        target.chmod(0o644)
    else:
        destination.chmod(0o700)
        if tamper == "extra":
            (destination / "unexpected").write_text("retain this")
        elif tamper == "missing":
            target.unlink()
        elif tamper == "symlink":
            target.unlink()
            target.symlink_to(plan)
        else:
            os.link(target, destination / "alias")
        destination.chmod(0o500)
    def snapshot():
        result = {}
        for path in destination.rglob("*"):
            info = path.lstat()
            # Validation reads may change atime, but must not rewrite data,
            # replace inodes, change permissions or follow symlinks.
            metadata = tuple(getattr(info, name) for name in (
                "st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid",
                "st_size", "st_mtime_ns", "st_ctime_ns",
            ))
            content = os.readlink(path) if path.is_symlink() else (
                path.read_bytes() if path.is_file() else None
            )
            result[path] = (metadata, content)
        return result

    before = snapshot()
    with pytest.raises((PackError, OSError)):
        p.provision(plan, destination, apply=True)
    assert before == snapshot()


def test_interrupted_output_fails_closed_and_never_reused(candidate, monkeypatch):
    plan, destination = candidate

    def fail_write(*args):
        raise OSError("simulated disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", fail_write)
        with pytest.raises(OSError):
            p.provision(plan, destination, apply=True)
    assert destination.is_dir()
    assert not (destination / p.RELEASE_FILE).exists()
    with pytest.raises(PackError, match="sealed"):
        p.provision(plan, destination, apply=True)


def test_source_changes_after_prepare_do_not_change_validated_snapshot(candidate, monkeypatch):
    plan, destination = candidate
    bundle = p.prepare(plan)
    source = Path(json.loads(plan.read_bytes())["packs"][0]["source_directory"])
    (source / "sheet.png").write_bytes(b"changed later")
    monkeypatch.setattr(p, "prepare", lambda _: bundle)
    result = p.provision(plan, destination, apply=True)
    assert result["manifest_sha256"] == bundle.sha256
    for relative, data in bundle.files:
        assert (destination / relative).read_bytes() == data


def test_secure_platform_required(candidate, monkeypatch):
    plan, destination = candidate
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(PackError, match="POSIX"):
        p.provision(plan, destination)


def test_catalog_remains_preview_only(candidate):
    from openvegas.store.catalog import STORE_CATALOG, public_catalog

    original = copy.deepcopy(STORE_CATALOG)
    p.provision(*candidate, apply=True)
    assert STORE_CATALOG == original
    for pack_id in p.PACK_SLOTS:
        item = public_catalog()[pack_id]
        assert item["purchasable"] is False
        assert "delivery_resource" not in item["asset"]


def test_cli_offline_no_config_default_dry_run_and_explicit_apply(candidate, tmp_path):
    plan, destination = candidate
    home = tmp_path / "empty-home"
    home.mkdir()
    env = {
        "PATH": os.defpath,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    # Guard before imports: the operator command must never consult credentials
    # or bootstrap an application just to copy reviewed local assets.
    bootstrap = r"""
import os, runpy, socket, sys
def denied(*args, **kwargs):
    raise RuntimeError("network forbidden")
socket.socket.connect = denied
socket.create_connection = denied
socket.getaddrinfo = denied
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        value = os.fsdecode(args[0])
        if os.path.basename(value).startswith(".env") or "/.openvegas/" in value:
            raise RuntimeError("configuration access forbidden")
    if event == "import" and args[0] in {"dotenv", "openvegas.cli", "openvegas.config"}:
        raise RuntimeError("configuration import forbidden")
sys.addaudithook(audit)
root = sys.argv.pop(1)
sys.path.insert(0, root)
runpy.run_path(root + "/scripts/provision_emote_packs.py", run_name="__main__")
"""
    command = [
        sys.executable,
        "-B",
        "-c",
        bootstrap,
        str(p.REPO_ROOT),
        "--plan",
        str(plan),
        "--destination",
        str(destination),
    ]
    result = subprocess.run(
        command, env=env, cwd=home, capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "dry_run"
    assert not destination.exists()
    result = subprocess.run(
        [*command, "--apply"],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "provisioned"
    result = subprocess.run(
        [sys.executable, "-B", "-c", bootstrap, str(p.REPO_ROOT), "--example"],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)["packs"]) == 6


def test_cli_redacts_os_error_details(candidate, monkeypatch, capsys):
    from scripts import provision_emote_packs as cli

    def fail(*args, **kwargs):
        raise OSError("sensitive-location-or-credential")

    monkeypatch.setattr(cli, "provision", fail)
    assert cli.main(["--plan", str(candidate[0]), "--destination", str(candidate[1])]) == 2
    assert "sensitive-location-or-credential" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "change",
    [
        lambda raw: raw.update(source_sha256="not-a-hash"),
        lambda raw: raw.update(version="9.9.9"),
    ],
)
def test_pinned_provenance_still_requires_source_and_version(candidate, change):
    plan, destination = candidate
    entry = json.loads(plan.read_bytes())["packs"][0]
    path = Path(entry["source_directory"]) / "provenance.json"
    raw = json.loads(path.read_bytes())
    change(raw)
    data = json.dumps(raw).encode()
    path.write_bytes(data)
    update_plan(plan, lambda value: value["packs"][0].update(provenance_sha256=digest(data)))
    with pytest.raises(PackError, match="Provenance"):
        p.provision(plan, destination, apply=True)
    assert not destination.exists()


def test_artwork_fingerprint_pins_all_three_exact_files(candidate):
    plan, destination = candidate
    result = p.provision(plan, destination)
    for entry in result["release"]["packs"]:
        keys = {
            "manifest_sha256": "manifest.json",
            "sheet_sha256": "sheet.png",
            "provenance_sha256": "provenance.json",
        }
        hashes = {key: entry["files"][name]["sha256"] for key, name in keys.items()}
        assert entry["artwork_fingerprint"] == digest(p._json_bytes(hashes))
        assert entry["source_sha256"] == digest(b"synthetic authored test source")
    entry = json.loads(plan.read_bytes())["packs"][0]
    path = Path(entry["source_directory"]) / "provenance.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(PackError, match="checksum"):
        p.provision(plan, destination)
    update_plan(
        plan, lambda value: value["packs"][0].update(provenance_sha256=digest(path.read_bytes()))
    )
    revised = p.provision(plan, destination)
    assert revised["manifest_sha256"] != result["manifest_sha256"]
    assert (
        revised["release"]["packs"][0]["artwork_fingerprint"]
        != result["release"]["packs"][0]["artwork_fingerprint"]
    )


def test_bounded_duplicate_json_plan_and_missing_parent(candidate):
    plan, destination = candidate
    original = plan.read_bytes()
    plan.write_bytes(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(PackError, match="JSON"):
        p.prepare(plan)
    plan.write_bytes(b" " * (p.MAX_MANIFEST_BYTES + 1))
    with pytest.raises(PackError, match="bounded"):
        p.prepare(plan)
    plan.write_bytes(original)
    with pytest.raises(FileNotFoundError):
        p.provision(plan, destination / "new")
    assert not destination.exists()


def test_concurrent_apply_never_overwrites_or_mixes_bundles(candidate):
    from concurrent.futures import ThreadPoolExecutor

    plan, destination = candidate

    def attempt():
        try:
            return p.provision(plan, destination, apply=True)["status"]
        except (FileExistsError, PackError):
            return "refused_concurrent_attempt"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert results.count("provisioned") == 1
    assert set(results) <= {"provisioned", "already_provisioned", "refused_concurrent_attempt"}
    assert p.provision(plan, destination)["status"] == "already_provisioned"
