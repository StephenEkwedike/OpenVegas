"""Real prompt-toolkit loop, offline input pipe and synthetic authorized art.

These are application integration tests, not native macOS terminal certification.
"""

from __future__ import annotations

import asyncio
import io
from contextlib import asynccontextmanager

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from openvegas.emotes.bridge import ChatEmoteBridge
from openvegas.emotes.compositor import OwnedChatCompositor, TurnCancelled
from openvegas.emotes.controller import State
from openvegas.emotes.events import Event, Phase
from openvegas.emotes.render import fit_frame
from openvegas.emotes.resources import PackRepository


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.01)


@asynccontextmanager
async def running(pack, clock, **kwargs):
    with create_pipe_input() as pipe:
        output_text, console_text = io.StringIO(), io.StringIO()
        size = [Size(rows=30, columns=90)]
        output = Vt100_Output(output_text, get_size=lambda: size[0], enable_cpr=False)
        console = Console(file=console_text, force_terminal=True, color_system="truecolor", width=80)
        session = PromptSession(input=pipe, output=output)
        owner = OwnedChatCompositor(
            session, console, session_id="local-test", pack=pack,
            clock=clock, access_guard=lambda: True, reduced_motion=False, **kwargs,
        )
        try:
            await owner.start()
            yield owner, pipe, output_text, console_text, size
        finally:
            await owner.close()


def mouse(kind, y=0):
    return MouseEvent(Point(x=0, y=y), kind, MouseButton.LEFT, frozenset())


@pytest.mark.asyncio
async def test_prompt_accept_once_and_busy_typing_survives_next_prompt(pack, clock):
    async with running(pack, clock) as (owner, pipe, terminal, transcript, _):
        prompt = asyncio.create_task(owner.prompt_async())
        await until(lambda: owner._pending is not None)
        pipe.send_text("hello\r")
        assert await prompt == "hello"
        pipe.send_text("next draft\r")
        await until(lambda: owner.default_buffer.text == "next draft")
        owner.console.print("First answer")
        await until(lambda: "First answer" in owner.history_buffer.text)
        assert not owner._app_task.done()
        prompt = asyncio.create_task(owner.prompt_async())
        await until(lambda: owner._pending is not None)
        assert owner.default_buffer.text == "next draft"
        pipe.send_text("\r")
        assert await prompt == "next draft"
        assert owner.default_buffer.text == ""
        assert terminal.getvalue().count("\x1b[?1049h") == 1
        assert transcript.getvalue() == ""
    assert "First answer" in transcript.getvalue()
    assert terminal.getvalue().count("\x1b[?1049l") == 1


@pytest.mark.asyncio
async def test_toolbar_command_and_voice_never_clobber_draft_or_autosend(pack, clock):
    async with running(pack, clock) as (owner, _, _, _, _):
        owner.default_buffer.document = Document("hello world", 5)
        prompt = asyncio.create_task(owner.prompt_async())
        await until(lambda: owner._pending is not None)
        assert owner.request_command("/voice")
        assert await prompt == "/voice"
        assert owner.default_buffer.document == Document("hello world", 5)
        assert owner.insert_voice("spoken")
        assert owner.default_buffer.text == "hello spoken world"
        cursor = owner.default_buffer.cursor_position
        prompt = asyncio.create_task(owner.prompt_async(default=""))
        await until(lambda: owner._pending is not None)
        assert owner.default_buffer.cursor_position == cursor
        assert not prompt.done()
        assert owner.request_command("/exit")
        assert await prompt == "/exit"


@pytest.mark.asyncio
async def test_alt_v_binding_not_swallowed_by_escape_focus_binding(pack, clock):
    async with running(pack, clock) as (owner, pipe, _, _, _):
        keys = KeyBindings()
        calls = []

        @keys.add("escape", "v")
        def voice(event):
            calls.append(True)
            owner.insert_voice("voice")

        prompt = asyncio.create_task(owner.prompt_async(key_bindings=keys))
        await until(lambda: owner._pending is not None)
        pipe.send_text("typed\x1bv")
        await until(lambda: calls)
        assert owner.default_buffer.text == "typed voice"
        assert not prompt.done()
        owner.request_command("/exit")
        await prompt


