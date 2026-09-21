"""Passive Gemini fixtures: no agent sessions or user's hook configuration."""

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from openvegas.emotes import hooks as h


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "gemini_version", lambda: (0, 59, 0))
    monkeypatch.setattr(h, "_probe_handler", lambda: True)
    path = tmp_path / "project 'with spaces'" / ".gemini" / "settings.json"
    path.parent.mkdir(parents=True, mode=0o700)
    return path


def write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def install(settings):
    result = h.setup(settings, provider="gemini", apply=True, observation_only=True)
    root = Path(result["installation"])
    return root, h._receipt(root)


def send(root, receipt, name, **extra):
    data = json.dumps({"session_id": "session-one", "hook_event_name": name,
                       "timestamp": "2026-09-14T06:00:00Z", **extra}).encode()
    return h.handle_input(data, installation=root, owner=receipt["id"])


def test_plan_only_no_probe_or_changes(settings, monkeypatch):
    monkeypatch.setattr(h, "_probe_handler", lambda: pytest.fail("dry-run probe"))
    result = h.setup(settings, provider="gemini")
    assert result["mode"] == "observation-only" and result["schema_reviewed"]
    assert not result["applied"] and not settings.exists()
    assert not h._root(settings).exists()
    assert set(result["hook_patch"]) == set(h.GEMINI_EVENTS)
    for group in result["hook_patch"].values():
        handler = group["hooks"][0]
        assert handler["timeout"] == 2000
        assert "args" not in handler and "async" not in handler
        command = shlex.split(handler["command"])
        assert command[:4] == [sys.executable, "-I", "-m", "openvegas.emotes.hook_dispatch"]


@pytest.mark.parametrize("version", [None, (0, 58, 0), (0, 59, 1), (0, 60, 0), (1, 0, 0)])
def test_unknown_versions_cannot_install(settings, monkeypatch, version):
    monkeypatch.setattr(h, "gemini_version", lambda: version)
    with pytest.raises(h.HookError, match="0.59.0"):
        install(settings)
    assert not settings.exists()


def test_observation_opt_in_is_not_activity_opt_in(settings):
    with pytest.raises(h.HookError, match="observation-only"):
        h.setup(settings, provider="gemini", apply=True, activity_only=True)
    assert not settings.exists()


def test_merge_backup_uninstall_preserves_policies_and_foreign_hooks(settings):
    original = {"hooks": {"enabled": True, "notifications": False, "disabled": ["foreign-disabled"],
                          "AfterAgent": [{"hooks": [{"type": "command", "command": "my-check"}]}]},
                "model": {"name": "user-choice"}}
    write(settings, original)
    before = settings.read_bytes()
    root, receipt = install(settings)
    assert (root / receipt["backup"]).read_bytes() == before
    assert h.setup(settings, provider="gemini")["changed"] is False
    config = json.loads(settings.read_bytes())
    config["new-user-setting"] = "keep"
    write(settings, config)
    assert not h.uninstall(settings)["applied"]
    h.uninstall(settings, apply=True)
    assert json.loads(settings.read_bytes()) == {**original, "new-user-setting": "keep"}
    assert not send(root, receipt, "BeforeAgent")


@pytest.mark.parametrize("container", ["hooks", "hooksConfig"])
@pytest.mark.parametrize("policy", [{"enabled": False}, {"enabled": "yes"}, {"disabled": True},
                                   {"notifications": "yes"}])
def test_disabled_or_invalid_policy_is_not_overridden(settings, policy, container):
    write(settings, {container: policy})
    before = settings.read_bytes()
    with pytest.raises(h.HookError):
        install(settings)
    assert settings.read_bytes() == before


def test_native_context_placeholders_in_paths_fail_closed(settings, monkeypatch):
    monkeypatch.setattr(sys, "executable", "/tmp/$GEMINI_SESSION_ID/bin/python")
    with pytest.raises(h.HookError, match="placeholders"):
        h.setup(settings, provider="gemini")
    assert not settings.exists()


