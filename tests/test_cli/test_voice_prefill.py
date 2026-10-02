from __future__ import annotations

import ast
from pathlib import Path

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


def test_shortcut_transcript_after_prompt_closed_uses_pending_prefill() -> None:
    source = Path(__file__).resolve().parents[2] / "openvegas/cli.py"
    tree = ast.parse(source.read_text())
    callbacks = [node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == "_insert_from_voice"]
    assert len(callbacks) == 1
    session = _Session(text="already submitted")
    queued = []

    def insert(transcript):
        queued.append(_insert_or_queue_voice_transcript(
            transcript=transcript, chat_prompt_session=session,
            prompt_active=False, pending_prefill=None,
        ))

    scope = {"owned_compositor": None, "prompt_input_active": False,
             "_insert_voice_transcript_text": insert}
    exec(compile(ast.Module(body=callbacks, type_ignores=[]), str(source), "exec"), scope)
    scope["_insert_from_voice"]("dictated continuation")
    assert queued == [("dictated continuation", "prefill", 21)]
    assert session.default_buffer.text == "already submitted"
    assert not session.app.invalidated
