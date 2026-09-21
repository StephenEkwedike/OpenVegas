"""Offline owner-to-private-spool lifecycle integration, never provider turns."""

import asyncio
from dataclasses import FrozenInstanceError, replace

import pytest

from openvegas.emotes import EmoteController, Phase, State
from openvegas.emotes import owned_stream as owned
from openvegas.emotes.spool import EventSpool, _locked

VERSION = (0, 153, 4)


def authorization(**changes):
    return replace(owned.OwnedStreamAuthorization(
        owner_id="host", stream_id="connection-1", thread_id="native-thread",
        session_id="watch-session", host_version=VERSION,
        owns_stream=True, ordered=True, metadata_only=True,
    ), **changes)


def record(ordinal, method="turn/started", turn="turn-1", status="inProgress", **changes):
    metadata = owned.CodexLifecycleMetadata(
        "native-thread", turn, method, status if method in owned.TURN_METHODS else None,
    )
    return replace(owned.OwnedStreamRecord("host", "connection-1", ordinal, metadata), **changes)


@pytest.fixture
def spool(tmp_path):
    return EventSpool(tmp_path / "private" / "events")


@pytest.fixture
def stream(spool):
    return owned.OwnedCodexStream(authorization(), enabled=True, spool=spool)


def drain(spool):
    return spool.drain(source="codex", session_id="watch-session")


def phases(spool):
    return [event.phase for event in drain(spool)]


@pytest.mark.parametrize("status,expected", [
    ("completed", Phase.COMPLETE), ("failed", Phase.ERROR), ("interrupted", Phase.CANCEL),
])
def test_owner_spool_controller_lifecycle(stream, spool, pack, clock, status, expected):
    controller = EmoteController(pack, source="codex", session_id="watch-session", clock=clock)
    assert stream.deliver(record(0))
    assert stream.deliver(record(1, "item/commandExecution/requestApproval"))
    events = drain(spool)
    assert [e.phase for e in events] == [Phase.START, Phase.PAUSE]
    for event in events:
        assert controller.handle(event)
    assert controller.current_state == State.PAUSED
    assert stream.deliver(record(2, "turn/completed", status=status))
    events = drain(spool)
    assert [e.phase for e in events] == [expected]
    assert events[0].sequence == 2
    assert controller.handle(events[0])
    assert controller.current_state == {
        Phase.COMPLETE: State.COMPLETE, Phase.ERROR: State.ERROR, Phase.CANCEL: State.CANCELLED,
    }[expected]
    assert not stream.deliver(record(3, "turn/completed", status="completed"))
    assert not stream.deliver(record(4))
    stream.close()
    assert not drain(spool)  # Cleanup does not erase an accepted completion.


def test_local_cancel_intent_and_old_turn_tokens(stream, spool):
    assert stream.deliver(record(0))
    assert stream.cancel("turn-1")
    assert not stream.cancel("turn-1")
    assert not stream.deliver(record(1, "turn/completed", status="completed"))
    assert stream.deliver(record(2, turn="turn-2"))
    assert not stream.cancel("turn-1")
    assert not stream.deliver(record(3, "turn/completed", status="failed"))
    assert stream.deliver(record(4, "turn/completed", turn="turn-2", status="completed"))
    events = drain(spool)
    assert [e.phase for e in events] == [Phase.START, Phase.CANCEL, Phase.START, Phase.COMPLETE]
    assert events[2].generation > events[0].generation
    assert events[3].turn_id == "turn-2"


@pytest.mark.parametrize("early", ["completed", "failed", "interrupted", "pause", "cancel"])
def test_early_barrier_tombstones_without_inventing_start(stream, spool, early):
    if early == "cancel":
        assert not stream.cancel("turn-1")
        start_ordinal = 0
    else:
        method = "item/tool/requestUserInput" if early == "pause" else "turn/completed"
        assert not stream.deliver(record(0, method, status=early))
        start_ordinal = 1
    assert not stream.deliver(record(start_ordinal))
    assert not stream.deliver(record(start_ordinal + 1, "turn/completed", status="completed"))
    assert stream.deliver(record(start_ordinal + 2, turn="turn-2"))
    assert phases(spool) == [Phase.START]


