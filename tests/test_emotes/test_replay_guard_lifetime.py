"""Offline replay tests, no asyncio/plugin dependency; state lives in tmp_path.

Unmocked tests use the actual platform's secure state backend, including Windows.
Only explicitly named fakehandles tests substitute Windows APIs. POSIX rename
interruption/mode bits are not evidence of native Windows behavior.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from openvegas.emotes import EmoteController, Phase, State
from openvegas.emotes import spool as spool_module
from openvegas.emotes.owned_stream import (
    CodexLifecycleMetadata,
    OwnedCodexStream,
    OwnedStreamAuthorization,
    OwnedStreamRecord,
)
from openvegas.emotes.spool import EventSpool, _locked, atomic_write, state_stat


def observer(spool, connection, *, session="watch", owner="host"):
    auth = OwnedStreamAuthorization(
        owner, connection, "thread", session, (0, 153, 4),
        owns_stream=True, ordered=True, metadata_only=True,
    )
    return OwnedCodexStream(auth, enabled=True, spool=spool)


def send(stream, connection, ordinal, *, turn="logical-turn", complete=False, owner="host"):
    metadata = CodexLifecycleMetadata(
        "thread", turn, "turn/completed" if complete else "turn/started",
        "completed" if complete else "inProgress",
    )
    return stream.deliver(OwnedStreamRecord(owner, connection, ordinal, metadata))


def test_completed_turn_cannot_replay_after_empty_spool_recreation(tmp_path, pack, clock):
    spool = EventSpool(tmp_path / "events")
    original = observer(spool, "connection-1")
    controller = EmoteController(pack, source="codex", session_id="watch", clock=clock)
    assert send(original, "connection-1", 0)
    assert send(original, "connection-1", 1, complete=True)
    for event in spool.drain(source="codex", session_id="watch"):
        controller.handle(event)
    assert controller.current_state == State.COMPLETE
    original.close()
    assert spool.drain(source="codex", session_id="watch") == []

    recreated = observer(EventSpool(spool.directory), "connection-2")
    fresh_controller = EmoteController(pack, source="codex", session_id="watch", clock=clock)
    replay_start = send(recreated, "connection-2", 0)
    replay_complete = send(recreated, "connection-2", 1, complete=True)
    for event in spool.drain(source="codex", session_id="watch"):
        fresh_controller.handle(event)
    assert fresh_controller.current_state == State.IDLE
    assert not replay_start and not replay_complete
    assert send(recreated, "connection-2", 2, turn="new-turn")
    assert send(recreated, "connection-2", 3, turn="new-turn", complete=True)


def test_two_live_observers_cannot_publish_two_completions(tmp_path):
    spool = EventSpool(tmp_path / "events")
    older, newer = observer(spool, "old"), observer(spool, "new")
    assert send(older, "old", 0)
    assert send(newer, "new", 0)
    assert send(newer, "new", 1, complete=True)
    assert not send(older, "old", 1, complete=True)
    assert older.reason == "completion_replayed"
    rows = spool.drain(source="codex", session_id="watch")
    assert [e.phase for e in rows].count(Phase.COMPLETE) == 1


def test_old_receipts_cannot_advance_new_observer_or_reset_completed_guard(tmp_path):
    spool = EventSpool(tmp_path / "events")
    old = observer(spool, "old")
    assert send(old, "old", 0)
    assert send(old, "old", 1, complete=True)
    spool.drain(source="codex", session_id="watch")
    new = observer(spool, "new", owner="new-host")
    assert not send(new, "old", 0, turn="fresh")
    assert not send(new, "new", 0, turn="fresh", owner="host")
    assert not send(new, "new", 0, owner="new-host")
    assert not send(new, "new", 1, complete=True, owner="new-host")
    assert send(new, "new", 2, turn="fresh", owner="new-host")
    assert send(new, "new", 3, turn="fresh", complete=True, owner="new-host")
    assert {e.turn_id for e in spool.drain(source="codex", session_id="watch")} == {"fresh"}


def test_early_completion_survives_recreation_without_synthetic_start(tmp_path):
    spool = EventSpool(tmp_path / "events")
    early = observer(spool, "early")
    assert not send(early, "early", 0, complete=True)
    assert early.enabled
    assert len(json.loads(ledger(spool).read_bytes())["completed"]) == 1
    new = observer(spool, "new")
    assert not send(new, "new", 0)
    assert not send(new, "new", 1, complete=True)
    assert not spool.directory.exists()
    assert send(new, "new", 2, turn="different-turn")
    assert send(new, "new", 3, turn="different-turn", complete=True)


def test_guard_does_not_conflate_sources_sessions_or_unfinished_turns(tmp_path):
    spool = EventSpool(tmp_path / "events")
    for connection in ("first", "second"):
        stream = observer(spool, connection)
        assert send(stream, connection, 0)
        stream.close()
        spool.drain(source="codex", session_id="watch")
    stream = observer(spool, "third")
    assert send(stream, "third", 0)
    assert send(stream, "third", 1, complete=True)
    other = observer(spool, "fourth", session="other")
    assert send(other, "fourth", 0)
    assert send(other, "fourth", 1, complete=True)
    assert spool.completion_allowed(source="different", session_id="watch", turn_id="logical-turn")


@pytest.mark.parametrize("raises", [False, True])
def test_failed_completion_publication_is_reserved_not_retried(tmp_path, monkeypatch, raises):
    spool = EventSpool(tmp_path / "events")
    stream = observer(spool, "old")
    assert send(stream, "old", 0)
    spool.drain(source="codex", session_id="watch")
    def fail_publish(_):
        if raises:
            raise RuntimeError("publication unavailable")
        return False

    monkeypatch.setattr(spool, "publish", fail_publish)
    assert not send(stream, "old", 1, complete=True)
    assert stream.reason == "publication_failed"
    new = observer(EventSpool(spool.directory), "new")
    assert not send(new, "new", 0)
    assert not send(new, "new", 1, complete=True)
    assert spool.drain(source="codex", session_id="watch") == []


def ledger(spool):
    return spool.directory.parent / "completion-guards" / spool_module.COMPLETION_FILE


@pytest.fixture
def wall(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(spool_module, "time", SimpleNamespace(time=lambda: clock.now))
    return clock


def identity(turn="turn", session="session"):
    return {"source": "codex", "session_id": session, "turn_id": turn}


def claim(spool, **key):
    assert spool.completion_allowed(**key)
    assert spool.completion_allowed(**key, claim=True)


def private_file(path, data):
    # Use the real ACL-aware backend on Windows, not chmod as a substitute.
    with _locked(path.parent) as directory:
        atomic_write(directory, path.name, data)


def make_symlink(path, target):
    try:
        path.symlink_to(target)
    except OSError:
        if os.name == "nt" and os.environ.get("EMOTE_REQUIRE_WINDOWS_SYMLINKS") != "1":
            pytest.skip("Native Windows symlink privilege unavailable; CI must require it")
        raise


def process_environment(tmp_path):
    # Windows needs its OS directory to load system components. Never inherit
    # provider/auth settings, PYTHONPATH, or the user's home into child fixtures.
    return {
        **{key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ},
        "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
        "TEMP": str(tmp_path), "TMP": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


@pytest.mark.parametrize("damage", ["corrupt", "oversized", "schema", "duplicates",
                                    "permissions", "symlink", "hardlink", "missing"])
def test_missing_or_unsafe_authority_cannot_publish_completion(tmp_path, damage, capsys):
    if damage == "permissions" and os.name != "posix":
        pytest.skip("POSIX mode-bit test; Windows ACL tests are separate")
    spool = EventSpool(tmp_path / "events")
    stream = observer(spool, "old")
    assert send(stream, "old", 0)
    spool.drain(source="codex", session_id="watch")
    path = ledger(spool)
    if damage == "corrupt":
        path.write_text("not json")
    elif damage == "oversized":
        path.write_bytes(b"x" * (spool_module.MAX_COMPLETION_STATE_BYTES + 1))
    elif damage == "schema":
        path.write_text('{"schema":true,"completed":[]}')
    elif damage == "duplicates":
        key = '"' + "a" * 64 + '":0'
        path.write_text('{"schema":1,"last_seen":0,"completed":{' + key + ',' + key + '}}')
    elif damage == "permissions":
        path.chmod(0o644)
    elif damage == "symlink":
        target = tmp_path / "outside"
        target.write_bytes(path.read_bytes())
        target.chmod(0o600)
        path.unlink()
        make_symlink(path, target)
    elif damage == "hardlink":
        os.link(path, tmp_path / "outside")
    else:
        path.unlink()
    before = path.read_bytes() if path.exists() else None
    assert not send(stream, "old", 1, complete=True)
    assert stream.reason == "replay_guard_unavailable"
    assert spool.drain(source="codex", session_id="watch") == []
    assert (path.read_bytes() if path.exists() else None) == before
    assert capsys.readouterr() == ("", "")


def test_locked_guard_disables_without_waiting_or_later_success(tmp_path):
    spool = EventSpool(tmp_path / "events")
    stream = observer(spool, "old")
    assert send(stream, "old", 0)
    spool.drain(source="codex", session_id="watch")
    with _locked(spool.directory.parent / "completion-guards"):
        assert not send(stream, "old", 1, complete=True)
    assert stream.reason == "replay_guard_unavailable"
    assert not send(stream, "old", 2, complete=True)
    assert spool.drain(source="codex", session_id="watch") == []


def test_full_recent_capacity_recovers_only_at_expiry(tmp_path, wall):
    spool = EventSpool(tmp_path / "events")
    claim(spool, **identity())
    path = ledger(spool)
    state = json.loads(path.read_bytes())
    for i in range(spool_module.MAX_COMPLETED_TURNS - 1):
        state["completed"][f"{i:064x}"] = wall.now
    path.write_text(json.dumps(state))
    assert len(state["completed"]) == 4096
    assert spool.completion_allowed(**identity("new", "new-session")) is None
    assert spool.completion_allowed(**identity()) is False
    wall.now += spool_module.COMPLETION_RETENTION_SECONDS - 0.001
    assert spool.completion_allowed(**identity("new", "new-session"), claim=True) is None
    assert json.loads(path.read_bytes())["completed"] == state["completed"]
    wall.now += 0.001
    claim(spool, **identity("new", "new-session"))
    assert len(json.loads(path.read_bytes())["completed"]) == 1
    assert len(list(path.parent.glob("*.json"))) == 1


def test_concurrent_claims_private_digest_only_and_no_event_id_dependency(tmp_path):
    spool = EventSpool(tmp_path / "events")
    identity = {"source": "codex", "session_id": "private-session", "turn_id": "private-turn"}
    assert spool.completion_allowed(**identity)
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: spool.completion_allowed(**identity, claim=True), range(8)))
    assert results.count(True) == 1
    assert spool.completion_allowed(**identity) is False
    path = ledger(spool)
    with _locked(path.parent) as directory:
        state_stat(directory, path.name)  # Real ownership/ACL validation on either backend.
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not any(value in path.read_text() or value in path.name for value in identity.values())
    state = json.loads(path.read_bytes())
    assert set(state) == {"schema", "last_seen", "completed"}
    assert len(state["completed"]) == 1


@pytest.mark.parametrize("during_completion", [False, True])
def test_guard_interface_exception_never_leaks_into_host(tmp_path, monkeypatch, during_completion):
    spool = EventSpool(tmp_path / "events")
    stream = observer(spool, "connection")
    if during_completion:
        assert send(stream, "connection", 0)
        spool.drain(source="codex", session_id="watch")

    def broken_guard(**kwargs):
        raise RuntimeError("private interface failure")

    monkeypatch.setattr(spool, "completion_allowed", broken_guard)
    assert not send(stream, "connection", int(during_completion), complete=during_completion)
    assert stream.reason == "replay_guard_unavailable"
    assert spool.drain(source="codex", session_id="watch") == []


def test_publish_only_spool_cannot_bypass_missing_guard():
    published = []
    stream = observer(SimpleNamespace(publish=lambda event: published.append(event) or True), "c")
    assert not send(stream, "c", 0)
    assert not send(stream, "c", 1, complete=True)
    assert stream.reason == "replay_guard_unavailable"
    assert published == []


class RaisingTruthValue:
    def __bool__(self):
        raise AssertionError("Guard result truthiness must never be evaluated")


@pytest.mark.parametrize("result", [1, [], RaisingTruthValue()], ids=["integer", "list", "raising-bool"])
@pytest.mark.parametrize("during_completion", [False, True])
def test_unsupported_guard_result_fails_neutral_without_truthiness(
    tmp_path, monkeypatch, result, during_completion,
):
    spool = EventSpool(tmp_path / "events")
    stream = observer(spool, "connection")
    if during_completion:
        assert send(stream, "connection", 0)
        spool.drain(source="codex", session_id="watch")
    monkeypatch.setattr(spool, "completion_allowed", lambda **kwargs: result)
    assert not send(stream, "connection", int(during_completion), complete=during_completion)
    assert stream.reason == "replay_guard_unavailable"
    assert spool.drain(source="codex", session_id="watch") == []


def test_exact_retention_boundary_and_live_instance_guard_never_expires(tmp_path, wall):
    spool = EventSpool(tmp_path / "events")
    live = observer(spool, "original")
    assert send(live, "original", 0)
    assert send(live, "original", 1, complete=True)
    spool.drain(source="codex", session_id="watch")
    key = identity("logical-turn", "watch")
    wall.now += spool_module.COMPLETION_RETENTION_SECONDS - 0.001
    assert spool.completion_allowed(**key) is False
    wall.now += 0.001
    assert spool.completion_allowed(**key) is True
    assert not send(live, "original", 2)
    assert not send(live, "original", 3, complete=True)
    assert spool.drain(source="codex", session_id="watch") == []
    # Only a new observer outside the explicitly bounded window can accept it.
    new = observer(spool, "new")
    assert send(new, "new", 0)
    assert send(new, "new", 1, complete=True)


@pytest.mark.parametrize("time_value", [999.0, -1, float("nan"), float("inf"), True, "1000"])
def test_clock_rollback_or_invalid_clock_preserves_claims_and_fails_neutral(tmp_path, wall, time_value):
    spool = EventSpool(tmp_path / "events")
    claim(spool, **identity())
    before = ledger(spool).read_bytes()
    wall.now = time_value
    assert spool.completion_allowed(**identity("new"), claim=True) is None
    assert ledger(spool).read_bytes() == before
    wall.now = 1000.0
    assert spool.completion_allowed(**identity()) is False


def test_more_than_64_sessions_use_one_ledger_without_session_exhaustion(tmp_path, wall):
    spool = EventSpool(tmp_path / "events")
    for i in range(70):
        claim(spool, **identity(session=f"session-{i}"))
    path = ledger(spool)
    assert len(json.loads(path.read_bytes())["completed"]) == 70
    assert sorted(p.name for p in path.parent.iterdir()) == [".lock", spool_module.COMPLETION_FILE]


def test_observed_clock_high_water_survives_nonclaim_checks(tmp_path, wall):
    spool = EventSpool(tmp_path / "events")
    claim(spool, **identity())
    wall.now += 10
    assert spool.completion_allowed(**identity("new")) is True
    wall.now -= 1
    assert spool.completion_allowed(**identity("new"), claim=True) is None


def test_valid_orphan_temp_removed_under_lock_without_losing_committed_claim(tmp_path, wall):
    spool = EventSpool(tmp_path / "events")
    claim(spool, **identity())
    path = ledger(spool)
    before = path.read_bytes()
    temp = path.parent / ("." + "a" * 32 + ".tmp")
    private_file(temp, b'{"interrupted":')
    with _locked(path.parent):
        assert spool.completion_allowed(**identity()) is None
        assert temp.exists()
    assert spool.completion_allowed(**identity()) is False
    assert not temp.exists()
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["unknown", "symlink", "hardlink", "permissions", "directory", "oversized"])
def test_unsafe_inventory_preserved_before_any_orphan_cleanup(tmp_path, wall, damage):
    if damage == "permissions" and os.name != "posix":
        pytest.skip("POSIX mode-bit test; Windows ACL tests are separate")
    spool = EventSpool(tmp_path / "events")
    claim(spool, **identity())
    path = ledger(spool)
    before = path.read_bytes()
    safe = path.parent / ("." + "a" * 32 + ".tmp")
    private_file(safe, b"partial")
    bad = path.parent / ("." + "b" * 32 + ".tmp")
    if damage == "unknown":
        bad = path.parent / "unrelated.txt"
        bad.write_bytes(b"keep")
    elif damage == "symlink":
        make_symlink(bad, path)
    elif damage == "hardlink":
        os.link(path, bad)
    elif damage == "directory":
        bad.mkdir(mode=0o700)
    else:
        bad.write_bytes(b"x" * (spool_module.MAX_COMPLETION_STATE_BYTES + 1)
                        if damage == "oversized" else b"keep")
        bad.chmod(0o644 if damage == "permissions" else 0o600)
    assert spool.completion_allowed(**identity("new")) is None
    assert safe.exists() and bad.exists()
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["overflow", "future", "bad-hash", "bad-stamp"])
def test_malformed_or_overflow_ledger_is_not_pruned_or_reset(tmp_path, wall, damage):
    spool = EventSpool(tmp_path / "events")
    claim(spool, **identity())
    path = ledger(spool)
    state = json.loads(path.read_bytes())
    if damage == "overflow":
        state["completed"] = {f"{i:064x}": 0 for i in range(4097)}
    elif damage == "future":
        state["completed"]["a" * 64] = wall.now + 1
    elif damage == "bad-hash":
        state["completed"]["raw-private-id"] = 0
    else:
        state["completed"]["a" * 64] = True
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    assert spool.completion_allowed(**identity("new")) is None
    assert path.read_bytes() == before


def test_real_crossprocess_claims_have_one_winner(tmp_path):
    spool = EventSpool(tmp_path / "events")
    assert spool.completion_allowed(**identity())
    code = """