@pytest.mark.asyncio
async def test_scroll_anchor_survives_output_animation_and_resize(pack, clock):
    async with running(pack, clock) as (owner, _, _, _, size):
        owner.append_output("\n".join(f"line {i}" for i in range(120)))
        await asyncio.sleep(0.05)
        owner.default_buffer.document = Document("keep draft cursor", 4)
        owner.history_window._mouse_handler(mouse(MouseEventType.SCROLL_UP))
        owner.scroll(-40)
        anchor = owner.history_window.vertical_scroll
        bridge = ChatEmoteBridge("local-test", publish=owner.publish)
        bridge.begin()
        for i in range(5):
            clock.advance(0.11)
            owner.tick()
            owner.append_output(f"\nstreaming {i}")
            await asyncio.sleep(0.03)
        size[0] = Size(rows=25, columns=55)
        owner.app.invalidate()
        await asyncio.sleep(0.05)
        assert owner.history_window.vertical_scroll == anchor
        assert owner.default_buffer.document == Document("keep draft cursor", 4)
        assert not owner.follow_tail
        owner.jump_to_latest()
        await asyncio.sleep(0.05)
        assert owner.follow_tail
        assert owner.history_buffer.cursor_position == len(owner.history_buffer.text)
        assert owner.history_window.vertical_scroll > anchor


@pytest.mark.asyncio
async def test_scrollbar_drag_and_text_selection_are_stable(pack, clock):
    async with running(pack, clock) as (owner, pipe, _, _, _):
        owner.append_output("\n".join(f"row {i}" for i in range(100)))
        await asyncio.sleep(0.05)
        owner._scrollbar_mouse(mouse(MouseEventType.MOUSE_DOWN, 2))
        owner._scrollbar_mouse(mouse(MouseEventType.MOUSE_MOVE, 4))
        owner._scrollbar_mouse(mouse(MouseEventType.MOUSE_UP, 4))
        anchor = owner.history_window.vertical_scroll
        owner.history_buffer.cursor_position = 0
        owner.history_buffer.start_selection()
        owner.history_buffer.cursor_position = 5
        selection = owner.history_buffer.selection_state
        owner.app.layout.focus(owner.history_control)
        for _ in range(3):
            owner.append_output("\nmore output")
            clock.advance(0.2)
            owner.tick()
            await asyncio.sleep(0.03)
        assert owner.history_buffer.selection_state is selection
        assert owner.history_buffer.cursor_position == 5
        assert owner.history_window.vertical_scroll == anchor
        pipe.send_text("\x03")
        await until(lambda: owner.app.clipboard.get_data().text)
        assert owner.app.clipboard.get_data().text == "row 0"
        assert not owner._app_task.done()


@pytest.mark.asyncio
async def test_voice_while_history_selected_targets_input_only(pack, clock):
    async with running(pack, clock) as (owner, _, _, _, _):
        owner.append_output("select history")
        owner.history_buffer.cursor_position = 0
        owner.history_buffer.start_selection()
        owner.history_buffer.cursor_position = 6
        state = owner.history_buffer.selection_state
        owner.app.layout.focus(owner.history_control)
        owner.default_buffer.document = Document("a b", 1)
        owner.insert_voice("spoken")
        assert owner.default_buffer.text == "a spoken b"
        assert owner.history_buffer.text == "select history"
        assert owner.history_buffer.selection_state is state
        assert owner.history_buffer.cursor_position == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "error", "success"])
async def test_authoritative_lifecycle_and_no_stale_resurrection(pack, clock, ending):
    async with running(pack, clock) as (owner, _, _, _, _):
        bridge = ChatEmoteBridge("local-test", publish=owner.publish)
        token = bridge.begin()
        assert owner.controller.current_state == State.ACTIVE
        clock.advance(0.11)
        owner.tick()
        assert owner.controller.frame_index == 1
        bridge.pause(turn=token)
        paused = owner.controller.frame_index
        clock.advance(10)
        owner.tick()
        assert owner.controller.frame_index == paused
        bridge.resume(turn=token)
        if ending == "cancel":
            bridge.cancel(turn=token)
            assert owner.controller.current_state == State.CANCELLED
        else:
            bridge.finish(success=ending == "success", turn=token)
            assert owner.controller.current_state == (State.COMPLETE if ending == "success" else State.ERROR)
        prior = owner.controller.current_state
        owner.publish(Event("openvegas", "local-test", token.turn_id, "late", Phase.COMPLETE, token.generation, 99, "success"))
        assert owner.controller.current_state == prior
        if ending == "success":
            clock.advance(5)
            owner.tick()
            assert owner.controller.current_state == State.IDLE
            assert not bridge.finish(success=True, turn=token)