def test_superseding_turn_cancels_and_late_duplicates_do_not_refresh(stream, spool):
    assert stream.deliver(record(0))
    assert not stream.deliver(record(0))
    assert not stream.deliver(record(1))
    assert stream.deliver(record(2, turn="turn-2"))
    assert not stream.deliver(record(1, "turn/completed", status="completed"))
    assert not stream.deliver(record(3, "turn/completed", status="completed"))
    assert not stream.deliver(record(4))
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.START]


def test_ambiguous_late_status_for_retired_turn_cannot_close_new_turn(stream, spool):
    assert stream.deliver(record(0))
    assert stream.deliver(record(1, turn="turn-2"))
    assert not stream.deliver(record(2, "turn/completed", status="unknown"))
    assert stream.enabled
    assert stream.deliver(record(3, "turn/completed", turn="turn-2", status="completed"))
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.START, Phase.COMPLETE]


@pytest.mark.parametrize("method", owned.PAUSE_METHODS)
def test_approval_never_guesses_resume(stream, spool, method):
    assert stream.deliver(record(0))
    assert stream.deliver(record(1, method))
    assert not stream.deliver(record(2, method))
    retry = record(3, "error")
    retry = replace(retry, metadata=replace(retry.metadata, will_retry=True))
    assert not stream.deliver(retry)
    assert stream.enabled
    assert phases(spool) == [Phase.START, Phase.PAUSE]


def test_nonretrying_error_latches_failure(stream, spool):
    stream.deliver(record(0))
    error = record(1, "error")
    assert stream.deliver(replace(error, metadata=replace(error.metadata, will_retry=False)))
    assert not stream.deliver(record(2, "turn/completed", status="completed"))
    assert phases(spool) == [Phase.START, Phase.ERROR]


@pytest.mark.parametrize("version", [None, [0, 153, 4], (0, 153, True), (0, 153, 3),
                                     (0, 154, 0), "0.153.4", (0, 153, 4, 0)])
def test_unknown_versions_are_neutral_and_do_not_read(version, spool):
    stream = owned.OwnedCodexStream(authorization(host_version=version), enabled=True, spool=spool)
    assert stream.reason == "unsupported_version"
    assert not stream.deliver(record(0))
    assert asyncio.run(stream.consume(None)) == "unsupported_version"
    assert not spool.directory.parent.exists()


@pytest.mark.parametrize("field", ["owns_stream", "ordered", "metadata_only"])
@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_authorization_must_be_explicit_booleans(field, value, spool):
    stream = owned.OwnedCodexStream(authorization(**{field: value}), enabled=True, spool=spool)
    assert stream.reason == "not_authorized"
    assert not stream.deliver(record(0))
    assert not spool.directory.parent.exists()


@pytest.mark.parametrize("enabled", [False, None, 1, "true"])
def test_default_and_nonboolean_opt_in_do_nothing(enabled, spool):
    stream = owned.OwnedCodexStream(authorization(), enabled=enabled, spool=spool)
    assert stream.reason == "not_enabled"
    assert not stream.deliver(record(0))
    stream.close()
    assert not spool.directory.parent.exists()


@pytest.mark.parametrize("field", ["owner_id", "stream_id", "thread_id", "session_id"])
def test_invalid_authorization_identity(field, spool):
    stream = owned.OwnedCodexStream(authorization(**{field: "secret\nprompt"}), enabled=True, spool=spool)
    assert stream.reason == "invalid_authorization"
    assert not stream.deliver(record(0))
    assert not spool.directory.parent.exists()


def test_wrong_owner_stream_thread_cannot_publish_or_advance_order(stream, spool):
    assert not stream.deliver(record(0, owner_id="other"))
    assert not stream.deliver(record(0, stream_id="old-connection"))
    other = record(0)
    assert not stream.deliver(replace(other, metadata=replace(other.metadata, thread_id="other")))
    assert not spool.directory.parent.exists()
    assert stream.deliver(record(0))
    with pytest.raises(FrozenInstanceError):
        stream._authorization.owns_stream = False
    assert phases(spool) == [Phase.START]


@pytest.mark.parametrize("ordinal", [2, 100, -1, True, 1.0, 2**53, "1"])
def test_gap_or_invalid_order_disables_no_success(stream, spool, ordinal):
    assert stream.deliver(record(0))
    assert not stream.deliver(record(ordinal, "turn/completed", status="completed"))
    assert not stream.enabled
    assert not stream.deliver(record(1, "turn/completed", status="completed"))
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]


