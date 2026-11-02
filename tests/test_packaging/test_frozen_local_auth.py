"""Import wiring only; synthetic frameworks never invoke native authentication."""
import builtins
import importlib.util
import json
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_keychain_dependency_is_explicit_without_biometrics():
    root = Path(__file__).resolve().parents[2]
    dependencies = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    assert any(item.startswith("pyobjc-framework-Security") and "darwin" in item for item in dependencies)
    assert not any("LocalAuthentication" in item for item in dependencies)


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("missing", [None, "objc", "Security", "Foundation"])
def test_bundle_never_imports_mac_keychain_frameworks(monkeypatch, capsys, platform, missing):
    path = Path(__file__).resolve().parents[2] / "scripts/frozen_entry.py"
    spec = importlib.util.spec_from_file_location("frozen_entry_probe", path)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    monkeypatch.setattr(entry, "verify_mac_credential_files", lambda: {"keychain_calls": 0})
    # This test isolates framework imports from the checkout's private review
    # inventory. Exact public/private inventories have their own fail-closed test.
    from openvegas.emotes.resources import PackRepository
    monkeypatch.setattr(PackRepository, 'names', lambda self: sorted(entry.REQUIRED_PACKS))
    monkeypatch.setattr(entry, "sys", SimpleNamespace(platform=platform, frozen=True))
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_keyring=lambda: object()))
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(__version__="fixture"))
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(get_portaudio_version=lambda: (1, "fixture")))

    class NoNativeCalls:
        def __getattr__(self, name):
            raise AssertionError("Native authentication must not be invoked")

    modules = {
        "objc": SimpleNamespace(),
        "Security": SimpleNamespace(),
        "Foundation": SimpleNamespace(NSObject=NoNativeCalls()),
    }
    seen = []
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name != "LocalAuthentication"
        if name in modules:
            seen.append(name)
            if name == missing:
                raise ImportError("Synthetic missing framework")
            return modules[name]
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    entry.verify_bundle()
    report = json.loads(capsys.readouterr().out)
    assert report["keychain_frameworks_imported"] is False
    assert seen == []
    if platform == "darwin":
        assert report["keyring_backend"] == "private_file"
        assert report["credential_storage_check"]["keychain_calls"] == 0


def test_real_mac_storage_probe(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/frozen_entry.py"
    spec = importlib.util.spec_from_file_location("frozen_storage_probe", path)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert entry.verify_mac_credential_files() == {
        "private_files": True, "keychain_calls": 0, "synthetic_only": True}
