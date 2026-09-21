"""Real POSIX PTYs with synthetic accounts/events, not native UX certification."""

import errno
import json
import os
import select
import signal
import sys
import textwrap
import time
from pathlib import Path

import pytest

from openvegas.emotes import commands
from openvegas.emotes.events import Event, Phase
from openvegas.emotes.spool import EventSpool

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX PTY evidence only")


class PtyProcess:
    def __init__(self, script):
        import pty

        import openvegas

        # Resolve the tested overlay too, rather than silently importing baseline commands.
        bootstrap = (
            f"import sys; sys.path.insert(0, {str(Path(openvegas.__file__).parents[1])!r}); "
            "import openvegas.emotes; "
            f"openvegas.emotes.__path__.insert(0, {str(Path(commands.__file__).parent)!r});\n"
        )
        self.pid, self.master = pty.fork()
        if self.pid == 0:
            os.execv(
                sys.executable, [sys.executable, "-B", "-c", bootstrap + textwrap.dedent(script)]
            )
        self.output = b""
        self.reaped = False

    def read(self):
        if select.select([self.master], [], [], 0.05)[0]:
            try:
                self.output += os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise

    def until(self, token):
        deadline = time.monotonic() + 8
        while token not in self.output and time.monotonic() < deadline:
            self.read()
        assert token in self.output, self.output.decode(errors="replace")

    def write(self, data):
        os.write(self.master, data)

    def wait(self, expected=0):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.read()
            waited, status = os.waitpid(self.pid, os.WNOHANG)
            if waited:
                self.reaped = True
                assert os.waitstatus_to_exitcode(status) == expected, self.output.decode(
                    errors="replace"
                )
                return
        pytest.fail("PTY child did not exit: " + self.output.decode(errors="replace"))

    def close(self):
        if not self.reaped:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(self.pid, 0)
        os.close(self.master)


@pytest.mark.parametrize("action", ["equip", "quit", "interrupt", "eof"])
def test_picker_real_pty_restores_modes_closes_library_and_preserves_input(tmp_path, action):
    child = PtyProcess(f"""
        import click, os, sys, termios
        from pathlib import Path
        from openvegas.emotes.commands import EmoteServices, emote
        from openvegas.emotes.resources import Catalog, PackRepository
        from openvegas.emotes.selection import SelectionStore
        from openvegas.emotes.spool import EventSpool
        root = Path({str(tmp_path)!r})
        selection = SelectionStore(root / "state")
        class Library:
            closed = 0
            calls = 0
            async def refresh(self):
                return {{"owned": {{"entitlements": [{{"pack_id": "synthetic.pack", "slot": "companion", "effective_status": "active", "activatable": True}}]}}}}
            async def equip(self, pack_id, *, slot):
                assert slot == "companion"
                self.calls += 1
                selection.write(pack_id)
            def close(self):
                self.closed += 1
        library = Library()
        services = EmoteServices(PackRepository(root / "empty"), Catalog(), selection, EventSpool(root / "events"), lambda _: library)
        before = termios.tcgetattr(0)
        try:
            emote.main(args=[], obj=services, standalone_mode=False)
        except click.Abort:
            print("ABORTED", flush=True)
        print("MODES=" + str(before == termios.tcgetattr(0)), flush=True)
        print("CLOSED=" + str(library.closed), flush=True)
        print("EQUIPS=" + str(library.calls), flush=True)
        print("HOST_READY", flush=True)
        line = sys.stdin.readline()
        print("HOST_INPUT=" + line.rstrip("\\n"), flush=True)
    """)
    try:
        child.until(b"Choose [q]:")
        if action in {"equip", "quit"}:
            # Both lines arrive together: the chooser must consume only its own line.
            child.write((b"1\n" if action == "equip" else b"q\n") + b"paste stays with host\n")
        else:
            child.write(b"\x03" if action == "interrupt" else b"\x04")
            child.until(b"HOST_READY")
            child.write(b"paste stays with host\n")
        child.until(b"HOST_INPUT=paste stays with host")
        child.wait()
        assert b"MODES=True" in child.output and b"CLOSED=1" in child.output
        assert (b"EQUIPS=1" if action == "equip" else b"EQUIPS=0") in child.output
        assert b"\x1b" not in child.output
        assert (b"ABORTED" in child.output) == (action in {"interrupt", "eof"})
    finally:
        child.close()


def _event(generation, sequence, phase):
    return Event(
        "openvegas",
        "synthetic-pty",
        f"turn-{generation}",
        f"event-{generation}-{sequence}",
        phase,
        generation,
        sequence,
        "success" if phase == Phase.COMPLETE else None,
    )