import json, sys
sys.path.insert(0, sys.argv[1])
from openvegas.emotes.spool import EventSpool
spool = EventSpool(sys.argv[2])
print('ready', flush=True)
sys.stdin.readline()
print(json.dumps(spool.completion_allowed(source='codex', session_id='session', turn_id='turn', claim=True)), flush=True)
"""
    processes = []
    try:
        for _ in range(4):
            processes.append(subprocess.Popen(
                [sys.executable, "-c", code, str(Path(__file__).parents[2]), str(spool.directory)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=tmp_path, env=process_environment(tmp_path),
            ))
        for process in processes:
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            process.stdin.write("claim\n")
            process.stdin.flush()
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0 and stderr == ""
            results.append(json.loads(stdout))
        assert results.count(True) == 1
        assert spool.completion_allowed(**identity()) is False
        assert len(json.loads(ledger(spool).read_bytes())["completed"]) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX rename interruption; Windows uses fake handles")
def test_process_exit_before_atomic_rename_recovers_orphan_not_committed_claims(tmp_path):
    spool = EventSpool(tmp_path / "events")
    claim(spool, **identity())
    committed = json.loads(ledger(spool).read_bytes())["completed"]
    code = """
import os, sys
sys.path.insert(0, sys.argv[1])
from openvegas.emotes import spool
def exit_before_rename(*args, **kwargs):
    os._exit(73)
