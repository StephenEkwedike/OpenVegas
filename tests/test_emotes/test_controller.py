import pytest

from openvegas.emotes import EmoteController, Event, Phase, State


def event(
    phase,
    seq,
    *,
    turn="turn1",
    generation=1,
    source="openvegas",
    session="s1",
    eid=None,
):
    return Event(
        source,
        session,
        turn,
        eid or f"event-{generation}-{seq}",
        phase,
        generation,
        seq,
        "success" if phase == Phase.COMPLETE else None,
    )


def test_busy_phase_ten_loops_pause_resume_and_completion(pack, clock):
    requests = []
    c = EmoteController(
        pack,
        source="openvegas",
        session_id="s1",
        clock=clock,
        request_invalidation=lambda: requests.append(clock.now),
    )
    c.handle(event(Phase.START, 0))
    for sequence in range(1, 101):
        clock.advance(0.031)
        index = c.frame_index
        assert c.handle(event(Phase.BUSY, sequence * 2 - 1))
        assert c.handle(event(Phase.START, sequence * 2))
        assert c.frame_index == index
    # Use authoritative sequence above all previous values.
    clock.now = 3.11
    before = c.frame_index
    c.handle(event(Phase.PAUSE, 2000))
    assert c.current_state == State.PAUSED
    clock.advance(200)
    c.handle(event(Phase.BUSY, 2001))
    assert c.current_state == State.PAUSED
    c.handle(event(Phase.RESUME, 2002))
    assert c.frame_index == before
    c.handle(event(Phase.COMPLETE, 2003))
    assert c.current_state == State.COMPLETE
    clock.advance(4.799)
    assert c.current_state == State.COMPLETE
    clock.advance(0.002)
    assert c.current_state == State.IDLE
    c.handle(event(Phase.COMPLETE, 2004, eid="different-transport-id"))
    assert c.current_state == State.IDLE
    assert requests


@pytest.mark.parametrize(
    "terminal,state", [(Phase.CANCEL, State.CANCELLED), (Phase.ERROR, State.ERROR)]
)
def test_cancel_fail_stale_success(pack, clock, terminal, state):
    c = EmoteController(pack, source="openvegas", session_id="s1", clock=clock)
    c.handle(event(Phase.START, 0))
    c.handle(event(terminal, 1))
    c.handle(event(Phase.COMPLETE, 2))
    assert c.current_state == state
    assert not c.handle(event(Phase.START, 3, generation=2))


def test_new_turn_cancels_old_and_foreign_events(pack, clock):
    c = EmoteController(pack, source="openvegas", session_id="s1", clock=clock)
    c.handle(event(Phase.START, 0))
    c.handle(event(Phase.COMPLETE, 1))
    clock.advance(0.2)
    assert not c.handle(event(Phase.CANCEL, 2, session="foreign"))
    c.handle(event(Phase.START, 0, turn="turn2", generation=2))
    assert c.current_state == State.ACTIVE
    assert not c.handle(event(Phase.COMPLETE, 100))
    assert not c.handle(event(Phase.START, 200, turn="turn3", generation=1))
    assert not c.handle(event(Phase.COMPLETE, 1, turn="turn2", generation=3))
    assert c.current_state == State.ACTIVE


def test_duplicate_event_ids_and_order(pack, clock):
    c = EmoteController(pack, source="openvegas", session_id="s1", clock=clock)
    assert not c.handle(event(Phase.COMPLETE, 0))
    c.handle(event(Phase.START, 0, eid="same"))
    assert not c.handle(event(Phase.COMPLETE, 1, eid="same"))
    c.handle(event(Phase.PAUSE, 2))
    assert not c.handle(event(Phase.RESUME, 1))
    assert c.current_state == State.PAUSED


def test_off_exit_disable_and_pack_replacement(pack, clock):
    c = EmoteController(pack, source="openvegas", session_id="s1", clock=clock)
    c.handle(event(Phase.START, 0))
    c.set_enabled(False)
    assert c.current_frame() is None
    c.handle(event(Phase.COMPLETE, 1))
    c.set_enabled(True)
    c.handle(event(Phase.COMPLETE, 2))
    assert c.current_state == State.IDLE
    c.handle(event(Phase.START, 0, turn="turn2", generation=2))
    c.replace_pack(pack)
    c.handle(event(Phase.COMPLETE, 1, turn="turn2", generation=2))
    assert c.current_state == State.IDLE
    c.handle(event(Phase.EXIT, 2, turn="turn2", generation=2))
    c.set_enabled(True)
    assert c.current_state == State.OFF


def test_reduced_motion_no_idle_redraw_and_callback_failure(pack, clock):
    calls = []
    c = EmoteController(
        pack,
        source="openvegas",
        session_id="s1",
        clock=clock,
        reduced_motion=True,
        request_invalidation=lambda: calls.append(1),
    )
    clock.advance(100)
    assert not c.tick()
    c.handle(event(Phase.START, 0))
    clock.advance(1)  # still within the active lease; expiry has separate coverage
    assert not c.tick()
    c.handle(event(Phase.COMPLETE, 1))
    assert c.frame_index == 0
    clock.advance(5)
    assert c.current_state == State.IDLE
    assert len(calls) == 3
    c._invalidate = lambda: 1 / 0
    c.set_enabled(False)
    assert c.reason == "invalidation_failed"


def test_session_guard_not_transport_lru(pack, clock):
    c = EmoteController(pack, source="openvegas", session_id="s1", clock=clock)
    c.handle(event(Phase.START, 0))
    c.handle(event(Phase.COMPLETE, 1))
    clock.advance(5)
    for i in range(2, 4200):
        c.handle(event(Phase.BUSY, i))
    c.handle(event(Phase.COMPLETE, 5000, eid="replayed-completion"))
    assert c.current_state == State.IDLE
    assert len(c._transport) == 4096


def test_turn_limit_fails_closed(pack, clock):
    c = EmoteController(pack, source="openvegas", session_id="s1", clock=clock, max_turns=1)
    c.handle(event(Phase.START, 0))
    c.handle(event(Phase.START, 0, turn="turn2", generation=2))
    assert c.current_state == State.OFF
    assert c.reason == "session_turn_limit"
