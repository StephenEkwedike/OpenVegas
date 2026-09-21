"""Mandatory installed-wheel Windows kernel gate, run explicitly (never skipped).

python -I -B tests/test_emotes/windows_state_native.py --junitxml /path/report.xml
Runs in an empty home outside the checkout, no dotenv/network. Not native UX
certification. Deliberately not named test_*: other operating systems cannot
execute Windows kernel tests and must not count them as passed or skipped.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


def install_guard():
    def guard(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"}:
            raise RuntimeError("Native state gate is offline")
        if event == "open" and isinstance(args[0], (str, bytes)):
            name = Path(os.fsdecode(args[0])).name.lower()
            if name == "env.md" or name == ".env" or name.startswith(".env."):
                raise RuntimeError("Native state gate must not read dotenv")

    sys.addaudithook(guard)


def main():
    if os.name != "nt":
        raise RuntimeError("This required native gate must run on Windows")
    if "--probe" in sys.argv:
        install_guard()
        import pytest

        import openvegas

        distribution = importlib.metadata.distribution("openvegas")
        installed = Path(distribution.locate_file("openvegas/__init__.py")).resolve()
        assert Path(openvegas.__file__).resolve() == installed
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
        assert not direct.get("dir_info", {}).get("editable")
        assert not installed.is_relative_to(Path(__file__).resolve().parents[2])
        args = [arg for arg in sys.argv[1:] if arg != "--probe"]
        return pytest.main(
            [
                "-q",
                "-ra",
                "-p",
                "no:cacheprovider",
                "--confcutdir=" + str(Path(__file__).parent),
                str(Path(__file__).resolve()),
                str(Path(__file__).with_name("test_selection_races.py")),
                *args,
            ]
        )
    with tempfile.TemporaryDirectory(prefix="openvegas-windows-state-") as temp:
        env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
        env.update(
            {
                "HOME": temp,
                "USERPROFILE": temp,
                "APPDATA": temp + "/appdata",
                "LOCALAPPDATA": temp + "/localappdata",
                "TMP": temp,
                "TEMP": temp,
                "PATH": os.defpath,
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "OPENVEGAS_TEST_MODE": "1",
                "OPENVEGAS_RUNTIME_ENV": "test",
                "OPENVEGAS_ENABLE_TOUCHID": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(Path(__file__).resolve()),
                "--probe",
                *sys.argv[1:],
            ],
            cwd=temp,
            env=env,
            timeout=180,
            check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())


import pytest

from openvegas.emotes import _windows_state as win
from openvegas.emotes.events import Event, Phase
from openvegas.emotes.runner import reserve_generation
from openvegas.emotes.selection import SelectionStore
from openvegas.emotes.spool import (
    EventSpool,
    _locked,
    _read,
    atomic_write,
    private_directory,
    state_names,
    state_stat,
    state_unlink,
)


def windows_tool(name):
    return str(Path(os.environ["SYSTEMROOT"]) / "System32" / name)


def test_native_protected_state_slots_and_generation(tmp_path):
    path = tmp_path / "private-\u00e9" / "state"
    selection = SelectionStore(path)
    selection.write_slots({"companion": "fixture.pack", "completion": "fixture.finish"})
    assert selection.read_slots() == {"companion": "fixture.pack", "completion": "fixture.finish"}
    with private_directory(path) as directory:
        assert isinstance(directory, win.WindowsDirectory)
        info = directory.api.private_info(directory.handle, directory=True)
        win.validate_security(info.security, info.user)
        win.validate_info(state_stat(directory, "selection.json"), directory=False)
    selection.disable()
    assert selection.read() is None
    events = EventSpool(tmp_path / "events")
    assert reserve_generation(events, source="native", session_id="s") == 1
    assert reserve_generation(events, source="native", session_id="s") == 2


def test_native_spool_roundtrip_and_content_bounds(tmp_path):
    queue = EventSpool(tmp_path / "events")
    start = Event("native", "session", "turn", "start", Phase.START, 0, 0)
    end = Event("native", "session", "turn", "end", Phase.COMPLETE, 0, 1, "success")
    assert queue.publish(end) and queue.publish(start)
    assert queue.drain(source="native", session_id="other") == []
    assert queue.drain(source="native", session_id="session") == [start, end]
    assert queue.drain(source="native", session_id="session") == []
    with _locked(queue.directory) as directory:
        assert not queue.publish(start)
        atomic_write(directory, "bound.json", b"x" * 256)
        with pytest.raises(OSError):
            _read(directory, "bound.json", 255)
        assert _read(directory, "bound.json", 256) == b"x" * 256
        info = state_stat(directory, "bound.json")
        state_unlink(directory, "bound.json", expected=info)


def test_native_cross_process_lock_nonblocking_and_release(tmp_path):
    path = tmp_path / "private"
    code = """import runpy,sys
gate=runpy.run_path(sys.argv[1]); gate['install_guard']()
from openvegas.emotes.spool import _locked
from pathlib import Path
try:
 with _locked(Path(sys.argv[2])): pass
