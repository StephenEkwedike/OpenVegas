import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from openvegas.emotes import EventSpool, Phase
from openvegas.emotes.commands import EmoteServices, emote
from openvegas.emotes.resources import Catalog, PackRepository
from openvegas.emotes.runner import reserve_generation, run_command
from openvegas.emotes.selection import SelectionStore
from openvegas.emotes.spool import _locked


def test_real_command_exit_and_event_scope(tmp_path, capfd):
    spool = EventSpool(tmp_path / "events")
    assert (
        run_command(
            [sys.executable, "-c", "import sys; print('child only'); sys.exit(37)"],
            session_id="s",
            spool=spool,
        )
        == 37
    )
    assert capfd.readouterr() == ("child only\n", "")
    events = spool.drain(source="openvegas", session_id="s")
    assert [e.phase for e in events] == [Phase.START, Phase.ERROR]
    assert events[0].generation == 1
    assert run_command([sys.executable, "-c", "pass"], session_id="s", spool=spool) == 0
    events = spool.drain(source="openvegas", session_id="s")
    assert [e.phase for e in events] == [Phase.START, Phase.COMPLETE]
    assert events[0].generation == 2
    assert events[-1].outcome == "success"
    # No command names, args, cwd or text can enter the event contract.
    assert b"child only" not in b"".join(e.to_bytes() for e in events)


def test_exact_argv_no_shell_and_inherited_streams(tmp_path, capfd):
    spool = EventSpool(tmp_path / "events")
    literal = "hello; $(do-not-execute) *.txt --session private-content"
    code = "import sys; print(repr(sys.argv[1])); sys.stderr.write('err\\x00bytes')"
    assert run_command([sys.executable, "-c", code, literal], session_id="s", spool=spool) == 0
    output = capfd.readouterr()
    assert output.out == repr(literal) + "\n"
    assert output.err == "err\x00bytes"


def test_launch_errors_silent_and_failures_not_success(tmp_path, capfd):
    spool = EventSpool(tmp_path / "events")
    assert run_command([str(tmp_path / "missing")], session_id="s", spool=spool) == 127
    target = tmp_path / "not-executable"
    target.write_text("test-data")
    assert run_command([str(target)], session_id="s", spool=spool) == 126
    phases = [e.phase for e in spool.drain(source="openvegas", session_id="s")]
    assert phases == [Phase.START, Phase.ERROR, Phase.START, Phase.ERROR]
    assert capfd.readouterr() == ("", "")


def test_busy_generation_lock_does_not_block_command(tmp_path):
    spool = EventSpool(tmp_path / "events")
    with _locked(tmp_path / "run-generations"):
        assert reserve_generation(spool, source="openvegas", session_id="s") is None
        assert run_command([sys.executable, "-c", "pass"], session_id="s", spool=spool) == 0
    assert spool.drain(source="openvegas", session_id="s") == []


def test_generation_corruption_fails_closed(tmp_path):
    spool = EventSpool(tmp_path / "events")
    assert reserve_generation(spool, source="openvegas", session_id="s") == 1
    path = next((tmp_path / "run-generations").glob("*.json"))
    path.write_text('{"generation":true}')
    assert reserve_generation(spool, source="openvegas", session_id="s") is None


def test_click_passthrough_and_watch_default_source(tmp_path, monkeypatch):
    from openvegas.emotes import runner

    received = []
    monkeypatch.setattr(runner, "run_command", lambda argv, **kw: received.append((argv, kw)) or 41)
    cli = CliRunner()
    result = cli.invoke(
        emote,
        [
            "run",
            "--session",
            "X",
            "--",
            "program",
            "--flag",
            "a b",
            "--session",
            "child",
        ],
    )
    assert result.exit_code == 41
    assert result.output == ""
    assert received[0][0] == ("program", "--flag", "a b", "--session", "child")
    assert received[0][1]["session_id"] == "X"
    assert received[0][1]["source"] == "openvegas"
    services = EmoteServices(
        PackRepository(tmp_path),
        Catalog(),
        SelectionStore(tmp_path / "state"),
        EventSpool(tmp_path / "events"),
    )
    result = cli.invoke(emote, ["watch", "--session", "X"], obj=services)
    assert result.exit_code == 0
    assert "requires its own interactive terminal" in result.output
    monkeypatch.setattr(runner, "run_command", lambda *args, **kwargs: -signal.SIGTERM)
    assert cli.invoke(emote, ["run", "--session", "X", "--", "program"]).exit_code == 143


