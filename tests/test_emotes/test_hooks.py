"""Offline hook protocol/config tests. Never install into a user's home."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner
from openvegas.emotes import hooks as h
from openvegas.emotes.controller import EmoteController, State
from openvegas.emotes.events import Phase
from openvegas.emotes.resources import PackRepository
from openvegas.emotes.spool import EventSpool, _locked


@pytest.fixture
def settings(tmp_path, monkeypatch):
    # Only fake version/probe here. No global installation or Claude model calls.
    monkeypatch.setattr(h, "claude_version", lambda: (2, 1, 270))
    monkeypatch.setattr(h, "_probe_handler", lambda: True)
    parent = tmp_path / "project with spaces" / ".claude"
    parent.mkdir(parents=True, mode=0o700)
    return parent / "settings.local.json"


def write(path, content):
    path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
    path.chmod(0o600)


def install(settings):
    result = h.setup(settings, apply=True, activity_only=True)
    root = Path(result["installation"])
    return root, h._receipt(root)


def payload(event, prompt, *, session="native-session", **extra):
    value = {"hook_event_name": event, "session_id": session, "prompt_id": prompt, **extra}
    return json.dumps(value).encode()


def events(spool, receipt, session="native-session"):
    identity = "claude-" + h._digest((receipt["id"] + ":" + session).encode())[:32]
    return spool.drain(source="claude", session_id=identity)


def test_setup_dry_run_is_read_only_and_does_not_print_original_secrets(settings):
    original = {"env": {"SECRET": "never-display-this"}, "theme": "dark"}
    write(settings, original)
    before = settings.stat()
    result = h.setup(settings)
    assert result["applied"] is False
    assert result["schema_reviewed"] is True
    assert "never-display-this" not in json.dumps(result)
    assert not h._root(settings).exists()
    assert settings.stat().st_mtime_ns == before.st_mtime_ns
    assert json.loads(settings.read_bytes()) == original


def test_missing_settings_dry_run_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "claude_version", lambda: (2, 1, 66))
    path = tmp_path / "missing" / "settings.json"
    result = h.setup(path)
    assert result["schema_reviewed"] is False
    assert not path.parent.exists()


@pytest.mark.parametrize("version", [None, (2, 1, 66), (2, 1, 195), (2, 1, 271), (3, 0, 0)])
def test_unverified_versions_cannot_apply(settings, monkeypatch, version):
    monkeypatch.setattr(h, "claude_version", lambda: version)
    with pytest.raises(h.HookError, match="2.1.196"):
        h.setup(settings, apply=True, activity_only=True)
    assert not settings.exists() and not h._root(settings).exists()


def test_apply_requires_acknowledgement_and_installed_module(settings, monkeypatch):
    with pytest.raises(h.HookError, match="activity-only"):
        h.setup(settings, apply=True)
    monkeypatch.setattr(h, "_probe_handler", lambda: False)
    with pytest.raises(h.HookError, match="isolated hook module"):
        h.setup(settings, apply=True, activity_only=True)
    assert not settings.exists()


@pytest.mark.parametrize("policy", ["disableAllHooks", "allowManagedHooksOnly"])
def test_never_disables_host_policy(settings, policy):
    write(settings, {policy: True})
    with pytest.raises(h.HookError, match="policy"):
        h.setup(settings, apply=True, activity_only=True)
    assert json.loads(settings.read_bytes()) == {policy: True}


def test_merge_is_idempotent_and_exact_backup_restores_original_bytes(settings):
    original = b'{ "theme":"dark", "hooks":{"Stop":[{"matcher":"", "hooks":[{"type":"command","command":"my-existing-hook"}]}]} }\n'
    write(settings, original)
    root, receipt = install(settings)
    config = json.loads(settings.read_bytes())
    assert config["hooks"]["Stop"][0]["hooks"][0]["command"] == "my-existing-hook"
    assert config["theme"] == "dark"
    assert (root / receipt["backup"]).read_bytes() == original
    assert (root.stat().st_mode & 0o777) == 0o700
    assert ((root / receipt["backup"]).stat().st_mode & 0o777) == 0o600
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir() if p.is_file()}
    assert h.setup(settings, apply=True, activity_only=True)["changed"] is False
    assert h.uninstall(settings)["applied"] is False
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before
    h.uninstall(settings, apply=True)
    assert settings.read_bytes() == original
    assert h._receipt(root)["enabled"] is False
    assert h.uninstall(settings, apply=True)["changed"] is False


def test_uninstall_preserves_new_user_settings_and_hooks_even_in_owned_group(settings):
    write(settings, {"theme": "light"})
    root, receipt = install(settings)
    config = json.loads(settings.read_bytes())
    foreign = {"type": "command", "command": "preserve me"}
    config["hooks"]["Stop"][0]["hooks"].append(foreign)
    config["hooks"]["Notification"] = [{"matcher": "idle_prompt", "hooks": [foreign]}]
    config["theme"] = "dark"
    config["permissions"] = {"deny": ["Bash(rm *)"]}
    write(settings, config)
    before = settings.read_bytes()
    h.uninstall(settings, apply=True)
    after = json.loads(settings.read_bytes())
    assert after == {"theme": "dark", "permissions": config["permissions"], "hooks": {
        "Stop": [{"hooks": [foreign]}], "Notification": config["hooks"]["Notification"]}}
    assert (root / (receipt["id"] + ".uninstall-backup")).read_bytes() == before


def test_uninstall_removes_new_file_only_if_unchanged(settings):
    install(settings)
    h.uninstall(settings, apply=True)
    assert not settings.exists()
    install(settings)
    config = json.loads(settings.read_bytes())
    config["theme"] = "dark"
    write(settings, config)
    h.uninstall(settings, apply=True)
    assert json.loads(settings.read_bytes()) == {"theme": "dark"}


@pytest.mark.parametrize("mutation", ["command", "duplicate", "matcher"])
def test_modified_owned_entries_are_not_clobbered(settings, mutation):
    install(settings)
    config = json.loads(settings.read_bytes())
    group = config["hooks"]["Stop"][0]
    if mutation == "command":
        group["hooks"][0]["command"] += ".edited"
    elif mutation == "duplicate":
        config["hooks"]["Stop"].append(copy.deepcopy(group))
    else:
        group["matcher"] = "user-modified"
    write(settings, config)
    before = settings.read_bytes()
    with pytest.raises(h.HookError):
        h.uninstall(settings, apply=True)
    assert settings.read_bytes() == before


def test_deleted_owned_hook_does_not_prevent_conservative_uninstall(settings):
    install(settings)
    config = json.loads(settings.read_bytes())
    config["hooks"].pop("Stop")
    config["foo"] = [1, 2, 3]
    write(settings, config)
    h.uninstall(settings, apply=True)
    assert json.loads(settings.read_bytes()) == {"foo": [1, 2, 3]}


@pytest.mark.parametrize("data", [b"{", b"[]", b'{"hooks":[],"hooks":{}}', b'{"hooks":{"Stop":{}}}',
                                 b'{"hooks":{"Stop":[{"hooks":["bad"]}]}}', b" " * (h.MAX_SETTINGS + 1)])
def test_invalid_or_oversized_settings_untouched(settings, data):
    write(settings, data)
    with pytest.raises(h.HookError):
        h.setup(settings, apply=True, activity_only=True)
    assert settings.read_bytes() == data


def test_symlink_fifo_hardlink_and_writable_settings_rejected(settings, tmp_path):
    target = tmp_path / "other"
    write(target, b"{}")
    settings.symlink_to(target)
    with pytest.raises(h.HookError):
        h.setup(settings)
    settings.unlink()
    os.mkfifo(settings, 0o600)
    with pytest.raises(h.HookError):
        h.setup(settings)
    settings.unlink()
    os.link(target, settings)
    with pytest.raises(h.HookError):
        h.setup(settings)
    settings.unlink()
    write(settings, b"{}")
    settings.chmod(0o666)
    with pytest.raises(h.HookError):
        h.setup(settings)


def test_concurrent_config_change_is_detected(settings):
    write(settings, b'{"new":"value"}')
    with pytest.raises(h.HookError, match="changed during"):
        h._replace_settings(settings, b"{}", b'{"wrong":"overwrite"}')
    assert json.loads(settings.read_bytes()) == {"new": "value"}


def test_owned_command_uses_same_interpreter_exec_args_not_payload_shell(settings, monkeypatch):
    python = "/tmp/venv with spaces/it's-$(touch BAD)/bin/python"
    monkeypatch.setattr(sys, "executable", python)
    result = h.setup(settings)
    for group in result["hook_patch"].values():
        handler = group["hooks"][0]
        assert handler["command"] == python
        assert handler["args"][:4] == ["-I", "-m", "openvegas.emotes.hook_dispatch", "handle"]
        assert handler["timeout"] == 2 and "async" not in handler
        assert not any("${" in arg for arg in handler["args"])


def test_frozen_build_requires_successful_early_dispatch_probe(settings, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    result = h.setup(settings)
    assert result["hook_patch"]["Stop"]["hooks"][0]["args"][0] == "--openvegas-emote-hook"
    monkeypatch.setattr(h, "_probe_handler", lambda: False)
    with pytest.raises(h.HookError, match="not available"):
        h.setup(settings, apply=True, activity_only=True)
    assert not settings.exists()


def test_success_never_inferred_and_quiet_is_latched(settings, tmp_path):
    root, receipt = install(settings)
    spool = EventSpool(tmp_path / "spool")
    prompt = str(uuid4())
    def send(name, **extra):
        return h.handle_input(payload(name, prompt, **extra), installation=root, owner=receipt["id"], spool=spool)
    # Content is intentionally unexamined; no prompt text, cwd, or paths persisted.
    assert send("UserPromptSubmit", transcript_path="/never/read/me", secret="not-logged")
    assert send("PreToolUse", tool_use_id="tool-1", tool_input={"command": "secret"})
    assert not send("PreToolUse", tool_use_id="tool-1")
    assert send("PermissionRequest")
    assert not send("PreToolUse", tool_use_id="tool-2")
    assert not send("Stop", stop_hook_active=False, last_assistant_message="Success!")
    rows = events(spool, receipt)
    assert [e.phase for e in rows] == [Phase.START, Phase.BUSY, Phase.PAUSE]
    assert rows[0].turn_id == prompt and all(e.generation == rows[0].generation for e in rows)
    assert [e.sequence for e in rows] == sorted(e.sequence for e in rows)
    for file in root.glob("claude-*.json"):
        assert not any(x in file.read_text() for x in ("SECRET", "not-logged", "Success!", "/never", "tool-1"))


@pytest.mark.parametrize("terminal,phase", [("Stop", Phase.PAUSE), ("StopFailure", Phase.ERROR),
                                            ("PostToolUseFailure", Phase.PAUSE), ("SessionEnd", Phase.EXIT)])
def test_terminal_events_are_quiet_never_completion(settings, tmp_path, terminal, phase):
    root, receipt = install(settings)
    spool = EventSpool(tmp_path / "events")
    prompt = str(uuid4())
    for name in ("UserPromptSubmit", terminal, "PreToolUse", "Stop", "UserPromptSubmit"):
        h.handle_input(payload(name, prompt, tool_use_id="tool-1", stop_hook_active=False,
                               authoritative_success=True, outcome="success"),
                       installation=root, owner=receipt["id"], spool=spool)
    rows = events(spool, receipt)
    assert [e.phase for e in rows] == [Phase.START, phase]
    assert all(e.outcome is None for e in rows)


def test_old_turn_events_and_duplicate_starts_cannot_affect_new_turn(settings, tmp_path):
    root, receipt = install(settings)
    spool = EventSpool(tmp_path / "events")
    first, second = str(uuid4()), str(uuid4())
    for name, prompt in [("UserPromptSubmit", first), ("UserPromptSubmit", second),
                         ("StopFailure", first), ("Stop", first), ("UserPromptSubmit", first),
                         ("Stop", second), ("PreToolUse", second)]:
        h.handle_input(payload(name, prompt, tool_use_id="tool"), installation=root, owner=receipt["id"], spool=spool)
    rows = events(spool, receipt)
    assert [(e.turn_id, e.phase) for e in rows] == [
        (first, Phase.START), (first, Phase.CANCEL), (second, Phase.START), (second, Phase.PAUSE)]
    assert rows[2].generation > rows[0].generation


def test_early_terminal_tombstones_delayed_start(settings, tmp_path):
    root, receipt = install(settings)
    spool = EventSpool(tmp_path / "events")
    prompt = str(uuid4())
    for name in ("Stop", "UserPromptSubmit", "PreToolUse"):
        assert not h.handle_input(payload(name, prompt, tool_use_id="tool"),
                                  installation=root, owner=receipt["id"], spool=spool)
    assert events(spool, receipt) == []


@pytest.mark.parametrize("data", [b"not json", b"[]", b"[" * 10000, b'{"x":NaN}',
                                  b'{"hook_event_name":"Stop","hook_event_name":"UserPromptSubmit"}',
                                  b" " * (h.MAX_INPUT + 1), b"\xff"])
def test_malformed_input_is_silent_and_no_side_effects(settings, tmp_path, capsys, data):
    root, receipt = install(settings)
    spool = EventSpool(tmp_path / "events")
    before = set(root.iterdir())
    assert not h.handle_input(data, installation=root, owner=receipt["id"], spool=spool)
    assert set(root.iterdir()) == before
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("extra", [{"agent_id": "subagent"}, {"agent_id": None},
                                   {"session_id": "../bad"}, {"prompt_id": None},
                                   {"prompt_id": "not-a-uuid"}])
def test_no_foreign_subagent_or_missing_prompt_identity(settings, tmp_path, extra):
    root, receipt = install(settings)
    value = {"session_id": "native-session", "hook_event_name": "UserPromptSubmit",
             "prompt_id": str(uuid4()), **extra}
    assert not h.handle_input(json.dumps(value).encode(), installation=root, owner=receipt["id"],
                              spool=EventSpool(tmp_path / "events"))


def test_owner_and_disabled_installation_gate_stale_handlers(settings, tmp_path):
    root, receipt = install(settings)
    spool = EventSpool(tmp_path / "events")
    data = payload("UserPromptSubmit", str(uuid4()))
    assert not h.handle_input(data, installation=root, owner=uuid4().hex, spool=spool)
    h.uninstall(settings, apply=True)
    assert not h.handle_input(data, installation=root, owner=receipt["id"], spool=spool)
    assert events(spool, receipt) == []


def test_lock_contention_and_corrupt_state_fail_closed(settings, tmp_path):
    root, receipt = install(settings)
    spool = EventSpool(tmp_path / "events")
    prompt = str(uuid4())
    data = payload("UserPromptSubmit", prompt)
    with _locked(root):
        assert not h.handle_input(data, installation=root, owner=receipt["id"], spool=spool)
    assert h.handle_input(data, installation=root, owner=receipt["id"], spool=spool)
    state = next(root.glob("claude-*.json"))
    write(state, b'{"corrupt":true}')
    assert not h.handle_input(payload("UserPromptSubmit", str(uuid4())), installation=root,
                              owner=receipt["id"], spool=spool)
    assert state.read_bytes() == b'{"corrupt":true}'


def test_limits_fail_closed_without_evicting_tombstones(settings, tmp_path, monkeypatch):
    root, receipt = install(settings)
    spool = EventSpool(tmp_path / "events")
    monkeypatch.setattr(h, "MAX_TURNS", 2)
    for _ in range(2):
        assert h.handle_input(payload("UserPromptSubmit", str(uuid4())), installation=root,
                              owner=receipt["id"], spool=spool)
    assert not h.handle_input(payload("UserPromptSubmit", str(uuid4())), installation=root,
                              owner=receipt["id"], spool=spool)
    monkeypatch.setattr(h, "MAX_SESSIONS", 1)
    assert not h.handle_input(payload("UserPromptSubmit", str(uuid4()), session="other"),
                              installation=root, owner=receipt["id"], spool=spool)


def test_separate_sessions_do_not_mix(settings, tmp_path):
    root, receipt = install(settings)
    spool = EventSpool(tmp_path / "events")
    for session in ("one", "two"):
        h.handle_input(payload("UserPromptSubmit", str(uuid4()), session=session),
                       installation=root, owner=receipt["id"], spool=spool)
    one, two = events(spool, receipt, "one"), events(spool, receipt, "two")
    assert len(one) == len(two) == 1
    assert one[0].session_id != two[0].session_id


def test_controller_stops_on_error_and_expires_unobserved_cancel_neutrally(settings, tmp_path):
    root, receipt = install(settings)
    spool = EventSpool(tmp_path / "events")
    h.handle_input(payload("UserPromptSubmit", str(uuid4())), installation=root, owner=receipt["id"], spool=spool)
    start = events(spool, receipt)[0]
    now = [0.0]
    pack = PackRepository().load("pixel-courier")
    controller = EmoteController(pack, source="claude", session_id=start.session_id, clock=lambda: now[0])
    assert controller.handle(start)
    assert controller.current_state == State.ACTIVE
    now[0] = 21
    assert controller.current_state == State.IDLE
    assert controller.reason == "active_lease_expired"


def test_bounded_stdin_eof_and_timeout(tmp_path):
    r, w = os.pipe()
    try:
        os.write(w, b'{"hello":true}')
        os.close(w)
        w = None
        assert h.read_input(r) == b'{"hello":true}'
    finally:
        os.close(r)
        if w is not None:
            os.close(w)
    r, w = os.pipe()
    try:
        start = time.monotonic()
        with pytest.raises(h.HookError, match="timed out"):
            h.read_input(r, timeout=0.01)
        assert time.monotonic() - start < 1.0
    finally:
        os.close(r)
        os.close(w)
    data = tmp_path / "large.json"
    write(data, b" " * (h.MAX_INPUT + 1))
    with data.open("rb") as file, pytest.raises(h.HookError, match="too large"):
        h.read_input(file.fileno())


def test_original_backup_integrity_failure_leaves_current_settings(settings):
    write(settings, b'{"theme":"light"}')
    root, receipt = install(settings)
    before = settings.read_bytes()
    write(root / receipt["backup"], b'{"tampered":"backup"}')
    with pytest.raises(h.HookError, match="integrity"):
        h.uninstall(settings, apply=True)
    assert settings.read_bytes() == before


def test_prepared_receipt_can_be_uninstalled_after_interrupted_setup(settings, monkeypatch):
    write(settings, b'{"theme":"light"}')
    original = settings.read_bytes()
    real_replace = h._replace_settings
    def fail(*args):
        raise h.HookError("simulated interrupted install")
    monkeypatch.setattr(h, "_replace_settings", fail)
    with pytest.raises(h.HookError, match="interrupted"):
        install(settings)
    assert settings.read_bytes() == original
    monkeypatch.setattr(h, "_replace_settings", real_replace)
    h.uninstall(settings, apply=True)
    assert json.loads(settings.read_bytes()) == {"theme": "light"}
    assert h._receipt(h._root(settings))["enabled"] is False


def test_corrupt_generation_never_resets_ordering(settings, tmp_path):
    root, receipt = install(settings)
    spool = EventSpool(tmp_path / "events")
    generation_dir = tmp_path / "run-generations"
    generation_dir.mkdir(mode=0o700)
    write(generation_dir / "global-v1.json", b'{"generation":-1}')
    assert not h.handle_input(payload("UserPromptSubmit", str(uuid4())), installation=root,
                              owner=receipt["id"], spool=spool)
    assert not list(root.glob("claude-*.json"))


def test_dropped_start_does_not_retry_or_synthesize_completion(settings, tmp_path, monkeypatch):
    root, receipt = install(settings)
    spool = EventSpool(tmp_path / "events")
    prompt = str(uuid4())
    seen = []
    monkeypatch.setattr(spool, "publish", lambda event: seen.append(event) and False)
    assert not h.handle_input(payload("UserPromptSubmit", prompt), installation=root,
                              owner=receipt["id"], spool=spool)
    assert not h.handle_input(payload("UserPromptSubmit", prompt), installation=root,
                              owner=receipt["id"], spool=spool)
    assert not h.handle_input(payload("Stop", prompt, authoritative_success=True), installation=root,
                              owner=receipt["id"], spool=spool)
    assert [event.phase for event in seen] == [Phase.START, Phase.PAUSE]


def test_reinstall_namespaces_are_distinct_and_old_handler_stays_disabled(settings, tmp_path):
    old_root, old = install(settings)
    h.uninstall(settings, apply=True)
    root, new = install(settings)
    assert root == old_root and new["id"] != old["id"]
    spool = EventSpool(tmp_path / "events")
    data = payload("UserPromptSubmit", str(uuid4()))
    assert not h.handle_input(data, installation=root, owner=old["id"], spool=spool)
    assert h.handle_input(data, installation=root, owner=new["id"], spool=spool)


def test_private_receipt_and_state_symlinks_fail_closed(settings, tmp_path):
    root, receipt = install(settings)
    target = tmp_path / "arbitrary.json"
    write(target, b"{}")
    spool = EventSpool(tmp_path / "events")
    assert h.handle_input(payload("UserPromptSubmit", str(uuid4())), installation=root,
                          owner=receipt["id"], spool=spool)
    state = next(root.glob("claude-*.json"))
    state.unlink()
    state.symlink_to(target)
    assert not h.handle_input(payload("UserPromptSubmit", str(uuid4())), installation=root,
                              owner=receipt["id"], spool=spool)
    assert target.read_bytes() == b"{}"


@pytest.mark.parametrize("provider", ["codex"])
def test_cli_rejects_unverified_providers_without_writing(settings, provider):
    result = CliRunner().invoke(h.hooks, ["setup", provider, "--apply", "--settings", str(settings)])
    assert result.exit_code != 0
    assert "unsupported" in result.output
    assert not settings.exists()


def test_click_group_exports_plan_status_sessions_and_silent_handler(settings):
    runner = CliRunner()
    result = runner.invoke(h.hooks, ["setup", "claude", "--settings", str(settings)])
    assert result.exit_code == 0 and json.loads(result.output)["applied"] is False
    root, receipt = install(settings)
    status = runner.invoke(h.hooks, ["status", "--settings", str(settings)])
    assert json.loads(status.output)["lifecycle_certified"] is False
    result = runner.invoke(h.hooks, ["handle", "--installation", str(root), "--owner", receipt["id"]], input="not json")
    assert result.exit_code == 0 and result.output == ""
    result = runner.invoke(h.hooks, ["sessions", "--settings", str(settings)])
    assert result.exit_code == 0 and json.loads(result.output)["sessions"] == []


def test_version_probe_is_only_no_shell_version_command(monkeypatch):
    seen = []
    monkeypatch.setattr(h.shutil, "which", lambda _: "/tmp/a path/claude")
    def run(args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b"2.1.270 (Claude Code)\n", b"")
    monkeypatch.setattr(h.subprocess, "run", run)
    assert h.claude_version() == (2, 1, 270)
    assert seen[0][0] == ["/tmp/a path/claude", "--version"]
    assert not seen[0][1].get("shell") and seen[0][1]["timeout"] == 3


def test_generated_exec_invocation_runs_isolated_without_importing_cli(settings, tmp_path, monkeypatch):
    runtime = tmp_path / "isolated runtime"
    venv.EnvBuilder(with_pip=False).create(runtime)
    python = runtime / "bin" / "python"
    site = runtime / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    package = site / "openvegas" / "emotes"
    package.mkdir(parents=True)
    source = Path(sys.modules[EmoteController.__module__].__file__).parent
    for module in source.glob("*.py"):
        shutil.copyfile(module, package / module.name)
    shutil.copyfile(h.__file__, package / "hooks.py")
    shutil.copyfile(Path(h.__file__).with_name("hook_dispatch.py"), package / "hook_dispatch.py")
    (site / "openvegas" / "__init__.py").write_text("")
    # Only dependencies are borrowed from this test interpreter, never a HOME
    # install or PYTHONPATH. Our fixture package wins normal site-package lookup.
    dependencies = [p for p in sys.path if p.endswith("site-packages")]
    (site / "test-dependencies.pth").write_text("\n".join(dependencies) + "\n")
    home = tmp_path / "isolated-home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.defpath, "LANG": "C.UTF-8",
           "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    # A hostile cwd module cannot shadow the installed hook module with -I.
    cwd = tmp_path / "hostile-cwd"
    (cwd / "openvegas").mkdir(parents=True)
    (cwd / "openvegas" / "__init__.py").write_text("raise RuntimeError('cwd shadow imported')")
    probe = subprocess.run([str(python), "-I", "-m", "openvegas.emotes.hooks", "probe"],
                           cwd=cwd, env=env, capture_output=True, timeout=5, check=False)
    assert (probe.returncode, probe.stdout, probe.stderr) == (0, b"openvegas-hooks-v1\n", b"")
    monkeypatch.setattr(sys, "executable", str(python))
    root, receipt = install(settings)
    handler = receipt["entries"]["UserPromptSubmit"]["hooks"][0]
    prompt = str(uuid4())
    for data in [b"not-json", payload("UserPromptSubmit", prompt, prompt_text="secret is transient")]:
        result = subprocess.run([handler["command"], *handler["args"]], cwd=cwd, env=env,
                                input=data, capture_output=True, timeout=5, check=False)
        assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    assert len(list(root.glob("claude-*.json"))) == 1
    assert "secret is transient" not in next(root.glob("claude-*.json")).read_text()
    broken_args = subprocess.run([str(python), "-I", "-m", "openvegas.emotes.hooks", "handle", "--bad"],
                                 cwd=cwd, env=env, capture_output=True, timeout=5, check=False)
    assert (broken_args.returncode, broken_args.stdout, broken_args.stderr) == (0, b"", b"")