def test_status_distinguishes_owned_disabled_hook(settings):
    _, receipt = install(settings)
    config = json.loads(settings.read_bytes())
    config["hooksConfig"] = {"disabled": ["openvegas-" + receipt["id"]]}
    write(settings, config)
    result = CliRunner().invoke(h.hooks, ["status", "--settings", str(settings)])
    assert json.loads(result.output)["policy_disabled"]


def test_rapid_turns_approvals_retries_cancel_never_publish(settings, monkeypatch):
    root, receipt = install(settings)
    monkeypatch.setattr(h, "EventSpool", lambda: pytest.fail("Gemini must not open IPC"))
    monkeypatch.setattr(h, "reserve_generation", lambda *a, **k: pytest.fail("invented turn"))
    for _ in range(20):
        for name in ("BeforeAgent", "Notification", "AfterAgent"):
            send(root, receipt, name, notification_type="ToolPermission", stop_hook_active=True,
                 prompt="SECRET-PROMPT", prompt_response="SECRET-RESPONSE", token="SECRET-TOKEN",
                 outcome="success", status="completed", authoritative_success=True,
                 transcript_path="/forbidden/transcript", tool_response={"error": "secret error"})
    assert send(root, receipt, "SessionEnd", reason="exit")
    assert not send(root, receipt, "BeforeAgent")
    state_path = next(root.glob("gemini-*.json"))
    state = json.loads(state_path.read_bytes())
    assert set(state) == {"schema", "session_id", "observed", "closed"}
    assert state["observed"] == sorted(h.GEMINI_EVENTS) and state["closed"]
    assert "SECRET" not in state_path.read_text() and "timestamp" not in state


def test_early_exit_tombstones_delayed_start_and_state_corruption(settings):
    root, receipt = install(settings)
    assert send(root, receipt, "SessionEnd")
    assert not send(root, receipt, "BeforeAgent")
    file = next(root.glob("gemini-*.json"))
    write(file, {"corrupt": True})
    assert not send(root, receipt, "AfterAgent")
    assert json.loads(file.read_bytes()) == {"corrupt": True}


def test_unknown_subagent_events_and_session_limit(settings, monkeypatch):
    root, receipt = install(settings)
    assert not send(root, receipt, "BeforeAgent", agent_id="child")
    assert not send(root, receipt, "AfterModel")
    assert not send(root, receipt, "BeforeAgent", session_id="../bad")
    monkeypatch.setattr(h, "MAX_SESSIONS", 1)
    assert send(root, receipt, "BeforeAgent")
    assert not send(root, receipt, "BeforeAgent", session_id="two")


def test_cli_status_sessions_and_invalid_status(settings):
    runner = CliRunner()
    assert runner.invoke(h.hooks, ["setup", "gemini"]).exit_code != 0
    result = runner.invoke(h.hooks, ["setup", "gemini", "--settings", str(settings)])
    assert result.exit_code == 0 and not json.loads(result.output)["applied"]
    root, receipt = install(settings)
    send(root, receipt, "BeforeAgent")
    status = runner.invoke(h.hooks, ["status", "--settings", str(settings)])
    assert json.loads(status.output)["provider"] == "gemini"
    rows = json.loads(runner.invoke(h.hooks, ["sessions", "--settings", str(settings)]).output)["sessions"]
    assert len(rows) == 1 and "watch" not in rows[0]
    settings.write_text("{")
    result = runner.invoke(h.hooks, ["status", "--settings", str(settings)])
    assert result.exit_code != 0 and "Invalid JSON" in result.output


def test_module_version_detection_without_running_provider(tmp_path, monkeypatch):
    root = tmp_path / "node_modules/@google/gemini-cli"
    (root / "dist").mkdir(parents=True)
    (root / "dist/index.js").write_text("throw Error('must not execute');")
    write(root / "package.json", {"name": "@google/gemini-cli", "version": "0.59.0"})
    link = tmp_path / "gemini"
    link.symlink_to(root / "dist/index.js")
    monkeypatch.setattr(h.shutil, "which", lambda _: str(link))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("provider executed"))
    assert h.gemini_version() == (0, 59, 0)
    write(root / "package.json", {"name": "@google/gemini-cli", "version": "0.59.0-preview.1"})
    assert h.gemini_version() is None