def test_picker_process_exit_preserves_typeahead_for_parent_host(tmp_path):
    import openvegas

    inner = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(Path(openvegas.__file__).parents[1])!r})
        import openvegas.emotes
        openvegas.emotes.__path__.insert(0, {str(Path(commands.__file__).parent)!r})
        from pathlib import Path
        from openvegas.emotes.commands import EmoteServices, emote
        from openvegas.emotes.manifest import PackError
        from openvegas.emotes.resources import Catalog, PackRepository
        from openvegas.emotes.selection import SelectionStore
        from openvegas.emotes.spool import EventSpool
        def offline(_):
            raise PackError("Synthetic offline account")
        root = Path({str(tmp_path)!r})
        services = EmoteServices(PackRepository(root / "empty"), Catalog(), SelectionStore(root / "state"), EventSpool(root / "events"), offline)
        emote.main(args=[], obj=services)
    """)
    child = PtyProcess(f"""
        import subprocess, sys, termios
        before = termios.tcgetattr(0)
        result = subprocess.run([sys.executable, "-B", "-c", {inner!r}], check=True)
        print("PARENT_MODES=" + str(before == termios.tcgetattr(0)), flush=True)
        print("PARENT_INPUT=" + sys.stdin.readline().rstrip("\\n"), flush=True)
    """)
    try:
        child.until(b"Choose [q]:")
        child.write(b"q\nnext host command survives process exit\n")
        child.until(b"PARENT_INPUT=next host command survives process exit")
        child.wait()
        assert b"PARENT_MODES=True" in child.output
    finally:
        child.close()


def test_watch_real_pty_synthetic_lifecycle_replay_failure_cancel_and_cleanup(tmp_path):
    spool = EventSpool(tmp_path / "events")
    assert spool.publish(_event(5, 0, Phase.START))
    assert spool.publish(_event(5, 1, Phase.COMPLETE))
    trace = tmp_path / "observed.jsonl"
    child = PtyProcess(f"""
        import json, os, sys, termios
        from pathlib import Path
        os.environ["TERM"] = "xterm-256color"
        os.environ["COLORTERM"] = "truecolor"
        os.environ.pop("NO_COLOR", None)
        os.environ.pop("OPENVEGAS_REDUCED_MOTION", None)
        from openvegas.emotes import commands
        from openvegas.emotes.resources import PackRepository, preview_catalog
        from openvegas.emotes.selection import SelectionStore
        from openvegas.emotes.spool import EventSpool
        root = Path({str(tmp_path)!r})
        def record(row):
            with (root / "observed.jsonl").open("a") as stream:
                stream.write(json.dumps(row) + "\\n")
        class ObservedController(commands.EmoteController):
            def handle(self, event):
                before = self.current_state
                accepted = super().handle(event)
                record({{"generation": event.generation, "sequence": event.sequence, "accepted": accepted, "before": before, "after": self.current_state}})
                return accepted
            def close(self):
                super().close()
                record({{"closed": self.current_state}})
        commands.EmoteController = ObservedController
        repository = PackRepository()
        services = commands.EmoteServices(repository, preview_catalog(repository), SelectionStore(root / "state"), EventSpool(root / "events"))
        before = termios.tcgetattr(0)
        commands.emote.main(args=["watch", "--session", "synthetic-pty", "--pack", "openvegas.pixel-courier", "--completion-pack", "openvegas.skyline-dunk"], obj=services, standalone_mode=False)
        print("MODES=" + str(before == termios.tcgetattr(0)), flush=True)
        size = os.get_terminal_size(1)
        print(f"SIZE={{size.columns}}x{{size.lines}}", flush=True)
        print("WATCH_RETURNED", flush=True)
        print("HOST_INPUT=" + sys.stdin.readline().rstrip("\\n"), flush=True)
    """)

    def rows():
        return (
            [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        )

    def send(event, count):
        assert spool.publish(event)
        deadline = time.monotonic() + 5
        while len(rows()) < count and time.monotonic() < deadline:
            child.read()
        assert len(rows()) >= count, child.output.decode(errors="replace")

    try:
        child.until(b"ready for the next turn")
        child.write(b"watch never consumes this line\n")
        # Old generations and queued completions are not eligible after readiness.
        assert spool.publish(_event(4, 0, Phase.START))
        send(_event(6, 1, Phase.COMPLETE), 1)
        events = [
            _event(7, 0, Phase.START),
            _event(7, 1, Phase.BUSY),
            _event(7, 2, Phase.PAUSE),
            _event(7, 3, Phase.RESUME),
            _event(7, 4, Phase.COMPLETE),
            _event(7, 4, Phase.COMPLETE),
            _event(8, 0, Phase.START),
            _event(8, 1, Phase.ERROR),
            _event(8, 2, Phase.COMPLETE),
            _event(9, 0, Phase.START),
            _event(9, 1, Phase.CANCEL),
            _event(9, 2, Phase.COMPLETE),
            _event(9, 3, Phase.EXIT),
        ]
        for count, event in enumerate(events, 2):
            send(event, count)
            if event.phase == Phase.PAUSE:
                import fcntl
                import struct
                import termios

                fcntl.ioctl(child.master, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 60, 0, 0))
                os.kill(child.pid, signal.SIGWINCH)
        child.until(b"WATCH_RETURNED")
        child.until(b"HOST_INPUT=watch never consumes this line")
        child.wait()
        handled = [row for row in rows() if "generation" in row]
        assert [row["generation"] for row in handled] == [6] + [e.generation for e in events]
        assert not handled[0]["accepted"] and handled[0]["after"] == "idle"
        assert (
            sum(row["before"] != "complete" and row["after"] == "complete" for row in handled) == 1
        )
        assert not handled[6]["accepted"]  # Duplicate successful event.
        assert [row["after"] for row in handled[1:5]] == ["active", "active", "paused", "active"]
        assert handled[9]["after"] == "error" and handled[12]["after"] == "cancelled"
        assert rows()[-1] == {"closed": "off"}
        assert b"MODES=True" in child.output
        assert b"SIZE=60x20" in child.output
        assert b"\x1b[?25l" in child.output and b"\x1b[?25h" in child.output
        assert b"\x1b[?1049h" not in child.output  # No alternate-screen takeover.
    finally:
        child.close()
