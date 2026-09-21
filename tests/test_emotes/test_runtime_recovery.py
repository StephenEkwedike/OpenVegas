"""Bounded spool/lease/supervision regressions. Only private temporary metadata."""

import asyncio
import os
import time

import pytest

from openvegas.emotes import EmoteController, Event, EventSpool, Phase, State
from openvegas.emotes.bridge import ChatEmoteBridge
from openvegas.emotes.spool import MAX_QUEUE, STALE_SECONDS, _locked
from tests.test_emotes.test_controller import event


def metadata(session, turn, generation, sequence, phase):
    return Event(
        "openvegas",
        session,
        turn,
        f"event-{generation}-{sequence}",
        phase,
        generation,
        sequence,
        "success" if phase == Phase.COMPLETE else None,
    )


@pytest.mark.parametrize("same_session", [True, False])
def test_128_unwatched_turns_cannot_permanently_fill_spool(tmp_path, same_session):
    spool = EventSpool(tmp_path / "events")
    for index in range(128):
        session = "old" if same_session else f"old-{index}"
        assert spool.publish(metadata(session, f"turn-{index}", index, 0, Phase.START))
        assert spool.publish(metadata(session, f"turn-{index}", index, 1, Phase.COMPLETE))
    assert len(list(spool.directory.glob("*.json"))) == MAX_QUEUE
    assert spool.publish(metadata("new", "new-turn", 1, 0, Phase.START))
    assert spool.publish(metadata("new", "new-turn", 1, 1, Phase.COMPLETE))
    assert len(list(spool.directory.glob("*.json"))) <= MAX_QUEUE
    got = spool.drain(source="openvegas", session_id="new")
    assert [e.phase for e in got] == [Phase.START, Phase.COMPLETE]
    # Eviction never strands a terminal without its queued START.
    for session in ["old"] if same_session else [f"old-{i}" for i in range(128)]:
        rows = spool.drain(source="openvegas", session_id=session)
        groups = {}
        for row in rows:
            groups.setdefault(row.key, []).append(row.phase)
        assert all(phases == [Phase.START, Phase.COMPLETE] for phases in groups.values())


def test_stale_sessions_and_abandoned_temp_files_cleanup_under_lock(tmp_path):
    spool = EventSpool(tmp_path / "events")
    assert spool.publish(metadata("old", "old-turn", 1, 0, Phase.START))
    temp = spool.directory / ("." + "a" * 32 + ".tmp")
    temp.write_bytes(b"{}")
    temp.chmod(0o600)
    stale = time.time() - STALE_SECONDS - 1
    for path in spool.directory.iterdir():
        if path.name != ".lock":
            os.utime(path, (stale, stale))
    with _locked(spool.directory):
        assert not spool.publish(metadata("new", "t", 1, 0, Phase.START))
        assert temp.exists()  # no cleanup outside the nonblocking lock
    assert spool.publish(metadata("new", "t", 1, 0, Phase.START))
    assert not temp.exists()
    assert spool.drain(source="openvegas", session_id="old") == []
    assert len(spool.drain(source="openvegas", session_id="new")) == 1


def test_busy_compaction_preserves_lifecycle_edges(tmp_path):
    spool = EventSpool(tmp_path / "events")
    assert spool.publish(metadata("s", "t", 1, 0, Phase.START))
    for sequence in range(1, 400):
        assert spool.publish(metadata("s", "t", 1, sequence, Phase.BUSY))
    assert len(list(spool.directory.glob("*.json"))) == 2
    assert spool.publish(metadata("s", "t", 1, 400, Phase.PAUSE))
    assert spool.publish(metadata("s", "t", 1, 401, Phase.RESUME))
    assert spool.publish(metadata("s", "t", 1, 402, Phase.BUSY))
    assert spool.publish(metadata("s", "t", 1, 403, Phase.COMPLETE))
    rows = spool.drain(source="openvegas", session_id="s")
    assert [e.phase for e in rows] == [
        Phase.START,
        Phase.PAUSE,
        Phase.RESUME,
        Phase.BUSY,
        Phase.COMPLETE,
    ]
    assert [e.sequence for e in rows] == sorted(e.sequence for e in rows)


def test_full_single_turn_prefers_real_cancel_over_repeated_start(tmp_path):
    spool = EventSpool(tmp_path / "events")
    for sequence in range(MAX_QUEUE):
        assert spool.publish(metadata("s", "t", 1, sequence, Phase.START))
    assert spool.publish(metadata("s", "t", 1, MAX_QUEUE, Phase.CANCEL))
    rows = spool.drain(source="openvegas", session_id="s")
    assert rows[0].phase == Phase.START
    assert rows[-1].phase == Phase.CANCEL
    assert len(rows) == MAX_QUEUE