except BlockingIOError: sys.exit(23)
"""
    command = [sys.executable, "-I", "-B", "-c", code, __file__, str(path)]
    with _locked(path):
        child = subprocess.run(command, timeout=10, capture_output=True, check=False)
        assert child.returncode == 23, child.stderr.decode(errors="replace")
    child = subprocess.run(command, timeout=10, capture_output=True, check=False)
    assert child.returncode == 0, child.stderr.decode(errors="replace")


def test_native_refuses_unsafe_acl_without_repair(tmp_path):
    path = tmp_path / "private"
    selection = SelectionStore(path)
    selection.write("fixture.pack")
    command = [windows_tool("icacls.exe"), str(path), "/grant", "*S-1-1-0:(RX)"]
    result = subprocess.run(command, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    with pytest.raises(OSError):
        selection.read()
    with pytest.raises(OSError):
        selection.disable()


def test_native_unsafe_file_acl_rejected(tmp_path):
    path = tmp_path / "private"
    selection = SelectionStore(path)
    selection.write("fixture.pack")
    result = subprocess.run(
        [
            windows_tool("icacls.exe"),
            str(path / "selection.json"),
            "/grant",
            "*S-1-1-0:(R)",
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    with pytest.raises(OSError):
        selection.read()
    with pytest.raises(OSError):
        selection.disable()


def test_native_hardlinks_fail_without_deleting_external_content(tmp_path):
    path = tmp_path / "private"
    selection = SelectionStore(path)
    selection.write("fixture.pack")
    external = tmp_path / "outside.json"
    os.link(path / "selection.json", external)
    before = external.read_bytes()
    with pytest.raises(OSError):
        selection.read()
    with pytest.raises(OSError):
        selection.disable()
    with private_directory(path) as directory, pytest.raises(OSError):
        state_unlink(directory, "selection.json")
    assert external.read_bytes() == before


@pytest.mark.parametrize("nested", [False, True])
def test_native_junction_root_or_ancestor_rejected(tmp_path, nested):
    real, link = tmp_path / "real", tmp_path / "junction"
    SelectionStore(real / "state").write("fixture.pack")
    result = subprocess.run(
        [
            windows_tool("cmd.exe"),
            "/d",
            "/c",
            "mklink",
            "/J",
            str(link),
            str(real),
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    try:
        with pytest.raises(OSError), private_directory(link / "state" if nested else link):
            pytest.fail("Junction accepted")
    finally:
        link.rmdir()


def test_native_file_symlink_rejected_no_privilege_skip(tmp_path):
    path = tmp_path / "private"
    selection = SelectionStore(path)
    selection.write("fixture.pack")
    target = path / "selection.json"
    target.rename(path / "real.json")
    target.symlink_to(path / "real.json")
    with pytest.raises(OSError):
        selection.read()
    with pytest.raises(OSError):
        selection.disable()


def test_native_parent_retained_read_and_atomic_replace(tmp_path):
    path = tmp_path / "private"
    with _locked(path) as directory:
        atomic_write(directory, "state.json", b"first")
        with pytest.raises(OSError):
            path.rename(tmp_path / "moved")
        old = state_stat(directory, "state.json")
        atomic_write(directory, "state.json", b"second")
        assert _read(directory, "state.json", 6) == b"second"
        assert state_stat(directory, "state.json").st_ino != old.st_ino
        with pytest.raises(OSError):
            state_unlink(directory, "state.json", expected=old)
        assert set(state_names(directory, limit=2)) == {".lock", "state.json"}
        with pytest.raises(OSError):
            state_names(directory, limit=1)
    path.rename(tmp_path / "moved")


def test_native_default_paths_use_only_isolated_profile():
    from openvegas.emotes.spool import default_state_dir

    assert default_state_dir().is_relative_to(Path(os.environ["USERPROFILE"]))
    selection = SelectionStore()
    selection.write("fixture.pack")
    assert selection.read() == "fixture.pack"


def native_library(tmp_path, **kwargs):
    from openvegas.emotes.remote import RemoteLibrary

    return RemoteLibrary(
        SimpleNamespace(), tmp_path / "cache", SelectionStore(tmp_path / "selection"), **kwargs
    )


def native_cache_name(index):
    return f"remote-{index:064x}.json"


def test_native_cache_publication_eviction_and_stale_recovery(tmp_path):
    library = native_library(tmp_path, cache_max_files=1, cache_max_bytes=20)
    library._cache_bundle(native_cache_name(1), b"first")
    library._cache_bundle(native_cache_name(2), b"second")
    temporary = "." + "a" * 32 + ".tmp"
    with _locked(library.cache) as directory:
        assert set(state_names(directory)) == {".lock", native_cache_name(2)}
        assert _read(directory, native_cache_name(2), 6) == b"second"
        atomic_write(directory, temporary, b"interrupted")
    old = time.time() - 90
    os.utime(library.cache / temporary, (old, old))
    library._cache_bundle(native_cache_name(2), b"replacement")
    with _locked(library.cache) as directory:
        assert set(state_names(directory)) == {".lock", native_cache_name(2)}
        assert _read(directory, native_cache_name(2), 11) == b"replacement"
        info = state_stat(directory, native_cache_name(2))
        win.validate_info(info, directory=False)
    assert not library.authorize("fixture.pack")  # Data alone is never ownership.


@pytest.mark.parametrize("unsafe", ["hardlink", "acl", "symlink"])
def test_native_cache_rejects_unsafe_eviction_without_external_writes(tmp_path, unsafe):
    library = native_library(tmp_path, cache_max_files=1)
    name = native_cache_name(1)
    library._cache_bundle(name, b"external-content")
    target = library.cache / name
    outside = tmp_path / "external.json"
    if unsafe == "hardlink":
        os.link(target, outside)
    elif unsafe == "symlink":
        target.rename(outside)
        target.symlink_to(outside)
    else:
        result = subprocess.run(
            [windows_tool("icacls.exe"), str(target), "/grant", "*S-1-1-0:(R)"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        outside = target
    with pytest.raises((OSError, ValueError)):
        library._cache_bundle(native_cache_name(2), b"new")
    assert outside.read_bytes() == b"external-content"
    assert not (library.cache / native_cache_name(2)).exists()


def test_native_cache_scan_busy_and_unknown_entries_fail_closed(tmp_path):
    from openvegas.emotes.remote import RemoteError

    library = native_library(tmp_path, cache_scan_limit=2)
    library._cache_bundle(native_cache_name(1), b"first")
    with _locked(library.cache) as directory:
        with pytest.raises(BlockingIOError):
            library._cache_bundle(native_cache_name(2), b"second")
        atomic_write(directory, "unrelated.txt", b"preserve")
    with pytest.raises(RemoteError, match="scan"):
        library._cache_bundle(native_cache_name(2), b"second")
    library.cache_scan_limit = 3
    with pytest.raises(RemoteError, match="Unexpected"):
        library._cache_bundle(native_cache_name(2), b"second")
    assert (library.cache / "unrelated.txt").read_bytes() == b"preserve"


def test_native_cache_eviction_rejects_changed_expected_identity(tmp_path):
    from openvegas.emotes.remote import RemoteError

    library = native_library(tmp_path)
    name = native_cache_name(1)
    library._cache_bundle(name, b"first")
    with _locked(library.cache) as directory:
        old = state_stat(directory, name)
        atomic_write(directory, name, b"replacement")
        with pytest.raises(RemoteError, match="changed"):
            library._unlink_cached(directory, name, old)
        assert _read(directory, name, 11) == b"replacement"


def test_native_watcher_cleanup_preserves_other_sessions_and_locks(tmp_path):
    from openvegas.emotes.commands import _watch_boundary

    queue = EventSpool(tmp_path / "events")
    own = Event("native", "session", "turn", "e0", Phase.START, 4, 0)
    other = Event("native", "other", "turn", "e1", Phase.START, 7, 0)
    assert queue.publish(own) and queue.publish(other)
    with _locked(queue.directory), pytest.raises(BlockingIOError):
        _watch_boundary(queue, "native", "session")
    assert _watch_boundary(queue, "native", "session") == 4
    assert queue.drain(source="native", session_id="session") == []
    assert queue.drain(source="native", session_id="other") == [other]


def test_native_watcher_cleanup_rejects_hardlinked_event(tmp_path):
    from openvegas.emotes.commands import _watch_boundary

    queue = EventSpool(tmp_path / "events")
    event = Event("native", "session", "turn", "e0", Phase.START, 4, 0)
    assert queue.publish(event)
    target = next(queue.directory.glob("*.json"))
    outside = tmp_path / "outside-event.json"
    os.link(target, outside)
    with pytest.raises(OSError):
        _watch_boundary(queue, "native", "session")
    assert outside.read_bytes() == event.to_bytes() and target.exists()


def test_native_doctor_probes_current_state_without_consuming_events(tmp_path):
    from click.testing import CliRunner

    from openvegas.emotes.commands import EmoteServices, emote

    queue = EventSpool(tmp_path / "events")
    selection = SelectionStore(tmp_path / "selection")
    selection.write("fixture.pack")
    revision = selection.revision()
    event = Event("native", "session", "turn", "e0", Phase.START, 4, 0)
    assert queue.publish(event)
    services = EmoteServices(SimpleNamespace(names=list), None, selection, queue)
    result = CliRunner().invoke(emote, ["doctor"], obj=services)
    assert result.exit_code == 0, result.output
    assert result.output.count("local lock/read/write/delete probe passed") == 2
    assert "Windows ACL/handle backend" in result.output
    assert "do not certify native terminal UX" in result.output
    assert selection.revision() == revision
    assert queue.drain(source="native", session_id="session") == [event]
    assert not list(queue.directory.glob("*.tmp"))
    with _locked(queue.directory):
        result = CliRunner().invoke(emote, ["doctor"], obj=services)
    assert result.exit_code == 1 and "busy; probe not completed" in result.output