def test_must_start_feed_at_zero(stream, spool):
    assert not stream.deliver(record(1))
    assert stream.reason == "stream_gap"
    assert not spool.directory.parent.exists()


@pytest.mark.parametrize("status,has_error", [("unknown", False), ("inProgress", False),
                                             ("completed", True)])
def test_ambiguous_finality_retires_without_success(stream, spool, status, has_error):
    stream.deliver(record(0))
    end = record(1, "turn/completed", status=status)
    assert not stream.deliver(replace(end, metadata=replace(end.metadata, has_error=has_error)))
    assert stream.reason == "unknown_lifecycle"
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]


@pytest.mark.parametrize("failure", [False, RuntimeError("private payload must not leak")])
def test_backpressure_latches_even_when_later_writes_could_succeed(stream, spool, monkeypatch, capsys, failure):
    assert stream.deliver(record(0))
    calls = []
    original = spool.publish

    def fail(event):
        calls.append(event.phase)
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(spool, "publish", fail)
    assert not stream.deliver(record(1, "item/tool/requestUserInput"))
    monkeypatch.setattr(spool, "publish", original)
    assert not stream.deliver(record(2, "turn/completed", status="completed"))
    stream.close()
    assert stream.reason == "publication_failed"
    assert calls == [Phase.PAUSE]
    assert phases(spool) == [Phase.START]
    assert capsys.readouterr() == ("", "")


def test_actual_spool_lock_contention_is_nonretrying(stream, spool):
    stream.deliver(record(0))
    with _locked(spool.directory):
        assert not stream.deliver(record(1, "turn/completed", status="completed"))
    assert stream.reason == "publication_failed"
    assert not stream.deliver(record(2, "turn/completed", status="completed"))
    assert phases(spool) == [Phase.START]


def test_generation_corruption_never_resets_or_publishes(stream, spool):
    root = spool.directory.parent / "run-generations"
    root.mkdir(mode=0o700, parents=True)
    counter = root / "global-v1.json"
    counter.write_text('{"generation":"broken"}')
    counter.chmod(0o600)
    assert not stream.deliver(record(0))
    assert stream.reason == "generation_unavailable"
    assert not spool.directory.exists()


def test_new_observers_reserve_above_previous_generation(spool):
    generations = []
    for index in range(2):
        stream = owned.OwnedCodexStream(
            authorization(stream_id=f"connection-{index}"), enabled=True, spool=spool,
        )
        assert stream.deliver(record(0, stream_id=f"connection-{index}"))
        generations.append(drain(spool)[0].generation)
        stream.close()
        drain(spool)
    assert generations[1] > generations[0]


def test_bounded_tombstones_fail_closed_not_evicted(spool):
    stream = owned.OwnedCodexStream(authorization(), enabled=True, spool=spool, max_turns=2)
    assert stream.deliver(record(0))
    assert not stream.deliver(record(1, "turn/completed", turn="early", status="completed"))
    assert not stream.deliver(record(2, turn="third"))
    assert stream.reason == "turn_limit"
    assert not stream.deliver(record(3))
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]


@pytest.mark.parametrize("bound", [0, 513, True, 1.1])
def test_invalid_bounds_disable_without_io(bound, spool):
    stream = owned.OwnedCodexStream(authorization(), enabled=True, spool=spool, max_turns=bound)
    assert stream.reason == "invalid_bound"
    assert not spool.directory.parent.exists()


def test_no_invented_heartbeat_after_silence(stream, spool, pack, clock):
    controller = EmoteController(pack, source="codex", session_id="watch-session", clock=clock)
    stream.deliver(record(0))
    controller.handle(drain(spool)[0])
    clock.advance(21)
    assert controller.current_state == State.IDLE
    assert stream.deliver(record(1, "turn/completed", status="completed"))
    controller.handle(drain(spool)[0])
    assert controller.current_state == State.IDLE  # Lease expiry also retires success.