@pytest.mark.asyncio
async def test_access_revocation_and_new_pack_do_not_restart_retired_turn(pack, clock):
    async with running(pack, clock) as (owner, _, _, _, _):
        allowed = [True]
        owner.set_packs(pack, access_guard=lambda: allowed[0])
        bridge = ChatEmoteBridge("local-test", publish=owner.publish)
        turn = bridge.begin()
        allowed[0] = False
        owner.tick()
        assert owner.controller.current_state == State.OFF
        assert owner._dock_fragments() == []
        allowed[0] = True
        owner.set_packs(pack, access_guard=lambda: allowed[0])
        bridge.finish(success=True, turn=turn)
        assert owner.controller.current_state != State.COMPLETE
        bridge.begin()
        assert owner.controller.current_state == State.ACTIVE


@pytest.mark.asyncio
async def test_reserved_dock_does_not_overlap_input_and_disappears_on_tiny_screen(pack, clock):
    async with running(pack, clock) as (owner, _, _, _, size):
        owner.append_output("history\n" * 100)
        owner.default_buffer.text = "long input " * 15
        owner.app.invalidate()
        await asyncio.sleep(0.06)
        positions = owner.app.renderer.last_rendered_screen.visible_windows_to_write_positions
        history = positions[owner.history_window]
        dock = positions[owner.dock_window]
        input_pos = positions[owner.input_window]
        assert history.ypos + history.height <= dock.ypos
        assert dock.ypos + dock.height <= input_pos.ypos
        assert input_pos.height >= 1
        size[0] = Size(rows=10, columns=35)
        owner.app.invalidate()
        await asyncio.sleep(0.06)
        assert owner.dock_height == 0
        assert owner.default_buffer.text == "long input " * 15


@pytest.mark.asyncio
async def test_single_owner_output_sanitization_replay_and_restore(pack, clock):
    async with running(pack, clock) as (owner, _, _, transcript, _):
        original_accept = owner._original_accept
        with pytest.raises(RuntimeError, match="already"):
            OwnedChatCompositor(owner.session, owner.console, session_id="other")
        owner.console.print("[on #333333]user prompt[/]")
        owner.append_output("\x1b[2Junsafe\x1b]52;c;secret")
        owner.append_output("\x07\x1b[1A\x1b[31mred\ncontinued\x1b[0m normal\n")
        owner.append_output("\x1b[3")
        owner.append_output("2mgreen\x1b[0m\n")
        await asyncio.sleep(0.03)
        assert "secret" not in owner.history_buffer.text
        rows = owner._rows
        red_chars = [value for row in rows for style, value in row if "ansired" in style]
        assert "continued" in "".join(red_chars)
        assert any("bg:#333333" in style for row in rows for style, _ in row)
        await owner.close()
        assert owner.console.file is transcript
        assert owner.default_buffer.accept_handler is original_accept
        result = transcript.getvalue()
        assert "\x1b[2J" not in result and "\x1b]52" not in result and "\x1b[1A" not in result
        assert result.count("user prompt") == 1
        await owner.close()
        assert transcript.getvalue() == result


@pytest.mark.asyncio
async def test_external_prompt_suspends_owner_and_restores_on_error(pack, clock):
    async with running(pack, clock) as (owner, _, terminal, transcript, _):
        owner.default_buffer.document = Document("preserved", 3)
        owner.scroll(-3)
        owner.default_buffer.start_selection()
        selection = owner.default_buffer.selection_state

        def approval():
            assert owner._suspended
            assert owner.console.file is transcript
            owner.console.print("external approval")
            raise ValueError("denied")

        with pytest.raises(ValueError, match="denied"):
            await owner.run_external(approval)
        assert not owner._suspended
        assert owner.console.file is not transcript
        assert owner.default_buffer.text == "preserved"
        assert owner.default_buffer.cursor_position == 3
        assert owner.default_buffer.selection_state is selection
        assert not owner.follow_tail
        assert "external approval" in transcript.getvalue()
        assert terminal.getvalue().count("\x1b[?1049l") == 1
        assert terminal.getvalue().count("\x1b[?1049h") == 2


