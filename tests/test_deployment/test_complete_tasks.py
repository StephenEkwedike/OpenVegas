"""Exercise the execution loop without paid model requests."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("complete_tasks", SCRIPTS / "complete_tasks.py")
runner = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPTS))
try:
    spec.loader.exec_module(runner)
finally:
    sys.path.pop(0)


@pytest.fixture
def project(tmp_path, monkeypatch):
    import task_completion as gate
    (tmp_path / "plan.md").write_text("# Plan\n- [ ] Deliver\n")
    manifest = tmp_path / "state.json"
    assert gate.main(["init", "--manifest", str(manifest), "--root", str(tmp_path),
                      "--checklist", "plan.md"]) == 0
    monkeypatch.setattr(runner, "progress_fingerprint", lambda root, path: path.read_bytes())
    return manifest


def response(argv, status):
    Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps({
        "status": status, "summary": "test fixture", "next_step": "next bounded action",
    }))


def test_preview_never_invokes_agent(project, monkeypatch):
    monkeypatch.setattr(runner, "run_agent", lambda *a: pytest.fail("preview executed"))
    assert runner.main(["--manifest", str(project)]) == 1


def block_requirement(project, *, valid=True):
    data = json.loads(project.read_text())
    task_id = next(iter(data["requirements"]))
    data["requirements"][task_id].update(
        status="blocked", reason="Operator acceptance deferred", owner="Operator",
        next_step="Provide native acceptance" if valid else "",
    )
    project.write_text(json.dumps(data))


def test_decision_is_read_only_and_requires_reconciliation(project, monkeypatch, capsys):
    before = project.read_bytes()
    capsys.readouterr()
    monkeypatch.setattr(runner, "run_agent", lambda *a: pytest.fail("decision executed"))
    assert runner.main(["--manifest", str(project), "--decision"]) == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["action"] == "run"
    assert len(decision["actionable_ids"]) == 1
    assert project.read_bytes() == before
    assert not project.with_name("state.json.runner.lock").exists()


def test_all_blocked_needs_no_agent_or_cli(project, monkeypatch, capsys):
    block_requirement(project)
    capsys.readouterr()
    monkeypatch.setattr(runner.shutil, "which", lambda *a: pytest.fail("looked for CLI"))
    monkeypatch.setattr(runner, "run_agent", lambda *a: pytest.fail("blocked executed"))
    assert runner.main(["--manifest", str(project), "--decision"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "blocked"
    assert runner.main(["--manifest", str(project), "--run"]) == 1
    assert not project.with_name("state.json.runner.lock").exists()


def test_incomplete_blocker_metadata_cannot_suppress_work(project, capsys):
    block_requirement(project, valid=False)
    capsys.readouterr()
    assert runner.main(["--manifest", str(project), "--decision"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "run"


def test_mixed_queue_still_launches(project, monkeypatch):
    import task_completion as gate
    block_requirement(project)
    with (project.parent / "plan.md").open("a") as f:
        f.write("- [ ] Independent local fix\n")
    calls = []
    monkeypatch.setattr(runner.shutil, "which", lambda *a: "/safe/codex")
    def agent(argv, prompt, timeout):
        calls.append(argv)
        response(argv, "blocked")
        return 0
    monkeypatch.setattr(runner, "run_agent", agent)
    assert runner.main(["--manifest", str(project), "--run", "--max-rounds", "1"]) == 1
    assert len(calls) == 1
    root, data = gate.load_manifest(project)
    assert gate.execution_queue(root, data)["actionable"]


def test_blanket_blocked_response_does_not_stop_first_cycle(project, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(runner.shutil, "which", lambda *a: "/safe/codex")
    def agent(argv, prompt, timeout):
        calls.append(argv)
        response(argv, "blocked")
        return 0
    monkeypatch.setattr(runner, "run_agent", agent)
    assert runner.main(["--manifest", str(project), "--run"]) == 1
    assert len(calls) == 2
    output = capsys.readouterr().out
    assert "rejected blanket blocker claim" in output
    assert "STALLED" in output


def test_decision_respects_existing_lock(project, capsys):
    lock = project.with_name("state.json.runner.lock")
    lock.write_text("interactive coordinator")
    capsys.readouterr()
    assert runner.main(["--manifest", str(project), "--decision"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "locked"
    assert lock.read_text() == "interactive coordinator"


def test_decision_and_run_are_mutually_exclusive(project):
    with pytest.raises(SystemExit):
        runner.main(["--manifest", str(project), "--decision", "--run"])


def test_actual_loop_advances_automatically_and_rejects_false_completion(project, monkeypatch):
    calls = []
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/safe/codex")
    def agent(argv, prompt, timeout):
        calls.append(argv)
        assert "Native manual terminal acceptance is deferred" in prompt
        response(argv, "complete")
        return 0
    monkeypatch.setattr(runner, "run_agent", agent)
    assert runner.main(["--manifest", str(project), "--run"]) == 1
    assert len(calls) == 2  # automatically advances; then stops a no-progress loop
    assert not project.with_name("state.json.runner.lock").exists()
    assert not list(project.parent.glob("ov-completion-*"))


def test_verified_completion_stops_loop(project, monkeypatch):
    import task_completion as gate
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/safe/codex")
    def agent(argv, prompt, timeout):
        root = project.parent
        (root / "plan.md").write_text("# Plan\n- [x] Deliver\n")
        (root / "proof.txt").write_text("fixture acceptance")
        task_id = gate.parse_checklist(root, "plan.md")[0]["id"]
        assert gate.main(["record", "--manifest", str(project), "--id", task_id,
                          "--status", "passed", "--evidence", "proof.txt",
                          "--summary", "fixture accepted"]) == 0
        response(argv, "progress")
        return 0
    monkeypatch.setattr(runner, "run_agent", agent)
    assert runner.main(["--manifest", str(project), "--run"]) == 0


def test_execution_failure_no_retry(project, monkeypatch):
    calls = []
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/safe/codex")
    monkeypatch.setattr(runner, "run_agent", lambda *a: calls.append(a) or 7)
    assert runner.main(["--manifest", str(project), "--run"]) == 2
    assert len(calls) == 1


def test_existing_lock_not_removed_or_bypassed(project, monkeypatch):
    lock = project.with_name("state.json.runner.lock")
    lock.write_text("other coordinator")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/safe/codex")
    monkeypatch.setattr(runner, "run_agent", lambda *a: pytest.fail("concurrent runner"))
    assert runner.main(["--manifest", str(project), "--run"]) == 2
    assert lock.read_text() == "other coordinator"


def test_command_uses_restricted_workspace_and_no_permission_bypass(tmp_path):
    argv = runner.command("codex", tmp_path, tmp_path / "schema", tmp_path / "result")
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "--ignore-user-config" in argv
    assert not any("bypass" in arg for arg in argv)


def test_source_progress_does_not_require_manifest_mutation(project, monkeypatch):
    calls = []
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/safe/codex")
    monkeypatch.setattr(runner, "progress_fingerprint", lambda *a: len(calls))
    def agent(argv, *args):
        calls.append(argv)
        response(argv, "progress")
        return 0
    monkeypatch.setattr(runner, "run_agent", agent)
    assert runner.main(["--manifest", str(project), "--run", "--max-rounds", "3"]) == 1
    assert len(calls) == 3


@pytest.mark.skipif(runner.os.name == "nt", reason="POSIX process group cleanup")
def test_timeout_kills_group_even_after_parent_exits(monkeypatch):
    import subprocess
    from types import SimpleNamespace
    calls = []
    def communicate(*args, **kwargs):
        raise subprocess.TimeoutExpired("fixture", 1)
    process = SimpleNamespace(pid=12345, communicate=communicate, wait=lambda **kw: 0)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run_agent(["fixture"], "prompt", 1)
    assert calls == [(12345, runner.signal.SIGTERM), (12345, runner.signal.SIGKILL)]
