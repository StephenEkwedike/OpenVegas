"""Exercise the npm launcher with fake executables; never invoke pipx/Python CLI."""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
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
    output.write(json.dumps({"name": name, "mode": mode, "args": arguments,
                             "tty": [os.isatty(fd) for fd in range(3)]}) + "\n")
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
time.sleep(behavior.get("run_delay", 0))
if behavior.get("ready"):
    assert os.environ["OPENVEGAS_LAUNCH_NOTIFY_FD"] == "3"
    # Include a fragmented readiness message, as pipes do not promise framing.
    os.write(3, b"rea")
    time.sleep(0.02)
    os.write(3, b"dy\n")
    assert os.read(3, 1) == b"1"
    os.close(3)
    print("Browser sign-in consent", flush=True)
    time.sleep(0.3)
if behavior.get("stderr"):
    print(behavior["stderr"], file=sys.stderr, flush=True)
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

    def invoke(behavior, arguments=("ask", "synthetic prompt"), preferred=None, *,
               tty=False, ci=False, interrupt=None, interrupt_mode="run"):
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
        if ci:
            env['CI'] = '1'
        if tty:
            import pty
            master, slave = pty.openpty()
            chunks = []
            try:
                process = subprocess.Popen([NODE, str(LAUNCHER), *arguments], cwd=tmp_path, env=env,
                                           stdin=slave, stdout=slave, stderr=slave)
                os.close(slave)
                slave = None
                started = time.monotonic()
                sent = False
                while time.monotonic() - started < 10:
                    if interrupt and not sent and time.monotonic() - started > 0.4 and log.exists():
                        recorded = [json.loads(line) for line in log.read_text().splitlines()]
                        if any(event['mode'] == interrupt_mode for event in recorded):
                            process.send_signal(interrupt)
                            sent = True
                    if select.select([master], [], [], 0.05)[0]:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError:
                            break
                        if not chunk:
                            break
                        chunks.append(chunk)
                    elif process.poll() is not None:
                        break
                result = subprocess.CompletedProcess(process.args, process.wait(timeout=2),
                                                     b"".join(chunks).decode(), "")
            finally:
                if 'process' in locals() and process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
                if slave is not None:
                    os.close(slave)
        else:
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
    assert events[-1]["args"][:5] == ["run", "--spec", "openvegas[audio]", "--quiet", "openvegas"]


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


@pytest.mark.parametrize('runtime', ['python3', 'pipx'])
def test_spinner_advances_through_bootstrap_then_clears_before_consent(launch, runtime):
    behavior = {runtime: {'probe': 0, 'probe_delay': 0.25, 'run_delay': 0.45, 'ready': True}}
    result, events = launch(behavior, arguments=(), tty=True)
    assert result.returncode == 0, result.stdout
    before, after = result.stdout.split('Browser sign-in consent')
    assert all(f'\x1b[34m{frame} ' in before for frame in '|/-\\')
    assert before.endswith('\r\x1b[2K')
    assert '\x1b[2K' not in after and '\x1b[34m' not in after
    assert events[-1]['tty'] == [True, True, True]
    assert sum(event['mode'] == 'run' for event in events) == 1
    if runtime == 'pipx':
        assert 'Preparing OpenVegas runtime...' in before
        assert events[-1]['args'].count('--quiet') == 1


@pytest.mark.parametrize('arguments,ci', [(['go', '--dry-run'], False), (['go', '--verbose'], False), (['go'], True)])
def test_spinner_is_disabled_for_diagnostics_and_ci(launch, arguments, ci):
    result, _ = launch({'python3': {'probe': 0, 'run_delay': 0.2}}, arguments, tty=True, ci=ci)
    assert result.returncode == 0
    assert '\x1b[34m' not in result.stdout


def test_download_failure_remains_visible_and_is_not_retried(launch):
    result, events = launch({'pipx': {'run_delay': 0.3, 'run': 17, 'stderr': 'fixture download failed'}},
                            arguments=(), tty=True)
    assert result.returncode == 17
    assert 'fixture download failed' in result.stdout
    assert 'OpenVegas ready' not in result.stdout
    assert result.stdout.endswith('\r\x1b[2K')
    assert len(events) == 1


@pytest.mark.parametrize('interrupt', [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize('runtime', ['python3', 'pipx'])
def test_interrupt_stops_bootstrap_without_fallback_or_replay(launch, runtime, interrupt):
    result, events = launch({runtime: {'probe': 0, 'run_delay': 8}}, arguments=(),
                            tty=True, interrupt=interrupt)
    assert result.returncode == 128 + interrupt, result.stdout
    assert sum(event['mode'] == 'run' for event in events) == 1
    assert 'OpenVegas ready' not in result.stdout


def test_verbose_pipx_keeps_diagnostics_opt_in(launch):
    result, events = launch({'pipx': {}}, arguments=('go', '--verbose'))
    assert result.returncode == 0
    assert '--quiet' not in events[-1]['args']


def test_interrupt_during_probe_never_starts_a_runtime(launch):
    result, events = launch({'python3': {'probe': 0, 'probe_delay': 8}, 'pipx': {}},
                            arguments=(), tty=True, interrupt=signal.SIGTERM, interrupt_mode='probe')
    assert result.returncode == 143
    assert [(event['name'], event['mode']) for event in events] == [('python3', 'probe')]


@pytest.mark.parametrize('tty,ci,arguments,expected', [
    (True, False, (), ['go']), (False, False, (), []),
    (True, True, (), []), (True, False, ('--help',), ['--help']),
])
def test_first_npx_run_only_enters_onboarding_interactively(launch, tty, ci, arguments, expected):
    result, events = launch({'python3': {'probe': 0}}, arguments, tty=tty, ci=ci)
    assert result.returncode == 0
    assert events[-1]['args'] == ['-m', 'openvegas.cli', *expected]


@pytest.mark.parametrize('tty,ci,arguments,expected', [
    (True, False, [], ['go']), (False, False, [], []),
    (True, True, [], []), (True, False, ['--help'], ['--help']),
])
def test_generated_binary_launcher_has_identical_first_run_contract(tmp_path, tty, ci, arguments, expected):
    import hashlib
    import pty
    from scripts.build_cli_candidate import npm_launcher
    if not NODE or os.name != 'posix':
        pytest.skip('Local PTY launcher check requires POSIX and Node')
    log = tmp_path / 'args.json'
    binary = tmp_path / 'fixture-cli'
    binary.write_text(f'#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\nPath({str(log)!r}).write_text(json.dumps(sys.argv[1:]))\n')
    binary.chmod(0o700)
    platform, arch = json.loads(subprocess.check_output([NODE, '-p', 'JSON.stringify([process.platform,process.arch])']))
    launcher = tmp_path / 'run.mjs'
    launcher.write_text(npm_launcher(binary.name, platform, arch, hashlib.sha256(binary.read_bytes()).hexdigest()))
    env = {'PATH': os.defpath, 'HOME': str(tmp_path)}
    if ci:
        env['CI'] = '1'
    master, slave = pty.openpty()
    try:
        result = subprocess.run([NODE, str(launcher), *arguments], env=env,
                                stdin=slave if tty else subprocess.DEVNULL,
                                stdout=slave if tty else subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=10)
    finally:
        os.close(master)
        os.close(slave)
    assert result.returncode == 0, result.stderr
    assert json.loads(log.read_text()) == expected