@pytest.mark.parametrize("terminal", [Phase.COMPLETE, Phase.CANCEL])
def test_dropped_terminal_expires_without_success(pack, clock, terminal):
    consumer = EmoteController(pack, source="openvegas", session_id="s1", clock=clock)
    bridge = ChatEmoteBridge(
        "s1", publish=lambda e: False if e.phase == terminal else consumer.handle(e)
    )
    token = bridge.begin("turn1")
    clock.advance(5)
    assert bridge.active(turn=token)
    assert not (
        bridge.finish(True, turn=token) if terminal == Phase.COMPLETE else bridge.cancel(turn=token)
    )
    clock.advance(19.99)
    assert consumer.current_state == State.ACTIVE
    clock.advance(0.01)
    assert consumer.current_state == State.IDLE
    assert consumer.reason == "active_lease_expired"
    consumer.handle(event(Phase.COMPLETE, 100))
    consumer.handle(event(Phase.BUSY, 101))
    assert consumer.current_state == State.IDLE
    clock.advance(100)
    assert not consumer.tick()
    token = bridge.begin("turn2")
    assert token is not None and consumer.current_state == State.ACTIVE


def test_healthy_heartbeats_keep_phase_continuous_and_pause_needs_none(pack, clock):
    c = EmoteController(pack, source="openvegas", session_id="s1", clock=clock)
    c.handle(event(Phase.START, 0))
    for seq in range(1, 30):
        clock.advance(5)
        before = c.frame_index
        c.handle(event(Phase.BUSY, seq))
        assert c.current_state == State.ACTIVE and c.frame_index == before
        assert c._since == 0
    c.handle(event(Phase.PAUSE, 31))
    clock.advance(1000)
    assert c.current_state == State.PAUSED
    c.handle(event(Phase.RESUME, 32))
    clock.advance(19)
    assert c.current_state == State.ACTIVE


def test_duplicate_foreign_or_late_heartbeat_cannot_extend_lease(pack, clock):
    c = EmoteController(pack, source="openvegas", session_id="s1", clock=clock)
    c.handle(event(Phase.START, 0))
    clock.advance(19)
    assert not c.handle(event(Phase.BUSY, 1, session="foreign"))
    assert not c.handle(event(Phase.START, 0))
    clock.advance(1)
    c.handle(event(Phase.BUSY, 2))  # expiry checked before handling queued heartbeat
    assert c.current_state == State.IDLE


@pytest.mark.asyncio
async def test_supervision_heartbeats_pause_resume_and_no_stray_task(monkeypatch):
    import openvegas.emotes.bridge as module

    monkeypatch.setattr(module, "HEARTBEAT_SECONDS", 0.005)
    delivered = []
    bridge = ChatEmoteBridge("s", publish=lambda e: delivered.append(e) or True)
    turn = bridge.begin("t")
    async with bridge.supervise(turn=turn):
        async with bridge.supervise(turn=turn):
            await asyncio.sleep(0.022)
            assert len([e for e in delivered if e.phase == Phase.BUSY]) >= 2
            assert len([t for t in asyncio.all_tasks() if t.get_name() == "emote-heartbeat"]) == 1
        bridge.pause(turn=turn)
        count = len(delivered)
        await asyncio.sleep(0.015)
        assert len(delivered) == count
        bridge.resume(turn=turn)
        await asyncio.sleep(0.012)
    assert not bridge._supervised
    assert not any(t.get_name() == "emote-heartbeat" for t in asyncio.all_tasks())
    count = len(delivered)
    await asyncio.sleep(0.012)
    assert len(delivered) == count
    assert bridge.finish(True, turn=turn)  # finalization may follow context exit
    assert [e.sequence for e in delivered] == list(range(len(delivered)))


@pytest.mark.asyncio
async def test_supervision_publisher_failure_preserves_host_exception(monkeypatch):
    import openvegas.emotes.bridge as module

    monkeypatch.setattr(module, "HEARTBEAT_SECONDS", 0.001)

    def broken(_event):
        raise OSError("synthetic spool failure")

    bridge = ChatEmoteBridge("s", publish=broken)
    token = bridge.begin("t")
    with pytest.raises(RuntimeError, match="host failure"):
        async with bridge.supervise(turn=token):
            await asyncio.sleep(0.006)
            raise RuntimeError("host failure")
    assert not bridge._supervised
    assert not any(t.get_name() == "emote-heartbeat" for t in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_supervision_cancellation_and_stale_tokens_leave_no_task(monkeypatch):
    import openvegas.emotes.bridge as module

    monkeypatch.setattr(module, "HEARTBEAT_SECONDS", 0.005)
    bridge = ChatEmoteBridge("s", publish=lambda e: True)
    old = bridge.begin("old")
    current = bridge.begin("current")
    for token in (None, old):
        async with bridge.supervise(turn=token):
            assert not bridge._supervised
    entered = asyncio.Event()

    async def host():
        async with bridge.supervise(turn=current):
            entered.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(host())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not bridge._supervised
    assert not any(t.get_name() == "emote-heartbeat" for t in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_supervision_task_setup_failure_is_silent(monkeypatch):
    from types import SimpleNamespace

    import openvegas.emotes.bridge as module

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic task setup failure")

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(create_task=broken))
    bridge = ChatEmoteBridge("s", publish=lambda e: True)
    token = bridge.begin("t")
    async with bridge.supervise(turn=token):
        assert not bridge._supervised
    assert bridge.finish(True, turn=token)