spool.os.rename = exit_before_rename
spool.EventSpool(sys.argv[2]).completion_allowed(source='codex', session_id='session', turn_id='uncommitted', claim=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(Path(__file__).parents[2]), str(spool.directory)],
        cwd=tmp_path, env=process_environment(tmp_path),
        capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 73 and result.stdout == result.stderr == b""
    assert len(list(ledger(spool).parent.glob(".*.tmp"))) == 1
    assert json.loads(ledger(spool).read_bytes())["completed"] == committed
    assert spool.completion_allowed(**identity()) is False
    assert not list(ledger(spool).parent.glob(".*.tmp"))
    assert json.loads(ledger(spool).read_bytes())["completed"] == committed
    claim(spool, **identity("uncommitted"))


def test_two_consumers_of_one_queue_cannot_both_celebrate(tmp_path, pack, clock):
    spool = EventSpool(tmp_path / "events")
    stream = observer(spool, "connection")
    assert send(stream, "connection", 0)
    assert send(stream, "connection", 1, complete=True)
    owners = [EmoteController(pack, source="codex", session_id="watch", clock=clock)
              for _ in range(2)]

    def consume(owner):
        for event in EventSpool(spool.directory).drain(source="codex", session_id="watch"):
            owner.handle(event)
        return owner.current_state

    with ThreadPoolExecutor(max_workers=2) as workers:
        states = list(workers.map(consume, owners))
    assert states.count(State.COMPLETE) == 1
    assert states.count(State.IDLE) == 1