@pytest.mark.parametrize("reason", ["closed", "overflow", "", "private prompt"])
def test_cleanup_is_idempotent_and_cannot_reenable(stream, spool, reason):
    stream.deliver(record(0))
    stream.close(reason=reason)
    stream.close()
    assert not stream.enabled
    assert not stream.deliver(record(1, turn="turn-2"))
    assert stream._bridge is None
    assert not stream._seen
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["eof", "failure", "success"])
async def test_consumes_owned_iterable_and_cleans_up(stream, spool, capsys, end):
    async def feed():
        yield record(0)
        if end == "failure":
            raise RuntimeError("private upstream detail")
        if end == "success":
            yield record(1, "turn/completed", status="completed")

    reason = await stream.consume(feed())
    assert reason == ("stream_error" if end == "failure" else "closed")
    assert phases(spool) == ([Phase.START, Phase.COMPLETE] if end == "success"
                             else [Phase.START, Phase.CANCEL, Phase.EXIT])
    assert stream._bridge is None
    assert not stream._consuming
    assert capsys.readouterr() == ("", "")


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_without_owning_feed(stream, spool):
    ready = asyncio.Event()

    class Feed:
        count = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            self.count += 1
            if self.count == 1:
                return record(0)
            ready.set()
            await asyncio.Event().wait()
        async def aclose(self):
            pytest.fail("observer must not close host transport")

    task = asyncio.create_task(stream.consume(Feed()))
    await asyncio.wait_for(ready.wait(), timeout=1)
    assert await stream.consume(None) == "already_consuming"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]
    assert stream._bridge is None
    assert not stream._consuming


@pytest.mark.asyncio
async def test_gap_stops_reading_without_draining_or_closing_owner(stream):
    class Feed:
        calls = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            self.calls += 1
            assert self.calls == 1
            return record(3)
        async def aclose(self):
            pytest.fail("host resource belongs to caller")
    feed = Feed()
    assert await stream.consume(feed) == "stream_gap"
    assert feed.calls == 1


def test_explicit_heartbeat_keeps_silent_turn_active_past_lease(spool, pack, clock):
    stream = owned.OwnedCodexStream(authorization(), enabled=True, spool=spool, clock=clock)
    controller = EmoteController(pack, source="codex", session_id="watch-session", clock=clock)
    assert stream.deliver(record(0))
    token = stream.active_turn
    controller.handle(drain(spool)[0])
    for index in range(1, 13):
        clock.advance(5)
        assert stream.heartbeat(turn=token)
        events = drain(spool)
        assert len(events) == 1
        assert events[0].phase == Phase.BUSY
        assert events[0].sequence == index
        controller.handle(events[0])
        assert controller.current_state == State.ACTIVE
    assert clock.now == 60
    # Native ordinals remain unchanged by heartbeat publication. The same bridge
    # owns every output sequence and still accepts the next native final barrier.
    assert stream.deliver(record(1, "turn/completed", status="completed"))
    final = drain(spool)[0]
    assert final.sequence == 13 and final.phase == Phase.COMPLETE
    controller.handle(final)
    assert controller.current_state == State.COMPLETE
    assert stream.active_turn is None


def test_heartbeat_rate_bound_and_stale_token_does_not_renew_new_turn(spool, clock):
    stream = owned.OwnedCodexStream(authorization(), enabled=True, spool=spool, clock=clock)
    stream.deliver(record(0))
    first = stream.active_turn
    assert not stream.heartbeat(turn=first)
    clock.advance(4.999)
    assert not stream.heartbeat(turn=first)
    clock.advance(0.001)
    assert stream.heartbeat(turn=first)
    for _ in range(1000):
        assert not stream.heartbeat(turn=first)
    assert stream.deliver(record(1, turn="turn-2"))
    second = stream.active_turn
    clock.advance(5)
    assert not stream.heartbeat(turn=first)
    assert stream.heartbeat(turn=second)
    events = drain(spool)
    assert [e.phase for e in events] == [Phase.START, Phase.BUSY, Phase.CANCEL,
                                       Phase.START, Phase.BUSY]
    assert [e.sequence for e in events] == [0, 1, 2, 0, 1]
    assert events[-1].turn_id == "turn-2"


@pytest.mark.parametrize("end", ["pause", "cancel", "completed", "failed", "interrupted",
                                 "error", "close", "gap"])