@pytest.mark.asyncio
async def test_ctrl_c_cancels_owned_work_not_prompt_and_no_success(pack, clock):
    async with running(pack, clock) as (owner, pipe, _, _, _):
        bridge = ChatEmoteBridge("local-test", publish=owner.publish)
        turn = bridge.begin()
        began = asyncio.Event()
        seen = []

        async def work():
            began.set()
            try:
                await asyncio.Event().wait()
            finally:
                seen.append(owner.controller.current_state)

        task = asyncio.create_task(owner.run_turn(work(), on_cancel=lambda: bridge.cancel(turn=turn)))
        await began.wait()
        pipe.send_text("draft\x03")
        with pytest.raises(TurnCancelled):
            await task
        assert seen == [State.CANCELLED]
        assert owner.default_buffer.text == "draft"
        assert not owner._app_task.done()
        assert not bridge.finish(success=True, turn=turn)
        prompt = asyncio.create_task(owner.prompt_async())
        await until(lambda: owner._pending is not None)
        pipe.send_text("\r")
        assert await prompt == "draft"


@pytest.mark.asyncio
async def test_returned_work_never_infers_success_and_outer_cancel_propagates(pack, clock):
    async with running(pack, clock) as (owner, _, _, _, _):
        bridge = ChatEmoteBridge("local-test", publish=owner.publish)
        turn = bridge.begin()
        assert await owner.run_turn(asyncio.sleep(0, result=42), on_cancel=bridge.cancel) == 42
        assert owner.controller.current_state == State.ACTIVE
        task = asyncio.create_task(owner.run_turn(asyncio.sleep(30), on_cancel=lambda: bridge.cancel(turn=turn)))
        await until(lambda: owner._work_task is not None)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owner.controller.current_state == State.CANCELLED


@pytest.mark.asyncio
async def test_terminal_eof_closes_prompt_without_celebration(pack, clock):
    async with running(pack, clock) as (owner, pipe, _, _, _):
        bridge = ChatEmoteBridge("local-test", publish=owner.publish)
        bridge.begin()
        prompt = asyncio.create_task(owner.prompt_async())
        await until(lambda: owner._pending is not None)
        pipe.close()
        with pytest.raises(EOFError):
            await prompt
        await owner.close()
        assert owner.controller.current_state == State.OFF
        assert owner._tick_task.done()


@pytest.mark.asyncio
async def test_reduced_motion_static_and_idle_does_not_redraw(pack, clock):
    async with running(pack, clock) as (owner, _, _, _, _):
        owner.controller.reduced_motion = True
        bridge = ChatEmoteBridge("local-test", publish=owner.publish)
        bridge.begin()
        clock.advance(0.11)
        owner.tick()
        first = owner._dock_fragments()
        clock.advance(0.12)
        owner.tick()
        assert owner._dock_fragments() == first
        bridge.cancel()
        await asyncio.sleep(0.05)
        before = owner.app.render_counter
        await asyncio.sleep(0.25)
        assert owner.app.render_counter == before


@pytest.mark.asyncio
async def test_second_session_cannot_become_second_painter(pack, clock):
    async with running(pack, clock) as (owner, pipe, terminal, _, _):
        session = PromptSession(input=pipe, output=owner.app.output)
        other = OwnedChatCompositor(session, owner.console, session_id="another")
        try:
            with pytest.raises(RuntimeError, match="owns the terminal"):
                await other.start()
            assert terminal.getvalue().count("\x1b[?1049h") == 1
        finally:
            await other.close()
        owner.console.print("original owner still works")
        await until(lambda: "still works" in owner.history_buffer.text)


@pytest.mark.asyncio
async def test_eof_cancels_work_and_stops_refresh_without_resurrection(pack, clock):
    async with running(pack, clock) as (owner, pipe, _, _, _):
        bridge = ChatEmoteBridge("local-test", publish=owner.publish)
        token = bridge.begin()
        task = asyncio.create_task(owner.run_turn(asyncio.sleep(30), on_cancel=lambda: bridge.cancel(turn=token)))
        await until(lambda: owner._work_task is not None)
        pipe.close()
        with pytest.raises(TurnCancelled):
            await task
        assert owner.controller.current_state == State.OFF
        await until(lambda: owner._tick_task.done())
        assert not bridge.finish(success=True, turn=token)
        assert not owner.publish(Event("openvegas", "local-test", token.turn_id, "late-start", Phase.START, token.generation, 99))


