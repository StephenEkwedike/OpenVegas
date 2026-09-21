import pytest

from openvegas.emotes import ChatEmoteBridge, EmoteController, Phase, State


def collector(events):
    def publish(event):
        events.append(event)
        return True

    return publish


def test_lifecycle_terminal_guard_and_tokens(capsys):
    events = []
    bridge = ChatEmoteBridge("s", collector(events))
    first = bridge.begin("t1")
    assert first.generation == 1
    assert bridge.begin("t1") == first
    assert bridge.active(turn=first)
    assert bridge.pause(turn=first)
    assert not bridge.pause(turn=first)
    assert not bridge.active(turn=first)
    assert bridge.resume(turn=first)
    assert not bridge.resume(turn=first)
    assert bridge.finish(True, turn=first)
    assert not bridge.finish(False, turn=first)
    assert not bridge.cancel(turn=first)
    assert not bridge.active(turn=first)
    second = bridge.begin("t2")
    assert second.generation == 2
    assert not bridge.finish(True, turn=first)
    assert bridge.cancel(turn=second)
    assert not bridge.finish(True, turn=second)
    assert bridge.close()
    assert not bridge.close()
    assert bridge.begin() is None
    assert [e.phase for e in events] == [
        Phase.START,
        Phase.BUSY,
        Phase.PAUSE,
        Phase.RESUME,
        Phase.COMPLETE,
        Phase.START,
        Phase.CANCEL,
        Phase.EXIT,
    ]
    assert [e.sequence for e in events[:5]] == list(range(5))
    assert [e.sequence for e in events[5:]] == list(range(3))
    assert len({e.event_id for e in events}) == len(events)
    assert capsys.readouterr() == ("", "")


def test_replacement_and_close_cancel_once():
    events = []
    bridge = ChatEmoteBridge("s", collector(events))
    first = bridge.begin("one")
    bridge.begin("two")
    assert bridge.begin("one") is None
    assert not bridge.finish(True, turn=first)
    bridge.close()
    assert [e.phase for e in events] == [
        Phase.START,
        Phase.CANCEL,
        Phase.START,
        Phase.CANCEL,
        Phase.EXIT,
    ]
    assert not bridge.cancel()


@pytest.mark.parametrize("failure", [False, RuntimeError("private detail")])
def test_failed_publish_never_retries_or_leaks(failure, capsys):
    calls = []

    def publish(e):
        calls.append(e)
        if isinstance(failure, Exception):
            raise failure
        return failure

    bridge = ChatEmoteBridge("s", publish)
    turn = bridge.begin()
    assert turn is not None
    assert not bridge.finish(True)
    assert not bridge.finish(True)
    assert not bridge.cancel()
    assert len(calls) == 2
    assert capsys.readouterr() == ("", "")


def test_bridge_controller_no_stale_celebration(pack, clock):
    controller = EmoteController(pack, source="openvegas", session_id="s", clock=clock)
    bridge = ChatEmoteBridge("s", controller.handle)
    first = bridge.begin()
    clock.advance(0.15)
    assert controller.frame_index == 1
    bridge.active()
    assert controller.frame_index == 1
    bridge.pause()
    clock.advance(20)
    bridge.resume()
    assert controller.frame_index == 1
    bridge.finish(True)
    assert controller.current_state == State.COMPLETE
    bridge.begin()
    bridge.finish(True, turn=first)
    assert controller.current_state == State.ACTIVE
    bridge.finish(False)
    assert controller.current_state == State.ERROR


def test_validation_no_content_fields():
    with pytest.raises(ValueError):
        ChatEmoteBridge("prompt text\n")
    with pytest.raises(ValueError):
        ChatEmoteBridge("s", initial_generation=True)
    bridge = ChatEmoteBridge("s", lambda _: True)
    with pytest.raises(ValueError):
        bridge.begin("prompt text")
    bridge.begin()
    assert not bridge.finish("success")
    assert bridge.finish(False)