def test_heartbeat_never_resurrects_paused_or_retired_turn(spool, clock, end):
    stream = owned.OwnedCodexStream(authorization(), enabled=True, spool=spool, clock=clock)
    stream.deliver(record(0))
    token = stream.active_turn
    if end == "pause":
        stream.deliver(record(1, "item/tool/requestUserInput"))
    elif end == "cancel":
        stream.cancel("turn-1")
    elif end == "close":
        stream.close()
    elif end == "gap":
        stream.deliver(record(3))
    elif end == "error":
        error = record(1, "error")
        stream.deliver(replace(error, metadata=replace(error.metadata, will_retry=False)))
    else:
        stream.deliver(record(1, "turn/completed", status=end))
    assert stream.active_turn is None
    drain(spool)
    clock.advance(60)
    assert not stream.heartbeat(turn=token)
    assert drain(spool) == []


def test_paused_turn_does_not_resume_on_retry_or_duplicate_start(spool, clock):
    stream = owned.OwnedCodexStream(authorization(), enabled=True, spool=spool, clock=clock)
    stream.deliver(record(0))
    token = stream.active_turn
    stream.deliver(record(1, "item/commandExecution/requestApproval"))
    retry = record(2, "error")
    stream.deliver(replace(retry, metadata=replace(retry.metadata, will_retry=True)))
    stream.deliver(record(3))
    clock.advance(10)
    assert stream.active_turn is None
    assert not stream.heartbeat(turn=token)
    assert phases(spool) == [Phase.START, Phase.PAUSE]


def test_failed_heartbeat_publication_permanently_disables(spool, clock, monkeypatch):
    stream = owned.OwnedCodexStream(authorization(), enabled=True, spool=spool, clock=clock)
    stream.deliver(record(0))
    token = stream.active_turn
    original = spool.publish
    monkeypatch.setattr(spool, "publish", lambda event: False)
    clock.advance(5)
    assert not stream.heartbeat(turn=token)
    assert stream.reason == "publication_failed"
    assert stream.active_turn is None
    monkeypatch.setattr(spool, "publish", original)
    clock.advance(5)
    assert not stream.heartbeat(turn=token)
    assert not stream.deliver(record(1, "turn/completed", status="completed"))
    assert phases(spool) == [Phase.START]


@pytest.mark.parametrize("value", [None, False, {}, "turn-1", owned.TurnToken("other", 0)])
def test_heartbeat_requires_current_typed_token(spool, clock, value):
    stream = owned.OwnedCodexStream(authorization(), enabled=True, spool=spool, clock=clock)
    assert stream.active_turn is None
    assert not stream.heartbeat(turn=value)
    stream.deliver(record(0))
    clock.advance(5)
    assert not stream.heartbeat(turn=value)
    assert phases(spool) == [Phase.START]


@pytest.mark.parametrize("bad_time", [float("nan"), float("inf"), True, "now", -1])
def test_invalid_or_reversed_clock_retires_without_success(spool, clock, bad_time):
    stream = owned.OwnedCodexStream(authorization(), enabled=True, spool=spool, clock=clock)
    stream.deliver(record(0))
    token = stream.active_turn
    clock.now = bad_time
    assert not stream.heartbeat(turn=token)
    assert stream.reason == "invalid_clock"
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]


def test_heartbeat_late_after_controller_expiry_cannot_resurrect(spool, clock, pack):
    stream = owned.OwnedCodexStream(authorization(), enabled=True, spool=spool, clock=clock)
    controller = EmoteController(pack, source="codex", session_id="watch-session", clock=clock)
    stream.deliver(record(0))
    token = stream.active_turn
    controller.handle(drain(spool)[0])
    clock.advance(25)  # A stalled owner missed every renewal deadline.
    assert controller.current_state == State.IDLE
    assert stream.heartbeat(turn=token)
    controller.handle(drain(spool)[0])
    assert controller.current_state == State.IDLE
    stream.deliver(record(1, "turn/completed", status="completed"))
    controller.handle(drain(spool)[0])
    assert controller.current_state == State.IDLE


@pytest.mark.asyncio
async def test_metadata_feed_eof_disallows_late_heartbeat(spool, clock):
    stream = owned.OwnedCodexStream(authorization(), enabled=True, spool=spool, clock=clock)
    tokens = []
    async def feed():
        yield record(0)
        tokens.append(stream.active_turn)
    await stream.consume(feed())
    clock.advance(10)
    assert not stream.heartbeat(turn=tokens[0])
    assert stream.active_turn is None
    assert phases(spool) == [Phase.START, Phase.CANCEL, Phase.EXIT]