def wrapper_process(tmp_path, code):
    # Exercise OS pipe inheritance, not Click's in-process redirected sys streams.
    root = str(Path(__file__).resolve().parents[2])
    script = (
        f"import sys; sys.path.insert(0, {root!r}); "
        "from openvegas.emotes.runner import run_command; "
        "from openvegas.emotes import EventSpool; "
        f"rc=run_command([sys.executable, '-c', {code!r}], session_id='s', "
        f"spool=EventSpool({str(tmp_path / 'events')!r})); "
        "sys.exit(rc if rc >= 0 else 128-rc)"
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_wrapper_binary_stdin_stdout_stderr_exact(tmp_path):
    process = wrapper_process(
        tmp_path,
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stderr.buffer.write(b'error\\x00tail'); sys.exit(23)",
    )
    data = b"input\x00\xff\nmultiline\r\n"
    out, err = process.communicate(data, timeout=10)
    assert process.returncode == 23
    assert out == data
    assert err == b"error\x00tail"


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal behavior")
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_parent_signal_forwarded_non_tty_and_cancelled(tmp_path, sig):
    code = "import signal,sys,time; signal.signal(signal.SIGINT, signal.SIG_DFL); print('ready', flush=True); time.sleep(30)"
    process = wrapper_process(tmp_path, code)
    try:
        assert process.stdout.readline() == b"ready\n"
        process.send_signal(sig)
        out, err = process.communicate(timeout=5)
        assert process.returncode == 128 + sig
        assert out == err == b""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    events = EventSpool(tmp_path / "events").drain(source="openvegas", session_id="s")
    assert [e.phase for e in events] == [Phase.START, Phase.CANCEL]


def test_signal_handlers_restored(tmp_path):
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    before = {sig: signal.getsignal(sig) for sig in signals}
    run_command(
        [sys.executable, "-c", "pass"],
        session_id="s",
        spool=EventSpool(tmp_path / "events"),
    )
    assert {sig: signal.getsignal(sig) for sig in signals} == before


@pytest.mark.parametrize("argv", [[], ["bad\0argument"]])
def test_invalid_command_rejected(argv, tmp_path):
    with pytest.raises(ValueError):
        run_command(argv, session_id="s", spool=EventSpool(tmp_path / "events"))


@pytest.mark.parametrize("exit_code", [0, 37, -signal.SIGTERM])
def test_long_child_wait_renews_lease_without_threads_or_capture(
    tmp_path, monkeypatch, capsys, exit_code, pack, clock
):
    from openvegas.emotes import EmoteController, State, runner

    events = []
    spool = EventSpool(tmp_path / "events")
    consumer = EmoteController(pack, source="openvegas", session_id="long", clock=clock)
    monkeypatch.setattr(spool, "publish", lambda e: events.append(e) or consumer.handle(e))
    waits = []

    class Child:
        def wait(self, *, timeout):
            waits.append(timeout)
            if len(waits) <= 6:  # virtual 30-second process; no real sleeping
                clock.advance(timeout)
                assert consumer.current_state == State.ACTIVE
                raise subprocess.TimeoutExpired("synthetic-child", timeout)
            return exit_code

    def popen(argv, **kwargs):
        assert argv == ["synthetic-child"]
        assert kwargs == {
            "shell": False,
            "stdin": None,
            "stdout": None,
            "stderr": None,
            "start_new_session": False,
        }
        return Child()

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    handlers = {sig: signal.getsignal(sig) for sig in signals}
    assert run_command(["synthetic-child"], session_id="long", spool=spool) == exit_code
    assert waits == [5] * 7
    assert clock.now == 30
    expected = Phase.COMPLETE if exit_code == 0 else Phase.ERROR if exit_code > 0 else Phase.CANCEL
    assert [e.phase for e in events] == [Phase.START] + [Phase.BUSY] * 6 + [expected]
    assert [e.sequence for e in events] == list(range(8))
    assert {sig: signal.getsignal(sig) for sig in signals} == handlers
    assert capsys.readouterr() == ("", "")


def test_257_distinct_sessions_use_one_monotonic_global_counter(tmp_path):
    spool = EventSpool(tmp_path / "events")
    for index in range(257):
        assert reserve_generation(spool, source="openvegas", session_id=f"s-{index}") == index + 1
    assert reserve_generation(spool, source="other-source", session_id="s-0") == 258
    assert reserve_generation(spool, source="openvegas", session_id="s-0") == 259
    files = list((tmp_path / "run-generations").glob("*.json"))
    assert [p.name for p in files] == ["global-v1.json"]
    assert files[0].stat().st_size <= 128


def test_global_counter_seeds_above_all_256_legacy_records_once(tmp_path, monkeypatch):
    from openvegas.emotes import runner
    from openvegas.emotes.spool import atomic_write

    spool = EventSpool(tmp_path / "events")
    with _locked(tmp_path / "run-generations") as fd:
        for index in range(256):
            atomic_write(fd, f"{index:064x}.json", json.dumps({"generation": index * 3}).encode())
    assert reserve_generation(spool, source="openvegas", session_id="fresh") == 766
    monkeypatch.setattr(runner, "_names", lambda fd: pytest.fail("must not rescan migrated state"))
    assert reserve_generation(spool, source="openvegas", session_id="another") == 767


@pytest.mark.parametrize("damage", ["corrupt", "oversized", "symlink", "overflow", "too_many"])
def test_legacy_migration_invalid_state_never_resets_counter(tmp_path, damage):
    from openvegas.emotes.spool import atomic_write

    spool = EventSpool(tmp_path / "events")
    root = tmp_path / "run-generations"
    with _locked(root) as fd:
        atomic_write(fd, "a" * 64 + ".json", b'{"generation":900}')
        if damage == "too_many":
            for index in range(256):
                atomic_write(fd, f"{index:064x}.json", b'{"generation":1}')
        elif damage == "symlink":
            (root / ("b" * 64 + ".json")).symlink_to(root / ("a" * 64 + ".json"))
        else:
            data = {
                "corrupt": b'{"generation":true}',
                "oversized": b"x" * 129,
                "overflow": json.dumps({"generation": 2**53 - 2}).encode(),
            }[damage]
            atomic_write(fd, "b" * 64 + ".json", data)
    assert reserve_generation(spool, source="openvegas", session_id="s") is None
    assert not (root / "global-v1.json").exists()
    assert (root / ("a" * 64 + ".json")).read_bytes() == b'{"generation":900}'


@pytest.mark.skipif(os.name != "posix", reason="Owned POSIX PTY test")
def test_owned_pty_inheritance_input_and_control_c(tmp_path):
    import pty
    import select
    import time

    root = str(Path(__file__).resolve().parents[2])
    code = (
        "import os,signal,sys,time; signal.signal(signal.SIGINT, signal.SIG_DFL); "
        "print('TTY='+str(all(os.isatty(i) for i in (0,1,2))),flush=True); "
        "line=sys.stdin.readline(); print('CHILD='+line.strip(),flush=True); time.sleep(30)"
    )
    script = (
        f"import sys; sys.path.insert(0,{root!r}); "
        "from openvegas.emotes.runner import run_command; from openvegas.emotes import EventSpool; "
        f"rc=run_command([sys.executable,'-c',{code!r}],session_id='pty',spool=EventSpool({str(tmp_path / 'events')!r})); "
        "sys.exit(rc if rc>=0 else 128-rc)"
    )
    pid, master = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", script])
    output = b""
    reaped = False

    def until(token):
        nonlocal output
        deadline = time.monotonic() + 5
        while token not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 4096)
        assert token in output

    try:
        until(b"TTY=True")
        os.write(master, b"typed input\n")
        until(b"CHILD=typed input")
        os.write(master, b"\x03")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                reaped = True
                assert os.waitstatus_to_exitcode(status) == 130
                break
            time.sleep(0.02)
        assert reaped
        events = EventSpool(tmp_path / "events").drain(source="openvegas", session_id="pty")
        assert [e.phase for e in events] == [Phase.START, Phase.CANCEL]
    finally:
        if not reaped:
            os.killpg(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(master)
