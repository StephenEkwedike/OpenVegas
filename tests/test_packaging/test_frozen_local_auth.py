"""Import wiring only; synthetic frameworks never invoke native authentication."""
import builtins
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("missing", [None, "objc", "Security", "Foundation"])
def test_bundle_requires_frameworks_on_darwin_only(monkeypatch, capsys, platform, missing):
    path = Path(__file__).resolve().parents[2] / "scripts/frozen_entry.py"
    spec = importlib.util.spec_from_file_location("frozen_entry_probe", path)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
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
    if platform == "darwin" and missing:
        with pytest.raises(ImportError, match="Synthetic missing framework"):
            entry.verify_bundle()
    else:
        entry.verify_bundle()
        report = json.loads(capsys.readouterr().out)
        assert report["keychain_frameworks_imported"] is (True if platform == "darwin" else None)
        assert seen == (list(modules) if platform == "darwin" else [])