def test_supported_wholeprocess_recreation_uses_distinct_logical_turns(tmp_path):
    from openvegas.emotes.runner import run_command

    spool = EventSpool(tmp_path / "events")
    completed = []
    for _ in range(2):
        assert run_command([sys.executable, "-c", "pass"], session_id="same", spool=spool) == 0
        rows = spool.drain(source="openvegas", session_id="same")
        assert [event.phase for event in rows] == [Phase.START, Phase.COMPLETE]
        completed.append(rows[-1])
        assert spool.drain(source="openvegas", session_id="same") == []
    assert completed[0].key != completed[1].key
    assert completed[1].generation > completed[0].generation
    assert not ledger(spool).exists()  # No new policy imposed on unrelated producers.


@pytest.fixture
def windows_guard(monkeypatch):
    from openvegas.emotes import _windows_state as win
    from tests.test_emotes.test_windows_state import FakeAPI

    api = FakeAPI()
    monkeypatch.setattr(win, "WindowsStateAPI", lambda: api)

    @contextmanager
    def directory(path):
        with win.private_windows_directory("C:\\" + path.name) as opened:
            yield opened

    monkeypatch.setattr(spool_module, "private_directory", directory)
    return api


def test_windows_fakehandles_guard_recreation_cleanup_lock_and_expiry(tmp_path, wall, windows_guard):
    spool = EventSpool(tmp_path / "events")
    claim(spool, **identity())
    path = ledger(spool)
    temp = "." + "a" * 32 + ".tmp"
    windows_guard.nodes[(path.parent.name, temp)] = windows_guard.node(data=b"interrupted")
    recreated = EventSpool(spool.directory)
    with _locked(path.parent):
        assert recreated.completion_allowed(**identity()) is None
    assert recreated.completion_allowed(**identity()) is False
    assert (path.parent.name, temp) not in windows_guard.nodes
    wall.now += spool_module.COMPLETION_RETENTION_SECONDS
    claim(recreated, **identity())
    assert not windows_guard.locks
    assert set(windows_guard.handles) == set(windows_guard.closed)


@pytest.mark.parametrize("damage", ["acl", "hardlink", "reparse", "unknown"])
def test_windows_fakehandles_unsafe_orphans_never_deleted(tmp_path, wall, windows_guard, damage):
    spool = EventSpool(tmp_path / "events")
    claim(spool, **identity())
    path = ledger(spool)
    safe = (path.parent.name, "." + "a" * 32 + ".tmp")
    bad = (path.parent.name, "unknown" if damage == "unknown" else "." + "b" * 32 + ".tmp")
    windows_guard.nodes[safe] = windows_guard.node(data=b"partial")
    node = windows_guard.node(data=b"keep")
    windows_guard.nodes[bad] = node
    if damage == "acl":
        node["security"] = replace(node["security"], protected=False)
    elif damage == "hardlink":
        node["links"] = 2
    elif damage == "reparse":
        node["attrs"] = 0x400
    committed = windows_guard.nodes[(path.parent.name, path.name)]["data"]
    assert spool.completion_allowed(**identity("new")) is None
    assert safe in windows_guard.nodes and bad in windows_guard.nodes
    assert windows_guard.nodes[(path.parent.name, path.name)]["data"] == committed
    assert not windows_guard.locks
