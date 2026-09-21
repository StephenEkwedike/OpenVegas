"""Build and install a real wheel offline; borrow local dependencies, never credentials.

These tests do not prove a fresh dependency resolution or a frozen executable.
EMOTE_BUILD_SITE may point at an existing offline setuptools/wheel toolchain.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify_emote_release.py"
spec = importlib.util.spec_from_file_location("verify_emote_release", SCRIPT)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def run(command, cwd, env):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                            timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.fixture(scope="session")
def installed(tmp_path_factory):
    root = tmp_path_factory.mktemp("emote installed wheel")
    source = root / "build input"
    source.mkdir()
    for package in ("openvegas", "server"):
        shutil.copytree(ROOT / package, source / package,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env*", "env.md"))
    shutil.copyfile(ROOT / "pyproject.toml", source / "pyproject.toml")
    wheels = root / "wheels"
    wheels.mkdir()
    env = verifier.clean_environment(root)
    builder = "from setuptools.build_meta import build_wheel; build_wheel(" + repr(str(wheels)) + ")"
    if os.environ.get("EMOTE_BUILD_SITE"):
        builder = "import sys; sys.path.insert(0, " + repr(os.environ["EMOTE_BUILD_SITE"]) + "); " + builder
    run([sys.executable, "-I", "-B", "-c", builder], source, env)
    wheel, = wheels.glob("*.whl")
    runtime = root / "runtime with spaces"
    venv.EnvBuilder(with_pip=True).create(runtime)
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run([str(python), "-I", "-B", "-m", "pip", "install", "--no-index", "--no-deps",
         "--no-compile", str(wheel)], root, env)
    site = Path(run([str(python), "-I", "-B", "-c",
                     "import sysconfig; print(sysconfig.get_path('purelib'))"], root, env).strip())
    # A plain path line does not process the donor environment's editable .pth hooks.
    (site / "test-dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n")
    return python, site, root


def verify(installed):
    python, _, root = installed
    result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--python", str(python)],
                            cwd=root, env=verifier.clean_environment(root),
                            capture_output=True, text=True, timeout=120, check=False)
    return result.returncode, json.loads(result.stdout)


def test_real_installed_wheel_and_cli_dispatch(installed):
    code, report = verify(installed)
    assert code == 0, report
    assert report["status"] == "pass"
    assert len(report["checks"]) == 22
    assert report["guard_violations"] == []
    assert {item["detail"]["pack_id"] for item in report["checks"]
            if item["name"].startswith("pack:")} == {f"openvegas.{name}" for name in verifier.PACKS}


@pytest.mark.parametrize("name", verifier.PACKS)
def test_missing_whole_pack_fails_even_if_discovery_skips_it(installed, name):
    _, site, root = installed
    pack = site / "openvegas/emotes/assets" / name
    removed = root / "removed-pack"
    pack.rename(removed)
    try:
        code, report = verify(installed)
        assert code == 1
        assert "six-pack-inventory" in report["failures"]
        assert f"pack:{name}" in report["failures"]
    finally:
        removed.rename(pack)


@pytest.mark.parametrize("filename", verifier.ASSETS)
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
@pytest.mark.parametrize("name", verifier.PACKS)
def test_missing_or_corrupt_required_asset_fails(installed, filename, damage, name):
    _, site, _ = installed
    asset = site / "openvegas/emotes/assets" / name / filename
    original = asset.read_bytes()
    try:
        if damage == "missing":
            asset.unlink()
        else:
            asset.write_bytes(b"corrupt")
        code, report = verify(installed)
        assert code == 1
        assert f"pack:{name}" in report["failures"]
    finally:
        asset.write_bytes(original)


def test_valid_json_provenance_tampering_fails_record(installed):
    _, site, _ = installed
    asset = site / "openvegas/emotes/assets/pixel-courier/provenance.json"
    original = asset.read_bytes()
    try:
        asset.write_bytes(original + b"\n")
        assert "pack:pixel-courier" in verify(installed)[1]["failures"]
    finally:
        asset.write_bytes(original)


def test_editable_install_is_rejected(installed):
    _, site, _ = installed
    metadata, = site.glob("openvegas-*.dist-info")
    direct = metadata / "direct_url.json"
    original = direct.read_bytes() if direct.exists() else None
    try:
        direct.write_text(json.dumps({"dir_info": {"editable": True}}))
        code, report = verify(installed)
        assert code == 1
        assert report["failures"] == ["installed-origin"]
    finally:
        if original is None:
            direct.unlink()
        else:
            direct.write_bytes(original)


def test_missing_cli_registration_fails_both_dispatchers(installed):
    _, site, _ = installed
    cli = site / "openvegas/cli.py"
    original = cli.read_bytes()
    assert b"cli.add_command(emote)" in original
    try:
        cli.write_bytes(original.replace(b"cli.add_command(emote)", b"# emote not registered"))
        code, report = verify(installed)
        assert code == 1
        assert "module:emote-doctor" in report["failures"]
        assert "console-entry-point:emote-doctor" in report["failures"]
        assert "console-entry-point:artist:pixel-courier" in report["failures"]
    finally:
        cli.write_bytes(original)


def test_source_shadowing_is_rejected(installed):
    _, site, root = installed
    shadow = root / "source-shadow"
    shutil.copytree(site / "openvegas", shadow / "openvegas")
    hook = site / "source-shadow.pth"
    try:
        hook.write_text("import sys; sys.path.insert(0, " + repr(str(shadow)) + ")\n")
        code, report = verify(installed)
        assert code == 1
        assert report["failures"] == ["installed-origin"]
    finally:
        hook.unlink()


def test_missing_asset_record_entry_fails(installed):
    _, site, _ = installed
    metadata, = site.glob("openvegas-*.dist-info")
    record = metadata / "RECORD"
    original = record.read_bytes()
    try:
        record.write_bytes(b"\n".join(line for line in original.split(b"\n")
                                    if not line.startswith(b"openvegas/emotes/assets/pixel-courier/sheet.png,")))
        code, report = verify(installed)
        assert code == 1
        assert "pack:pixel-courier" in report["failures"]
    finally:
        record.write_bytes(original)


def test_environment_does_not_inherit_credentials_or_pythonpath(tmp_path, monkeypatch):
    for key in ("OPENAI_API_KEY", "OPENVEGAS_ENV_FILE", "PYTHONPATH", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "MUST_NOT_PROPAGATE")
    env = verifier.clean_environment(tmp_path)
    assert "MUST_NOT_PROPAGATE" not in env.values()
    assert env["HOME"] == env["USERPROFILE"] == str(tmp_path)


@pytest.mark.parametrize("operation", [
    "import socket; socket.getaddrinfo('example.invalid', 443)",
    "import subprocess; subprocess.run(['not-a-real-command'])",
    "open('.env', 'w')",
])
def test_offline_guard_blocks_operations_in_child(tmp_path, operation):
    code = (
        "import runpy; m = runpy.run_path(" + repr(str(SCRIPT)) + "); "
        "m['install_guard'](); " + operation
    )
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code], cwd=tmp_path,
                            env=verifier.clean_environment(tmp_path), capture_output=True,
                            text=True, timeout=10, check=False)
    assert result.returncode != 0
    assert "Offline verification blocked" in result.stderr
    assert not (tmp_path / ".env").exists()


def test_bad_interpreter_reports_failure(tmp_path):
    report = tmp_path / "report.json"
    assert verifier.main(["--python", str(tmp_path / "missing"), "--json", str(report)]) == 1
    assert json.loads(report.read_text())["status"] == "fail"
