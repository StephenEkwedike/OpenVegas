"""Exercise the npm launcher with fake executables; never invoke pipx/Python CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "npm-cli/bin/openvegas.js"
NODE = shutil.which("node")
FAKE_EXECUTABLE = r'''
import json
import os
from pathlib import Path
import signal
import sys
import time

name = Path(sys.argv[0]).name
arguments = sys.argv[1:]
mode = "probe" if arguments[:2] == ["-B", "-c"] else "run"
with open(os.environ["INVOCATION_LOG"], "a", encoding="utf-8") as output:
    output.write(json.dumps({"name": name, "mode": mode, "args": arguments}) + "\n")
behavior = json.loads(os.environ["FAKE_BEHAVIOR"]).get(name, {})
if mode == "probe":
    assert "find_spec" in arguments[-1]
    assert "import openvegas" not in arguments[-1]
    time.sleep(behavior.get("probe_delay", 0))
    if behavior.get("remove_after_probe"):
        Path(sys.argv[0]).unlink()
    raise SystemExit(behavior.get("probe", 3))
if behavior.get("signal"):
    os.kill(os.getpid(), getattr(signal, behavior["signal"]))
raise SystemExit(behavior.get("run", 0))
'''


@pytest.fixture
def launch(tmp_path):
    if not NODE:
        pytest.skip("Node.js is required to execute npm launcher contracts")
    if os.name != "posix":
        pytest.skip("Controlled executable fixtures require POSIX shebang support")
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    log = tmp_path / "invocations.jsonl"

    def invoke(behavior, arguments=("ask", "synthetic prompt"), preferred=None):
        for name in behavior:
            executable = binary_dir / name
            executable.write_text(f"#!{sys.executable}\n" + FAKE_EXECUTABLE)
            executable.chmod(0o700)
        env = {
            "PATH": str(binary_dir), "HOME": str(tmp_path),
            "INVOCATION_LOG": str(log), "FAKE_BEHAVIOR": json.dumps(behavior),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if preferred is not None:
            env["OPENVEGAS_PYTHON"] = preferred
        result = subprocess.run(
            [NODE, str(LAUNCHER), *arguments], cwd=tmp_path, env=env,
            capture_output=True, text=True, timeout=10, check=False,
        )
        events = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, events

    return invoke


@pytest.mark.parametrize("status", [0, 2, 3, 27])
def test_available_runtime_executes_command_once_and_preserves_status(launch, status):
    arguments = ("ask", "argument with spaces", "--model", "synthetic-model")
    result, events = launch({"python3": {"probe": 0, "run": status}, "python": {"probe": 0}, "pipx": {}}, arguments)
    assert result.returncode == status
    assert [(event["name"], event["mode"]) for event in events] == [("python3", "probe"), ("python3", "run")]
    assert events[-1]["args"] == ["-m", "openvegas.cli", *arguments]


def test_missing_module_can_fall_back_before_command_execution(launch):
    result, events = launch({"python3": {"probe": 3}, "python": {"probe": 0, "run": 19}, "pipx": {}})
    assert result.returncode == 19
    assert [(event["name"], event["mode"]) for event in events] == [("python3", "probe"), ("python", "probe"), ("python", "run")]


def test_preferred_python_failure_never_replays_on_default_python(launch):
    result, events = launch({"chosen-python": {"probe": 0, "run": 23}, "python3": {"probe": 0}, "pipx": {}}, preferred="chosen-python")
    assert result.returncode == 23
    assert [(event["name"], event["mode"]) for event in events] == [("chosen-python", "probe"), ("chosen-python", "run")]


def test_missing_preferred_executable_can_fall_back(launch):
    result, events = launch({"python3": {"probe": 0}}, preferred="nonexistent-python")
    assert result.returncode == 0
    assert [(event["name"], event["mode"]) for event in events] == [("python3", "probe"), ("python3", "run")]


def test_unexpected_probe_failure_stops_without_running_command(launch):
    result, events = launch({"python3": {"probe": 9}, "python": {"probe": 0}, "pipx": {}})
    assert result.returncode == 9
    assert [(event["name"], event["mode"]) for event in events] == [("python3", "probe")]


def test_probe_timeout_stops_without_running_or_falling_back(launch):
    result, events = launch({"python3": {"probe": 0, "probe_delay": 6}, "python": {"probe": 0}, "pipx": {}})
    assert result.returncode == 1
    assert [(event["name"], event["mode"]) for event in events] == [("python3", "probe")]


def test_command_arguments_are_not_interpreted_as_probe_options(launch):
    arguments = ("ask", "-c", "synthetic prompt")
    result, events = launch({"python3": {"probe": 0, "run": 2}, "python": {"probe": 0}}, arguments)
    assert result.returncode == 2
    assert [(event["name"], event["mode"]) for event in events] == [("python3", "probe"), ("python3", "run")]
    assert events[-1]["args"] == ["-m", "openvegas.cli", *arguments]


def test_pipx_fallback_only_after_missing_runtimes_and_preserves_exit(launch):
    result, events = launch({"python3": {"probe": 3}, "python": {"probe": 3}, "pipx": {"run": 41}})
    assert result.returncode == 41
    assert [(event["name"], event["mode"]) for event in events] == [("python3", "probe"), ("python", "probe"), ("pipx", "run")]
    assert events[-1]["args"][:4] == ["run", "--spec", "openvegas[audio]", "openvegas"]


def test_absent_interpreters_can_use_pipx_once(launch):
    result, events = launch({"pipx": {"run": 17}})
    assert result.returncode == 17
    assert [(event["name"], event["mode"]) for event in events] == [("pipx", "run")]


def test_missing_all_runtimes_reports_install_guidance_without_execution(launch):
    result, events = launch({})
    assert result.returncode == 1
    assert events == []
    assert "runtime not found" in result.stderr


def test_duplicate_preferred_candidate_is_not_probed_twice(launch):
    result, events = launch({"python3": {"probe": 3}, "python": {"probe": 3}, "pipx": {}}, preferred="python3")
    assert result.returncode == 0
    assert [(event["name"], event["mode"]) for event in events] == [("python3", "probe"), ("python", "probe"), ("pipx", "run")]


def test_runtime_disappearing_after_probe_does_not_trigger_replay(launch):
    result, events = launch({"python3": {"probe": 0, "remove_after_probe": True}, "python": {"probe": 0}, "pipx": {}})
    assert result.returncode == 127
    assert [(event["name"], event["mode"]) for event in events] == [("python3", "probe")]


def test_signaled_command_does_not_trigger_replay(launch):
    result, events = launch({"python3": {"probe": 0, "signal": "SIGTERM"}, "python": {"probe": 0}, "pipx": {}})
    assert result.returncode == 143
    assert [(event["name"], event["mode"]) for event in events] == [("python3", "probe"), ("python3", "run")]
