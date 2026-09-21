"""Installed-module and simulated-frozen dispatch with clean, offline homes."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from uuid import uuid4

import pytest
from openvegas.emotes import hook_dispatch as dispatch
from openvegas.emotes import hooks as h
from openvegas.emotes.controller import EmoteController
from openvegas.emotes.spool import _locked, atomic_write


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "runtime with 'quotes' and $(touch SHOULD_NOT_EXIST)"
    venv.EnvBuilder(with_pip=False).create(root)
    python = root / "bin/python"
    site = root / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    package = site / "openvegas/emotes"
    package.mkdir(parents=True)
    source = Path(sys.modules[EmoteController.__module__].__file__).parent
    for module in source.glob("*.py"):
        shutil.copyfile(module, package / module.name)
    for name in ("hooks.py", "hook_dispatch.py", "adapters.py"):
        shutil.copyfile(Path(h.__file__).with_name(name), package / name)
    (site / "openvegas/__init__.py").write_text("")
    (site / "test-dependencies.pth").write_text("\n".join(p for p in sys.path if p.endswith("site-packages")))
    # -I still loads this installed sitecustomize. Catch content/config imports
    # and socket operations before application package imports in child processes.
    (site / "sitecustomize.py").write_text('''
import socket, sys
def deny(*a, **k):
    raise RuntimeError("network forbidden in hook test")
socket.create_connection = socket.getaddrinfo = deny
socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = deny
def audit(event, args):
    if event == "import" and args[0] in ("openvegas.cli", "openvegas.config", "openvegas.telemetry", "dotenv"):
        raise RuntimeError("application startup forbidden")
    if event == "open" and isinstance(args[0], str) and args[0].rsplit("/", 1)[-1].startswith(".env"):
        raise RuntimeError("dotenv read forbidden")
sys.addaudithook(audit)
''')
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.defpath, "LANG": "C.UTF-8",
           "XDG_DATA_HOME": str(home / "data"), "XDG_CONFIG_HOME": str(home / "config")}
    driver = root / "frozen_entry.py"
    driver.write_text('''
import sys
if sys.argv[1:2] == ["--openvegas-emote-hook"]:
    from openvegas.emotes.hook_dispatch import main
    raise SystemExit(main(sys.argv[2:]))
raise RuntimeError("normal CLI entry reached")
''')
    frozen = root / "openvegas-frozen"
    frozen.write_text("#!/bin/sh\nexec " + shlex.join([str(python), "-I", str(driver)]) + ' "$@"\n')
    frozen.chmod(0o700)
    return python, frozen, env, home


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("provider", ["claude", "gemini"])
def test_installed_and_frozen_generated_handlers_are_passive(runtime, tmp_path, monkeypatch, frozen, provider):
    python, binary, env, home = runtime
    monkeypatch.setattr(sys, "executable", str(binary if frozen else python))
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(h, "claude_version", lambda: (2, 1, 270))
    monkeypatch.setattr(h, "gemini_version", lambda: (0, 59, 0))
    assert h._probe_handler()
    path = tmp_path / ("." + provider) / "settings.json"
    result = h.setup(path, provider=provider, apply=True, activity_only=True, observation_only=True)
    root = Path(result["installation"])
    receipt = h._receipt(root)
    event = "BeforeAgent" if provider == "gemini" else "UserPromptSubmit"
    handler = receipt["entries"][event]["hooks"][0]
    command = (["/bin/sh", "-c", handler["command"]] if provider == "gemini"
               else [handler["command"], *handler["args"]])
    for data in (b"not-json", json.dumps({"hook_event_name": event, "session_id": "native",
                     "prompt_id": str(uuid4()), "prompt": "PRIVATE", "token": "PRIVATE"}).encode()):
        run = subprocess.run(command, cwd=home, env=env, input=data, capture_output=True, timeout=5, check=False)
        assert (run.returncode, run.stdout, run.stderr) == (0, b"", b"")
    state = next(root.glob(provider + "-*.json"))
    assert "PRIVATE" not in state.read_text()
    assert not (home / "SHOULD_NOT_EXIST").exists()
    malformed = subprocess.run([*h._invocation(), "handle", "--bad"], cwd=home, env=env,
                               input=b"private", capture_output=True, timeout=5, check=False)
    assert (malformed.returncode, malformed.stdout, malformed.stderr) == (0, b"", b"")


@pytest.mark.parametrize("args", [[], ["setup"], ["handle"], ["handle", "--bad"], ["probe", "extra"]])
def test_malformed_dispatch_is_silent(args, capsys):
    assert dispatch.main(args) == 0
    assert capsys.readouterr() == ("", "")


def test_stalled_input_returns_success_before_host_timeout(runtime):
    python, _, env, home = runtime
    process = subprocess.Popen([str(python), "-I", "-m", "openvegas.emotes.hook_dispatch", "handle",
                                "--installation", str(home / "missing"), "--owner", uuid4().hex],
                               cwd=home, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        process.wait(timeout=2)
        assert process.returncode == 0
        assert process.stdout.read() == b"" and process.stderr.read() == b""
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=2)


def test_stalled_handler_exits_successfully_before_native_timeout(runtime):
    python, _, env, home = runtime
    code = '''
import time
from openvegas.emotes import hook_dispatch, hooks
hooks.handle_input = lambda *a, **k: time.sleep(10)
raise SystemExit(hook_dispatch.main(["handle", "--installation", "/missing", "--owner", "a" * 32]))
'''
    result = subprocess.run([str(python), "-I", "-c", code], cwd=home, env=env,
                            input=b"{}", capture_output=True, timeout=3, check=False)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


def test_probe_rejects_a_binary_without_early_dispatch(runtime, monkeypatch):
    _, binary, _, _ = runtime
    binary.write_text("#!/bin/sh\nprintf 'ordinary CLI help\\n'\n")
    monkeypatch.setattr(sys, "executable", str(binary))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert not h._probe_handler()


def test_legacy_schema_one_receipt_remains_uninstallable(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "claude_version", lambda: (2, 1, 270))
    monkeypatch.setattr(h, "_probe_handler", lambda: True)
    path = tmp_path / "settings.json"
    result = h.setup(path, apply=True, activity_only=True)
    root = Path(result["installation"])
    receipt = h._receipt(root)
    receipt["schema"] = 1
    receipt.pop("provider")
    for group in receipt["entries"].values():
        group["hooks"][0]["args"][2] = "openvegas.emotes.hooks"
    body = h._bytes({"hooks": {name: [group] for name, group in receipt["entries"].items()}})
    path.write_bytes(body)
    receipt["after"] = h._digest(body)
    with _locked(root) as fd:
        atomic_write(fd, h.RECEIPT, h._bytes(receipt))
    assert h._receipt(root)["schema"] == 1
    assert h.uninstall(path, apply=True)["applied"]
    assert not path.exists()
