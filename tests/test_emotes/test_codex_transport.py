"""Paid-free real subprocess fixtures, NOT live Codex/native UX certification."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from openvegas.emotes import EmoteController, Phase, State
from openvegas.emotes import codex_transport as transport
from openvegas.emotes import owned_stream as owned
from openvegas.emotes.spool import EventSpool

pytestmark = pytest.mark.skipif(os.name != "posix", reason="owned process groups are POSIX-only")


def line(method="turn/started", *, turn="turn-1", thread="thread-1", status="inProgress", **extra):
    params = {"threadId": thread, "turn": {"id": turn, "status": status, "error": None}}
    if method == "error":
        params = {"threadId": thread, "turnId": turn, "willRetry": False,
                  "error": {"message": "PRIVATE ERROR TEXT"}}
    elif method not in ("turn/started", "turn/completed"):
        params = {"threadId": thread, "turnId": turn}
    params.update(extra)
    return json.dumps({"method": method, "params": params}).encode() + b"\n"


def request(method="turn/start", **params):
    return json.dumps({"id": 1, "method": method, "params": {
        "threadId": "thread-1", **params,
    }}).encode() + b"\n"


@pytest.fixture
def executable(tmp_path):
    def build(body="", *, version="codex-cli 0.153.4\n", probe=None):
        path = tmp_path / "fake-codex"
        marker = tmp_path / "launched"
        script = (
            f"#!{sys.executable}\n"
            "import json, os, signal, sys, time\n"
            "from pathlib import Path\n"
            "if sys.argv[1:] == ['--version']:\n"
            f"    {(probe or f'os.write(1, {version.encode()!r})')}\n"
            "    raise SystemExit(0)\n"
            "assert sys.argv[1:] == ['app-server', '--stdio']\n"
            f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
            + body
        )
        path.write_text(script)
        path.chmod(0o700)
        return (str(path), "app-server", "--stdio")
    return build


@pytest.fixture
def spool(tmp_path):
    return EventSpool(tmp_path / "private" / "events")


async def chunks(*values):
    for value in values:
        yield value


async def relay(command, spool, input_bytes=None, **kwargs):
    out, err = bytearray(), bytearray()

    async def stdout(value):
        out.extend(value)

    async def stderr(value):
        err.extend(value)

    result = await asyncio.wait_for(transport.run_codex_transport(
        command, enabled=True, session_id="watch-session", spool=spool,
        input_bytes=chunks(request()) if input_bytes is None else input_bytes,
        stdout=stdout, stderr=stderr, shutdown_grace=0.2, **kwargs,
    ), timeout=8)
    return result, bytes(out), bytes(err)


def phases(spool):
    return [e.phase for e in spool.drain(source="codex", session_id="watch-session")]


@pytest.mark.parametrize("enabled", [False, None, "yes", 1])
def test_no_literal_opt_in_does_not_spawn_or_consume(enabled, tmp_path, monkeypatch):
    async def spawn(*args, **kwargs):
        pytest.fail("no process may launch without opt-in")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = asyncio.run(transport.run_codex_transport(
        None, enabled=enabled, session_id="anything", input_bytes=None,
        stdout=None, stderr=None,
    ))
    assert result == transport.TransportResult(False, None, "not_enabled")


@pytest.mark.parametrize("command,overrides", [
    ("codex app-server --stdio", {}),
    (("codex", "app-server", "--stdio"), {}),
    (("/tmp/codex", "exec", "--json"), {}),
    (("/tmp/codex", "app-server", "proxy"), {}),
    (("/tmp/codex", "app-server", "--listen", "ws://localhost:1234"), {}),
    (("/tmp/codex", "app-server", "--stdio"), {"session_id": "secret\ninvalid"}),
    (("/tmp/codex", "app-server", "--stdio"), {"thread_id": "a/b"}),
    (("/tmp/codex", "app-server", "--stdio"), {"version_timeout": float("inf")}),
    (("/tmp/codex", "app-server", "--stdio"), {"shutdown_grace": True}),
])
def test_rejects_shell_and_nonowned_commands_before_spawning(command, overrides, monkeypatch):
    async def spawn(*args, **kwargs):
        pytest.fail("invalid configuration must not launch")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    kwargs = {"enabled": True, "session_id": "watch-session", "input_bytes": None,
              "stdout": None, "stderr": None, **overrides}
    assert asyncio.run(transport.run_codex_transport(command, **kwargs)).reason == "invalid_configuration"


@pytest.mark.parametrize("version", ["codex-cli 0.153.3\n", "codex-cli 0.154.0\n",
                                     "codex-cli 0.153.4-preview\n", "0.153.4\n", "garbage"])
def test_unknown_version_neutral_no_app_launch_or_state(executable, spool, tmp_path, version):
    command = executable(version=version)
    result, out, err = asyncio.run(relay(command, spool))
    assert result == transport.TransportResult(False, None, "unsupported_version")
    assert (out, err) == (b"", b"")
    assert not (tmp_path / "launched").exists()
    assert not spool.directory.parent.exists()


def test_probe_handles_fragmented_version(executable, spool):
    command = executable(probe="os.write(1, b'codex-cli '); time.sleep(.02); os.write(1, b'0.153.4\\n')")
    assert asyncio.run(relay(command, spool))[0].launched


@pytest.mark.parametrize("probe,reason", [
    ("time.sleep(60)", "version_probe_failed"),
    ("os.write(1, b'x' * 10000)", "unsupported_version"),
    ("os.write(1, b'codex-cli 0.153.4\\n'); raise SystemExit(2)", "unsupported_version"),
])
def test_probe_bounded_and_nonzero_not_trusted(executable, spool, tmp_path, probe, reason):
    result, out, err = asyncio.run(relay(executable(probe=probe), spool, version_timeout=1.0))
    assert result.reason == reason
    assert not result.launched
    assert (out, err) == (b"", b"")
    assert not (tmp_path / "launched").exists()


@pytest.mark.parametrize("status,expected", [
    ("completed", Phase.COMPLETE), ("failed", Phase.ERROR), ("interrupted", Phase.CANCEL),
])
def test_success_failure_interrupt_are_native_metadata_and_output_exact(executable, spool, status, expected):
    message = b'{"method":"item/agentMessage/delta","params":{"delta":"SECRET ANSWER"}}\n'
    output = line() + message + line("turn/completed", status=status)
    command = executable(f"sys.stdin.buffer.read()\nos.write(1, {output!r})\nos.write(2, b'normal stderr\\n')\n")
    result, out, err = asyncio.run(relay(command, spool))
    assert result.returncode == 0
    assert out == output
    assert err == b"normal stderr\n"
    records = spool.drain(source="codex", session_id="watch-session")
    assert [e.phase for e in records] == [Phase.START, expected]
    assert all("SECRET" not in repr(e) and "delta" not in repr(e) for e in records)
    for path in spool.directory.parent.rglob("*"):
        if path.is_file():
            assert b"SECRET" not in path.read_bytes()


def test_normal_frontend_input_bytes_are_not_rewritten(executable, spool):
    data = b'{"id":1,"method":"initialize","params":{"clientInfo":{"name":"private frontend"}}}\r\n'
    data += request(input=[{"type": "text", "text": "SYNTHETIC PRIVATE PROMPT"}])
    command = executable("os.write(1, sys.stdin.buffer.read())\n")
    result, out, err = asyncio.run(relay(command, spool, chunks(data[:13], data[13:70], data[70:])))
    assert result.returncode == 0
    assert out == data and err == b""
    assert not spool.directory.parent.exists()


def test_cancel_intent_latches_before_forwarding_late_success(executable, spool):
    start, end = line(), line("turn/completed", status="completed")
    command = executable(
        f"sys.stdin.buffer.readline()\nos.write(1, {start!r})\n"
        f"sys.stdin.buffer.readline()\nos.write(1, {end!r})\n"
    )

    async def run():
        seen = asyncio.Event()
        output = bytearray()

        async def sink(data):
            output.extend(data)
            if start in output:
                seen.set()

        async def incoming():
            yield request()
            await seen.wait()
            yield request("turn/interrupt", turnId="turn-1")

        result = await asyncio.wait_for(transport.run_codex_transport(
            command, enabled=True, session_id="watch-session", spool=spool,
            input_bytes=incoming(), stdout=sink, stderr=sink, shutdown_grace=0.2,
        ), timeout=8)
        assert result.returncode == 0
        assert bytes(output) == start + end
    asyncio.run(run())
    assert phases(spool) == [Phase.START, Phase.CANCEL]


def test_early_interrupt_tombstones_before_start(executable, spool):
    output = line() + line("turn/completed", status="completed")
    command = executable(f"sys.stdin.buffer.read()\nos.write(1, {output!r})\n")
    asyncio.run(relay(command, spool, chunks(request(), request("turn/interrupt", turnId="turn-1"))))
    assert phases(spool) == []


def test_other_threads_do_not_animate_and_duplicate_success_once(executable, spool):
    output = (line(thread="other") + line() + line() + line("item/tool/requestUserInput")
              + line("turn/completed", thread="other", status="completed")
              + line("turn/completed", status="completed") * 2)
    command = executable(f"sys.stdin.buffer.read()\nos.write(1, {output!r})\n")
    _, out, _ = asyncio.run(relay(command, spool))
    assert out == output
    assert phases(spool) == [Phase.START, Phase.PAUSE, Phase.COMPLETE]


def test_nonretrying_error_never_then_celebrates(executable, spool):
    output = line() + line("error") + line("turn/completed", status="completed")
    command = executable(f"sys.stdin.buffer.read()\nos.write(1, {output!r})\n")
    _, out, _ = asyncio.run(relay(command, spool))
    assert out == output
    assert phases(spool) == [Phase.START, Phase.ERROR]


@pytest.mark.parametrize("exitcode", [0, 1, 17])
def test_eof_or_exit_status_never_infers_success(executable, spool, exitcode):
    command = executable(f"sys.stdin.buffer.read()\nos.write(1, {line()!r})\nraise SystemExit({exitcode})\n")
    result, _, _ = asyncio.run(relay(command, spool))
    assert result.returncode == exitcode
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]


@pytest.mark.parametrize("bad", [
    b'not-json\n', b'{"method":"error","params":null}\n',
    b'{"method":"unknown","method":"turn/completed"}\n',
    b'[]\n', b'{"test":NaN}\n', b'\xff\n', b'{"method":',
])
def test_malformed_record_disables_cosmetics_but_forwarded_exactly(executable, spool, bad):
    output = line() + bad + line("turn/completed", status="completed")
    command = executable(f"sys.stdin.buffer.read()\nos.write(1, {output!r})\n")
    result, out, _ = asyncio.run(relay(command, spool))
    assert result.returncode == 0 and out == output
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]


def test_oversized_record_is_forwarded_without_unbounded_observer_buffer(executable, spool):
    command = executable(
        f"sys.stdin.buffer.read()\nos.write(1, {line()!r})\n"
        f"os.write(1, b'x' * {transport.MAX_LINE_BYTES + 1} + b'\\n')\n"
        f"os.write(1, {line('turn/completed', status='completed')!r})\n"
    )
    result, out, _ = asyncio.run(relay(command, spool))
    assert result.reason == "oversized_record"
    assert out == line() + b"x" * (transport.MAX_LINE_BYTES + 1) + b"\n" + line("turn/completed", status="completed")
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]


def test_partial_record_eof_neutral(executable, spool):
    output = line() + b'{"method":"turn/completed"'
    command = executable(f"sys.stdin.buffer.read()\nos.write(1, {output!r})\n")
    result, out, _ = asyncio.run(relay(command, spool))
    assert result.reason == "incomplete_record" and out == output
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]


def test_spool_failure_does_not_break_protocol(executable, spool, monkeypatch):
    output = line() + line("turn/completed", status="completed")
    command = executable(f"sys.stdin.buffer.read()\nos.write(1, {output!r})\n")
    monkeypatch.setattr(spool, "publish", lambda _: False)
    result, out, _ = asyncio.run(relay(command, spool))
    assert result.returncode == 0 and out == output
    assert result.reason == "publication_failed"


def test_no_outbound_or_explicit_thread_selection_means_no_cosmetics(executable, spool):
    output = line() + line("turn/completed", status="completed")
    command = executable(f"os.write(1, {output!r})\n")
    result, out, _ = asyncio.run(relay(command, spool, chunks()))
    assert result.returncode == 0 and out == output
    assert not spool.directory.parent.exists()


def test_explicit_thread_binding(executable, spool):
    output = line() + line("turn/completed", status="completed")
    command = executable(f"os.write(1, {output!r})\n")
    result, out, _ = asyncio.run(relay(command, spool, chunks(), thread_id="thread-1"))
    assert result.returncode == 0 and out == output
    assert phases(spool) == [Phase.START, Phase.COMPLETE]


def test_shutdown_after_frontend_eof_is_bounded(executable, spool, tmp_path):
    command = executable(
        f"sys.stdin.buffer.read()\nos.write(1, {line()!r})\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n"
    )
    result, _, _ = asyncio.run(relay(command, spool))
    assert result.reason == "shutdown_timeout"
    assert result.returncode == -signal.SIGKILL
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "launched").read_text()), 0)
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]


def test_caller_cancellation_cleans_owned_process_and_remains_cancelled(executable, spool, tmp_path):
    command = executable(f"sys.stdin.buffer.readline()\nos.write(1, {line()!r})\ntime.sleep(60)\n")

    async def run():
        seen = asyncio.Event()

        async def incoming():
            yield request()
            await asyncio.Event().wait()

        async def sink(data):
            seen.set()

        task = asyncio.create_task(transport.run_codex_transport(
            command, enabled=True, session_id="watch-session", spool=spool,
            input_bytes=incoming(), stdout=sink, stderr=sink, shutdown_grace=0.2,
        ))
        await asyncio.wait_for(seen.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
    asyncio.run(run())
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "launched").read_text()), 0)
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]


def test_sink_disconnect_does_not_leave_listening(executable, spool):
    command = executable(f"os.write(1, {line()!r})\ntime.sleep(60)\n")

    async def run():
        async def incoming():
            yield request()
            await asyncio.Event().wait()

        async def broken(data):
            raise BrokenPipeError("SYNTHETIC PRIVATE DETAIL")

        return await asyncio.wait_for(transport.run_codex_transport(
            command, enabled=True, session_id="watch-session", thread_id="thread-1", spool=spool,
            input_bytes=incoming(), stdout=broken, stderr=broken, shutdown_grace=0.2,
        ), timeout=5)
    result = asyncio.run(run())
    assert result.reason == "transport_error"
    assert "PRIVATE" not in repr(result)
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]


def test_real_module_entrypoint_forwards_raw_protocol_and_never_ansi(executable, tmp_path):
    output = b'{"method":"item/agentMessage/delta","params":{"delta":"synthetic"}}\n'
    command = executable(f"sys.stdin.buffer.read()\nos.write(1, {output!r})\nos.write(2, b'child log\\n')\n")
    result = subprocess.run([
        sys.executable, "-m", "openvegas.emotes.codex_transport", "--enable",
        "--session-id", "test-session", "--", *command,
    ], input=request(), capture_output=True, timeout=8, check=False)
    assert result.returncode == 0
    assert result.stdout == output
    assert result.stderr == b"child log\n"
    assert b"\x1b" not in result.stdout


def test_module_help_is_passive_and_no_optin_never_launches(executable, tmp_path):
    command = executable()
    result = subprocess.run([
        sys.executable, "-m", "openvegas.emotes.codex_transport",
        "--session-id", "test-session", "--", *command,
    ], input=b"", capture_output=True, timeout=5, check=False)
    assert result.returncode == 2 and result.stdout == b""
    assert not (tmp_path / "launched").exists()
    result = subprocess.run([
        sys.executable, "-m", "openvegas.emotes.codex_transport", "--help",
    ], capture_output=True, timeout=5, check=False)
    assert result.returncode == 0
    assert b"--enable" in result.stdout
    assert not (tmp_path / "launched").exists()


def test_probe_cancellation_reaps_owned_probe(executable, tmp_path):
    probe_pid = tmp_path / "probe-pid"
    command = executable(probe=f"Path({str(probe_pid)!r}).write_text(str(os.getpid())); time.sleep(60)")

    async def run():
        task = asyncio.create_task(transport.run_codex_transport(
            command, enabled=True, session_id="watch-session", input_bytes=None,
            stdout=None, stderr=None, shutdown_grace=0.1,
        ))
        async with asyncio.timeout(5):
            while not probe_pid.exists():
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
    asyncio.run(run())
    with pytest.raises(ProcessLookupError):
        os.kill(int(probe_pid.read_text()), 0)
    assert not (tmp_path / "launched").exists()


def test_repeated_cancellation_does_not_abandon_child(executable, spool, tmp_path):
    command = executable(
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"os.write(1, {line()!r})\ntime.sleep(60)\n"
    )

    async def run():
        seen = asyncio.Event()
        async def sink(_data):
            seen.set()
        async def incoming():
            yield request()
            await asyncio.Event().wait()
        task = asyncio.create_task(transport.run_codex_transport(
            command, enabled=True, session_id="watch-session", spool=spool,
            input_bytes=incoming(), stdout=sink, stderr=sink, shutdown_grace=0.2,
        ))
        await asyncio.wait_for(seen.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
    asyncio.run(run())
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "launched").read_text()), 0)
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]


def test_child_retaining_inherited_pipe_cannot_hang_shutdown(executable, spool):
    command = executable(
        f"sys.stdin.buffer.read()\nos.write(1, {line()!r})\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    time.sleep(60)\n"
        "else:\n"
        "    raise SystemExit(0)\n"
    )
    result, _, _ = asyncio.run(relay(command, spool))
    assert result.returncode == 0
    assert result.reason == "shutdown_timeout"
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]


def test_module_propagates_child_exit_code(executable):
    command = executable("raise SystemExit(17)\n")
    result = subprocess.run([
        sys.executable, "-m", "openvegas.emotes.codex_transport", "--enable",
        "--session-id", "test-session", "--", *command,
    ], input=b"", capture_output=True, timeout=8, check=False)
    assert result.returncode == 17
    assert result.stdout == b"" and result.stderr == b""


def test_module_refuses_transcript_file_redirection(executable, tmp_path):
    command = executable()
    with (tmp_path / "output-file").open("wb") as output:
        result = subprocess.run([
            sys.executable, "-m", "openvegas.emotes.codex_transport", "--enable",
            "--session-id", "test-session", "--", *command,
        ], input=b"", stdout=output, stderr=subprocess.PIPE, timeout=5, check=False)
    assert result.returncode == 2
    assert not (tmp_path / "launched").exists()
    assert (tmp_path / "output-file").read_bytes() == b""


def test_slow_frontend_receives_all_buffered_output_after_child_exit(executable, spool):
    output = (b'{"method":"item/agentMessage/delta","params":{"delta":"' + b"x" * 20000 + b'"}}\n') * 5
    command = executable(f"sys.stdin.buffer.read()\nos.write(1, {output!r})\n")

    async def run():
        out = bytearray()
        async def sink(data):
            await asyncio.sleep(0.3)
            out.extend(data)
        result = await asyncio.wait_for(transport.run_codex_transport(
            command, enabled=True, session_id="watch-session", spool=spool,
            input_bytes=chunks(request()), stdout=sink, stderr=sink, shutdown_grace=0.2,
        ), timeout=8)
        assert result.returncode == 0
        assert result.reason == "closed"
        assert bytes(out) == output
    asyncio.run(run())


def test_real_silent_owned_turn_survives_twenty_second_lease(executable, spool, pack, monkeypatch):
    """Real 25+ second owned fixture, not an accelerated timer or provider call."""
    command = executable(
        f"sys.stdin.buffer.readline()\nos.write(1, {line()!r})\n"
        f"sys.stdin.buffer.readline()\nos.write(1, {line('turn/completed', status='completed')!r})\n"
    )
    events, states, writers = [], [], set()
    controller = EmoteController(pack, source="codex", session_id="watch-session")
    original = spool.publish

    async def run():
        renewed = asyncio.Event()
        def publish(event):
            delivered = original(event)
            events.append(event)
            writers.add(threading.get_ident())
            controller.handle(event)
            states.append(controller.current_state)
            if sum(e.phase == Phase.BUSY for e in events) >= 5:
                renewed.set()
            return delivered
        monkeypatch.setattr(spool, "publish", publish)
        async def incoming():
            yield request()
            await renewed.wait()
            yield request("fixture/finish")
        out = bytearray()
        async def sink(data):
            out.extend(data)
        started = time.monotonic()
        result = await asyncio.wait_for(transport.run_codex_transport(
            command, enabled=True, session_id="watch-session", spool=spool,
            input_bytes=incoming(), stdout=sink, stderr=sink, shutdown_grace=0.2,
        ), timeout=35)
        assert time.monotonic() - started >= 25
        assert result.returncode == 0 and result.reason == "closed"
        assert bytes(out) == line() + line("turn/completed", status="completed")
    asyncio.run(run())
    assert [e.phase for e in events] == [Phase.START] + [Phase.BUSY] * 5 + [Phase.COMPLETE]
    assert [e.sequence for e in events] == list(range(7))
    assert states == [State.ACTIVE] * 6 + [State.COMPLETE]
    assert len(writers) == 1


@pytest.mark.parametrize("barrier", ["pause", "failed", "completed", "interrupted", "error",
                                     "cancel", "server_eof", "client_eof", "invalid"])
def test_transport_never_heartbeats_after_barrier(executable, spool, monkeypatch, barrier):
    monkeypatch.setattr(owned, "HEARTBEAT_SECONDS", 0.04)
    monkeypatch.setattr(transport, "HEARTBEAT_POLL_SECONDS", 0.01)
    after = {
        "pause": line("item/tool/requestUserInput"),
        "failed": line("turn/completed", status="failed"),
        "completed": line("turn/completed", status="completed"),
        "interrupted": line("turn/completed", status="interrupted"),
        "error": line("error"),
        "cancel": b"",
        "server_eof": b"",
        "client_eof": b"",
        "invalid": b"bad-json\n",
    }[barrier]
    ending = "os.close(1)\ntime.sleep(60)\n" if barrier == "server_eof" else (
        f"os.write(1, {after!r})\ntime.sleep(.2)\n"
        f"os.write(1, {line('turn/completed', status='completed')!r})\n"
    )
    command = executable(
        f"sys.stdin.buffer.readline()\nos.write(1, {line()!r})\n"
        "sys.stdin.buffer.readline()\n" + ending
    )
    events, marker = [], []
    original = spool.publish

    async def run():
        renewed = asyncio.Event()
        ended = asyncio.Event()
        def publish(event):
            result = original(event)
            events.append(event)
            if event.phase == Phase.BUSY:
                renewed.set()
            elif (not marker and barrier not in {"cancel", "client_eof"}
                  and event.phase in {Phase.PAUSE, Phase.CANCEL, Phase.ERROR, Phase.COMPLETE}):
                marker.append(len(events))
            return result
        monkeypatch.setattr(spool, "publish", publish)
        async def incoming():
            yield request()
            await renewed.wait()
            if barrier in {"cancel", "client_eof"}:
                marker.append(len(events))
            if barrier == "client_eof":
                return
            if barrier == "cancel":
                yield request("turn/interrupt", turnId="turn-1")
            else:
                yield request("fixture/barrier")
            await ended.wait()
        async def sink(_data):
            pass
        result = await asyncio.wait_for(transport.run_codex_transport(
            command, enabled=True, session_id="watch-session", spool=spool,
            input_bytes=incoming(), stdout=sink, stderr=sink, shutdown_grace=0.4,
        ), timeout=5)
        assert result.launched
    asyncio.run(run())
    # Native pause/end becomes authoritative when observed, not when the test
    # requests it. Local cancellation/EOF is latched immediately by the owner.
    assert marker
    assert Phase.BUSY not in [e.phase for e in events[marker[0]:]]
    assert [e.sequence for e in events] == list(range(len(events)))
    if barrier in {"failed", "interrupted", "error", "cancel", "server_eof", "invalid"}:
        assert Phase.COMPLETE not in [e.phase for e in events]


def test_transport_heartbeat_failure_detaches_without_stopping_protocol(executable, spool, monkeypatch):
    monkeypatch.setattr(owned, "HEARTBEAT_SECONDS", 0.04)
    monkeypatch.setattr(transport, "HEARTBEAT_POLL_SECONDS", 0.01)
    command = executable(
        f"sys.stdin.buffer.readline()\nos.write(1, {line()!r})\n"
        f"sys.stdin.buffer.readline()\nos.write(1, {line('turn/completed', status='completed')!r})\n"
    )
    events = []
    original = spool.publish
    async def run():
        attempted = asyncio.Event()
        def publish(event):
            events.append(event)
            if event.phase == Phase.BUSY:
                attempted.set()
                return False
            return original(event)
        monkeypatch.setattr(spool, "publish", publish)
        async def incoming():
            yield request()
            await attempted.wait()
            yield request("fixture/finish")
        result, output, _ = await relay(command, spool, incoming())
        assert result.reason == "publication_failed" and result.returncode == 0
        assert output == line() + line("turn/completed", status="completed")
    asyncio.run(run())
    assert [e.phase for e in events] == [Phase.START, Phase.BUSY]


def test_transport_cancellation_after_renewal_never_renews_again(executable, spool, monkeypatch):
    monkeypatch.setattr(owned, "HEARTBEAT_SECONDS", 0.04)
    monkeypatch.setattr(transport, "HEARTBEAT_POLL_SECONDS", 0.01)
    command = executable(
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"sys.stdin.buffer.readline()\nos.write(1, {line()!r})\ntime.sleep(60)\n"
    )
    events = []
    original = spool.publish
    async def run():
        renewed = asyncio.Event()
        def publish(event):
            result = original(event)
            events.append(event)
            if event.phase == Phase.BUSY:
                renewed.set()
            return result
        monkeypatch.setattr(spool, "publish", publish)
        async def incoming():
            yield request()
            await asyncio.Event().wait()
        async def sink(_data):
            pass
        task = asyncio.create_task(transport.run_codex_transport(
            command, enabled=True, session_id="watch-session", spool=spool,
            input_bytes=incoming(), stdout=sink, stderr=sink, shutdown_grace=0.2,
        ))
        await asyncio.wait_for(renewed.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
        await asyncio.sleep(0.1)
    asyncio.run(run())
    assert [e.phase for e in events] == [Phase.START, Phase.BUSY, Phase.CANCEL, Phase.EXIT]


def test_known_server_eof_suppresses_heartbeat_even_with_slow_output_sink(executable, spool, monkeypatch):
    monkeypatch.setattr(owned, "HEARTBEAT_SECONDS", 0.04)
    monkeypatch.setattr(transport, "HEARTBEAT_POLL_SECONDS", 0.01)
    command = executable(
        f"sys.stdin.buffer.readline()\nos.write(1, {line()!r})\nos.close(1)\ntime.sleep(60)\n"
    )
    events = []
    original = spool.publish
    def publish(event):
        events.append(event)
        return original(event)
    monkeypatch.setattr(spool, "publish", publish)

    async def run():
        async def incoming():
            yield request()
            await asyncio.Event().wait()
        async def slow_sink(data):
            await asyncio.sleep(0.3)
        return await asyncio.wait_for(transport.run_codex_transport(
            command, enabled=True, session_id="watch-session", spool=spool,
            input_bytes=incoming(), stdout=slow_sink, stderr=slow_sink, shutdown_grace=0.2,
        ), timeout=5)
    assert asyncio.run(run()).reason == "shutdown_timeout"
    assert [e.phase for e in events] == [Phase.START, Phase.CANCEL, Phase.EXIT]
