"""Candidate build guards; these unit tests are not native build evidence."""

import importlib.util
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

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


def test_ci_source_builds_use_static_openssl_and_check_macos_linkage():
    workflow = (ROOT / '.github/workflows/emote-frozen.yml').read_text(encoding='utf-8')
    assert "OPENSSL_STATIC: '1'" in workflow
    assert 'pip install --no-cache-dir -c requirements.lock' in workflow
    assert "['otool','-L',r.__file__]" in workflow
    assert "('libssl.', 'libcrypto.')" in workflow


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
        "openvegas/integrations/assets/SKILL.md",
        "openvegas/integrations/assets/register.js",
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
    for name in ('SKILL.md', 'register.js'):
        before = candidate.source_digest()
        (tmp_path / 'openvegas/integrations/assets' / name).write_text('changed native asset')
        assert candidate.source_digest() != before


def test_candidate_collects_only_public_assets_unless_explicitly_private():
    from scripts.frozen_entry import REQUIRED_PACKS, REVIEW_PACKS
    for private in (False, True):
        arguments = candidate.pack_inputs(include_review_packs=private)
        names = {Path(value.split(':openvegas/')[0]).name for value in arguments[1::2]}
        assert names == REQUIRED_PACKS | (REVIEW_PACKS if private else set())
        assert arguments[::2] == ['--add-data'] * len(names)


def test_candidate_rejects_redirected_pack_inputs(monkeypatch, tmp_path):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / 'frozen_entry.py').write_text('REQUIRED_PACKS={"fixture"}\nREVIEW_PACKS=set()\n')
    assets = tmp_path / 'openvegas/emotes/assets'
    assets.mkdir(parents=True)
    (assets / 'fixture').symlink_to(scripts, target_is_directory=True)
    monkeypatch.setattr(candidate, 'ROOT', tmp_path)
    with pytest.raises(ValueError, match='symlinked'):
        candidate.pack_inputs()


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
    for framework in ("objc", "Foundation", "Security"):
        assert (framework in captured) == (system == "Darwin")
        if system == "Darwin":
            assert captured[captured.index(framework) - 1] == "--collect-all"
    assert "LocalAuthentication" not in captured
    assert captured[-1] == str(ROOT / "scripts/frozen_entry.py")
    assert captured[captured.index('openvegas.integrations') - 1] == '--collect-all'
    assert captured[captured.index('openvegas.emotes') - 1] == '--collect-submodules'


def test_frozen_native_asset_probe_is_read_only(tmp_path, monkeypatch):
    from scripts.frozen_entry import verify_integration_assets
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('OPENVEGAS_CONFIG_HOME', str(tmp_path))
    monkeypatch.setenv('OPENVEGAS_BACKEND_URL', 'http://127.0.0.1:1')
    result = verify_integration_assets()
    assert result['claude'] > 0 and result['codex'] > 0
    assert not list(tmp_path.iterdir())


def test_native_plugin_assets_are_utf8_even_on_windows_code_pages(tmp_path, monkeypatch):
    from scripts.frozen_entry import verify_integration_assets
    original = Path.read_text

    def windows_read(path, encoding=None, errors=None):
        return original(path, encoding=encoding or 'cp1252', errors=errors)

    monkeypatch.setattr(Path, 'read_text', windows_read)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('OPENVEGAS_CONFIG_HOME', str(tmp_path))
    monkeypatch.setenv('OPENVEGAS_BACKEND_URL', 'http://127.0.0.1:1')
    assert set(verify_integration_assets()) == {'claude', 'codex'}
    assert not list(tmp_path.iterdir())


def test_private_review_inventory_never_weakens_public_pack_gate():
    from scripts.frozen_entry import REQUIRED_PACKS, REVIEW_PACKS, verify_pack_inventory
    verify_pack_inventory(list(REQUIRED_PACKS))
    with pytest.raises(ValueError):
        verify_pack_inventory(list(REQUIRED_PACKS | REVIEW_PACKS))
    verify_pack_inventory(list(REQUIRED_PACKS | REVIEW_PACKS), include_review_packs=True)
    for missing in REQUIRED_PACKS | REVIEW_PACKS:
        with pytest.raises(ValueError):
            verify_pack_inventory(list((REQUIRED_PACKS | REVIEW_PACKS) - {missing}), include_review_packs=True)
    with pytest.raises(ValueError):
        verify_pack_inventory(list(REQUIRED_PACKS | REVIEW_PACKS | {'unreviewed'}), include_review_packs=True)


def test_frozen_entry_routes_relay_arguments_without_python_interpretation(monkeypatch):
    received = []
    monkeypatch.setitem(sys.modules, 'openvegas.integrations.terminal_relay',
                        SimpleNamespace(main=received.append))
    monkeypatch.setattr(sys, 'argv', ['openvegas', '--_terminal-relay', '--', '/bin/zsh', '-l'])
    runpy.run_path(str(ROOT / 'scripts/frozen_entry.py'), run_name='__main__')
    assert received == [['--', '/bin/zsh', '-l']]


def test_source_digest_uses_platform_independent_case_sensitive_order(monkeypatch, tmp_path):
    import hashlib
    files = ['pyproject.toml', 'requirements.lock', 'scripts/frozen_entry.py',
             'scripts/build_cli_candidate.py', 'openvegas/assets/SKILL.md',
             'openvegas/assets/game.js']
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    monkeypatch.setattr(candidate, 'ROOT', tmp_path)
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b'\0')
        digest.update(hashlib.sha256(name.encode()).digest())
    assert candidate.source_digest() == digest.hexdigest()
