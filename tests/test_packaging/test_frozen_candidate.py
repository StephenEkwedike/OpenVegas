"""Candidate build guards; these unit tests are not native build evidence."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("candidate", ROOT / "scripts/build_cli_candidate.py")
candidate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(candidate)


def test_candidate_clean_environment_excludes_accounts_and_custom_paths(monkeypatch, tmp_path):
    for key in (
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "DATABASE_URL",
        "STRIPE_SECRET_KEY",
        "PYTHONPATH",
        "NODE_OPTIONS",
        "NPM_CONFIG_USERCONFIG",
        "OPENVEGAS_ENV_FILE",
    ):
        monkeypatch.setenv(key, "must-not-be-inherited")
    env = candidate.clean_environment(tmp_path)
    assert "must-not-be-inherited" not in env.values()
    assert env["HOME"] == str(tmp_path)
    assert env["OPENVEGAS_DOTENV_OVERRIDE"] == "0"


def test_candidate_never_overwrites_existing_output(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_text("existing artifact")
    with pytest.raises(SystemExit) as exc:
        candidate.main(["--output", str(tmp_path)])
    assert exc.value.code == 2
    assert marker.read_text() == "existing artifact"


def test_candidate_source_digest_tracks_code_and_resources(monkeypatch, tmp_path):
    for name in (
        "pyproject.toml",
        "requirements.lock",
        "scripts/frozen_entry.py",
        "scripts/build_cli_candidate.py",
        "openvegas/cli.py",
        "openvegas/emotes/assets/test/sheet.png",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original")
    monkeypatch.setattr(candidate, "ROOT", tmp_path)
    before = candidate.source_digest()
    (tmp_path / "openvegas/cli.py").write_text("changed")
    assert candidate.source_digest() != before
    before = candidate.source_digest()
    (tmp_path / "openvegas/emotes/assets/test/sheet.png").write_bytes(b"changed pixels")
    assert candidate.source_digest() != before
    before = candidate.source_digest()
    (tmp_path / ".env").write_text("ignored secret")
    assert candidate.source_digest() == before


@pytest.mark.parametrize("system", ["Darwin", "Linux", "Windows"])
def test_framework_collection_only_on_macos(monkeypatch, tmp_path, system):
    monkeypatch.setattr(candidate.platform, "system", lambda: system)
    monkeypatch.setattr(candidate.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(candidate, "source_digest", lambda: "fixture")
    captured = []

    class BuildNotExecuted(Exception):
        pass

    def capture(args, **kwargs):
        captured.extend(args)
        raise BuildNotExecuted

    monkeypatch.setattr(candidate, "run", capture)
    with pytest.raises(BuildNotExecuted):
        candidate.main(["--output", str(tmp_path / "candidate")])
    for framework in ("objc", "Foundation", "Security", "LocalAuthentication"):
        assert (framework in captured) == (system == "Darwin")
        if system == "Darwin":
            assert captured[captured.index(framework) - 1] == "--collect-all"
    assert captured[-1] == str(ROOT / "scripts/frozen_entry.py")