@pytest.mark.asyncio
async def test_failed_work_has_no_automatic_completion(pack, clock):
    async with running(pack, clock) as (owner, _, _, _, _):
        bridge = ChatEmoteBridge("local-test", publish=owner.publish)
        token = bridge.begin()

        async def fail():
            raise ValueError("offline failure")

        with pytest.raises(ValueError, match="offline failure"):
            await owner.run_turn(fail(), on_cancel=lambda: bridge.cancel(turn=token))
        bridge.finish(success=False, turn=token)
        assert owner.controller.current_state == State.ERROR
        clock.advance(10)
        owner.tick()
        assert owner.controller.current_state == State.ERROR


@pytest.mark.asyncio
async def test_foreign_external_events_and_disable_do_not_enable_pack(pack, clock):
    async with running(pack, clock) as (owner, _, _, _, _):
        assert not owner.publish(Event("codex", "local-test", "turn", "event", Phase.START, 1, 1))
        owner.set_packs(None, access_guard=lambda: True)
        owner.tick()
        assert owner.controller.current_state == State.OFF
        bridge = ChatEmoteBridge("local-test", publish=owner.publish)
        bridge.begin()
        bridge.finish(success=True)
        assert owner.controller.current_state == State.OFF
        assert owner._dock_fragments() == []


@pytest.mark.asyncio
async def test_bracketed_paste_and_input_selection_survive_redraws(pack, clock):
    async with running(pack, clock) as (owner, pipe, _, _, _):
        prompt = asyncio.create_task(owner.prompt_async())
        await until(lambda: owner._pending is not None)
        pipe.send_text("\x1b[200~pasted one\npasted two\x1b[201~")
        await until(lambda: "pasted two" in owner.default_buffer.text)
        assert not prompt.done()
        buffer = owner.default_buffer
        buffer.cursor_position = 2
        buffer.start_selection()
        buffer.cursor_position = 6
        selection = buffer.selection_state
        owner.append_output("output while selecting\n")
        owner.tick()
        await asyncio.sleep(0.03)
        assert buffer.selection_state is selection
        assert buffer.cursor_position == 6
        assert buffer.text == "pasted one\npasted two"
        pipe.send_text("\x03")
        await until(lambda: owner.app.clipboard.get_data().text)
        assert owner.app.clipboard.get_data().text == "sted"
        assert not prompt.done()
        owner.request_command("/exit")
        await prompt


@pytest.mark.asyncio
async def test_background_console_writes_are_marshaled_to_owner(pack, clock):
    async with running(pack, clock) as (owner, _, _, _, _):
        await asyncio.to_thread(owner.console.print, "worker output")
        await until(lambda: "worker output" in owner.history_buffer.text)
        assert owner.history_buffer.text.count("worker output") == 1


@pytest.mark.asyncio
async def test_pending_prompt_close_is_idempotent_and_cannot_restart(pack, clock):
    async with running(pack, clock) as (owner, _, _, _, _):
        prompt = asyncio.create_task(owner.prompt_async())
        await until(lambda: owner._pending is not None)
        await owner.close()
        with pytest.raises(EOFError):
            await prompt
        with pytest.raises(RuntimeError, match="closed"):
            await owner.start()
        owner.set_packs(pack, access_guard=lambda: True)
        assert owner.controller.current_state == State.OFF


@pytest.mark.asyncio
async def test_heartbeat_supervision_survives_silent_active_turn(pack, clock):
    async with running(pack, clock) as (owner, _, _, _, _):
        bridge = ChatEmoteBridge("local-test", publish=owner.publish)
        token = bridge.begin()
        for _ in range(8):
            clock.advance(5)
            bridge.active(turn=token)
            owner.tick()
        assert owner.controller.current_state == State.ACTIVE
        clock.advance(21)
        owner.tick()
        assert owner.controller.current_state == State.IDLE
        bridge.finish(success=True, turn=token)
        assert owner.controller.current_state == State.IDLE


