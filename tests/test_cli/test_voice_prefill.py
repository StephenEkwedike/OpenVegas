from __future__ import annotations

from openvegas.cli import _insert_or_queue_voice_transcript


class _Buffer:
    def __init__(self, text: str = "", cursor: int | None = None) -> None:
        self.text = text
        self.cursor_position = len(text) if cursor is None else int(cursor)


class _App:
    def __init__(self) -> None:
        self.invalidated = False

    def invalidate(self) -> None:
        self.invalidated = True


class _Session:
    def __init__(self, text: str = "", cursor: int | None = None) -> None:
        self.default_buffer = _Buffer(text=text, cursor=cursor)
        self.app = _App()


def test_voice_prefill_queues_when_prompt_inactive() -> None:
    pending, mode, chars = _insert_or_queue_voice_transcript(
        transcript="hello world",
        chat_prompt_session=None,
        prompt_active=False,
        pending_prefill=None,
    )
    assert pending == "hello world"
    assert mode == "prefill"
    assert chars == 11


def test_voice_prefill_appends_existing_queue() -> None:
    pending, mode, chars = _insert_or_queue_voice_transcript(
        transcript="second",
        chat_prompt_session=None,
        prompt_active=False,
        pending_prefill="first",
    )
    assert pending == "first second"
    assert mode == "prefill"
    assert chars == 6


def test_voice_live_insert_updates_buffer_and_invalidates() -> None:
    session = _Session(text="hello", cursor=5)
    pending, mode, chars = _insert_or_queue_voice_transcript(
        transcript="there",
        chat_prompt_session=session,
        prompt_active=True,
        pending_prefill=None,
    )
    assert pending is None
    assert mode == "live"
    assert chars == 5
    assert session.default_buffer.text == "hello there"
    assert session.default_buffer.cursor_position == len("hello there")
    assert session.app.invalidated is True


def test_voice_empty_transcript_noop() -> None:
    session = _Session(text="hello", cursor=5)
    pending, mode, chars = _insert_or_queue_voice_transcript(
        transcript="   ",
        chat_prompt_session=session,
        prompt_active=True,
        pending_prefill="queued",
    )
    assert pending == "queued"
    assert mode == "none"
    assert chars == 0
    assert session.default_buffer.text == "hello"