@pytest.mark.asyncio
async def test_safe_prompt_contract_and_no_double_waiter(pack, clock):
    async with running(pack, clock) as (owner, _, _, _, _):
        with pytest.raises(ValueError, match="one refresh owner"):
            await owner.prompt_async(refresh_interval=0.1)
        prompt = asyncio.create_task(owner.prompt_async(default="prefilled voice"))
        await until(lambda: owner._pending is not None)
        with pytest.raises(RuntimeError, match="Only one prompt"):
            await owner.prompt_async()
        assert owner.default_buffer.text == "prefilled voice"
        assert owner.request_command("/exit")
        await prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("dimensions,height,mode", [
    ((80, 24), 8, "art"), ((120, 36), 12, "art"), ((160, 48), 16, "art"),
    ((200, 70), 16, "art"), ((80, 20), 1, "compact"), ((40, 48), 1, "compact"),
    ((80, 15), 0, "hidden"),
])
async def test_responsive_budget_preserves_history_and_four_row_draft(pack, clock, dimensions, height, mode):
    async with running(pack, clock) as (owner, _, _, _, size):
        columns, rows = dimensions
        size[0] = Size(rows=rows, columns=columns)
        owner.append_output("selectable history\n" * 100)
        owner.default_buffer.text = "draft " * columns
        draft = owner.default_buffer.document
        owner.app.invalidate()
        await asyncio.sleep(0.05)
        assert owner.dock_height == height
        assert owner.dock_mode == mode
        assert height <= rows // 3
        positions = owner.app.renderer.last_rendered_screen.visible_windows_to_write_positions
        assert positions[owner.history_window].height >= 8
        assert positions[owner.input_window].height == 4
        assert owner.default_buffer.document == draft
        if mode == "compact":
            fragments = owner._dock_fragments()
            assert "Test Fixture Only | Ready (compact)" in "".join(t for _, t in fragments)
            assert not any("\u2580" in t or "\u2584" in t for _, t in fragments)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [
    "pixel-courier", "beat-maker", "visor-explorer", "skyline-dunk", "bicycle-finish", "three-point-glow",
])
async def test_real_art_readability_pixel_budget_and_distinct_poses(clock, name):
    pack = PackRepository().load(name)
    animation = "complete" if "completion" in pack.manifest.tags else "waiting"
    original = pack.frame(pack.manifest.animations[animation].frames[0])
    old = fit_frame(original, max_columns=80, max_rows=6)
    async with running(pack, clock) as (owner, _, _, _, size):
        fitted = []
        for columns, rows, dock_height in [(80, 24, 8), (160, 48, 16)]:
            size[0] = Size(rows=rows, columns=columns)
            assert owner.dock_height == dock_height
            frame = fit_frame(original, max_columns=columns - 2, max_rows=dock_height)
            fitted.append(frame)
            # This measures retained pixels, not subjective native art approval.
            assert frame.width * frame.height > old.width * old.height * 1.4
            assert frame.getbbox() is not None
            fragments = owner._dock_fragments()
            assert pack.manifest.display_name in "".join(t for _, t in fragments)
            poses = {
                fit_frame(pack.frame(index), max_columns=columns - 2, max_rows=dock_height).tobytes()
                for index in pack.manifest.animations[animation].frames
            }
            # Multi-pose dances stay distinct after fitting, not just a label.
            assert len(poses) >= 2
        if original.height == 80:
            assert [frame.size for frame in fitted] == [(12, 16), (21, 26)]
        else:
            assert [frame.size for frame in fitted] == [(20, 15), (40, 30)]


@pytest.mark.asyncio
async def test_resize_art_compact_art_keeps_selection_cursor_and_history_anchor(pack, clock):
    async with running(pack, clock) as (owner, _, _, _, size):
        owner.append_output("old history\n" * 100)
        owner.default_buffer.document = Document("typed draft", 4)
        owner.history_buffer.cursor_position = 0
        owner.history_buffer.start_selection()
        owner.history_buffer.cursor_position = 5
        selection = owner.history_buffer.selection_state
        owner.scroll(15)
        anchor = owner.history_window.vertical_scroll
        for rows, columns in [(48, 160), (24, 80), (20, 80), (48, 160)]:
            size[0] = Size(rows=rows, columns=columns)
            owner.append_output("new streaming output\n")
            owner.app.invalidate()
            await asyncio.sleep(0.05)
            assert owner.default_buffer.document == Document("typed draft", 4)
            assert owner.history_buffer.selection_state is selection
            assert owner.history_buffer.cursor_position == 5
            assert owner.history_window.vertical_scroll == anchor
