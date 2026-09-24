"""Same-window compositor for OpenVegas-owned chat, not external CLI overlays.

One persistent prompt-toolkit Application owns the alternate screen, including
history scrolling/selection, a reserved sprite dock, the existing input buffer,
and voice/actions. The shell's primary scrollback is never painted over. On
close only the transcript is replayed there, once; no transcript is recorded to
disk. Route Rich output through the supplied Console and suspend via
``run_external`` for other interactive terminal owners.

The coordinator supplies already-authorized packs and a cheap cached access
guard. No purchases, network refresh, spool reader, hooks, or global config
changes occur here. Only validated OpenVegas lifecycle events drive animation.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import queue
import time
import weakref
from bisect import bisect_right
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import TypeVar

from prompt_toolkit.application import Application, in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.key_binding import DynamicKeyBindings, KeyBindings, merge_key_bindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.styles import DynamicStyle, Style, merge_styles

from .controller import EmoteController, State
from .events import Event
from .manifest import LoadedPack
from .render import fit_frame, motion_allowed, prompt_toolkit_fragments

T = TypeVar("T")


@lru_cache(maxsize=2048)
def _cell_width(char: str) -> int:
    return Char(char).width


def _wrapped_height(fragments, width: int) -> int:
    # Sum-of-widths / columns undercounts rows when a wide glyph cannot fit in
    # the final cell. Mirror Window's character-boundary wrapping instead.
    row, column = 1, 0
    for style, text, *_ in fragments:
        if "[ZeroWidthEscape]" in style:
            continue
        for char in text:
            size = _cell_width(char)
            if column + size > width:
                row, column = row + 1, 0
            column += size
    return row


class TurnCancelled(Exception):
    """The user cancelled this owned work task, not the entire chat session."""


class _SafeAnsi:
    """Keep SGR only; discard cursor/OSC/control sequences across write chunks."""

    def __init__(self):
        self.mode = "text"
        self.pending = ""

    def feed(self, text: str) -> str:
        out = []
        for char in text:
            if self.mode == "osc":
                if char == "\x07":
                    self.mode = "text"
                elif char == "\x1b":
                    self.mode = "osc_end"
            elif self.mode == "osc_end":
                self.mode = "text" if char == "\\" else "osc"
            elif self.mode == "escape":
                if char == "[":
                    self.mode, self.pending = "csi", ""
                elif char in "]P_^":
                    self.mode = "osc"
                else:
                    self.mode = "text"
            elif self.mode == "csi":
                if "@" <= char <= "~":
                    if char == "m" and all(c in "0123456789;" for c in self.pending):
                        out.append("\x1b[" + self.pending + char)
                    self.mode = "text"
                elif len(self.pending) < 128:
                    self.pending += char
                else:
                    self.mode = "discard_csi"
            elif self.mode == "discard_csi":
                if "@" <= char <= "~":
                    self.mode = "text"
            elif char == "\x1b":
                self.mode = "escape"
            elif char == "\n" or char == "\t" or (char >= " " and not "\x7f" <= char <= "\x9f"):
                out.append(char)
        return "".join(out)


class _HistoryLexer(Lexer):
    def __init__(self, owner):
        self.owner = owner

    def lex_document(self, document):
        rows = self.owner._rows
        return lambda line: rows[line] if line < len(rows) else []


class _HistoryControl(BufferControl):
    def __init__(self, owner, **kwargs):
        self.owner = owner
        super().__init__(**kwargs)

    def mouse_handler(self, event):
        if event.event_type == MouseEventType.MOUSE_DOWN:
            self.owner.follow_tail = False
            self.owner.app.layout.focus(self)
        return super().mouse_handler(event)


class _HistoryWindow(Window):
    def __init__(self, owner, **kwargs):
        self.owner = owner
        super().__init__(**kwargs)

    def _scroll(self, ui_content, width, height):
        offsets = self.owner._history_line_offsets(ui_content, max(1, width))
        maximum = max(0, offsets[-1] - max(1, height))
        if self.owner.follow_tail:
            row = maximum
        else:
            line = min(self.vertical_scroll, len(offsets) - 2)
            subrow = min(self.vertical_scroll_2, offsets[line + 1] - offsets[line] - 1)
            row = min(maximum, offsets[line] + subrow)
        self.vertical_scroll = bisect_right(offsets, row) - 1
        self.vertical_scroll_2 = row - offsets[self.vertical_scroll]
        self.horizontal_scroll = 0

    def _mouse_handler(self, event):
        if event.event_type in {MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN}:
            self.owner.scroll(-3 if event.event_type == MouseEventType.SCROLL_UP else 3)
            return None
        return super()._mouse_handler(event)


class _ConsoleSink(io.TextIOBase):
    def __init__(self, owner, original):
        self.owner, self.original = owner, original

    @property
    def encoding(self):
        return getattr(self.original, "encoding", "utf-8")

    def isatty(self):
        return self.original.isatty()

    def fileno(self):
        return self.original.fileno()

    def write(self, text):
        if self.owner._closed:
            return len(text)
        self.owner._writes.put(str(text))
        self.owner._loop.call_soon_threadsafe(self.owner._drain_output)
        return len(text)

    def flush(self):
        pass


class OwnedChatCompositor:
    """PromptSession-compatible owner kept alive between ``prompt_async`` calls.

    Construct inside the chat event loop, then ``await start()``. Pass the
    existing PromptSession to retain its buffer, history, validators, and paste
    normalization callbacks. Assign the returned owner as the CLI's prompt
    session; do not also run the original PromptSession's Application.
    """

    _active_owner: weakref.ReferenceType | None = None

    def __init__(
        self,
        session,
        console,
        *,
        session_id: str,
        pack: LoadedPack | None = None,
        completion_pack: LoadedPack | None = None,
        access_guard: Callable[[], bool] = lambda: False,
        voice_active: Callable[[], bool] = lambda: False,
        reduced_motion: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
        input=None,
        output=None,
        replay_on_close: bool = True,
        dock_rows: int = 16,
        companion_enabled: bool = True,
    ):
        if getattr(session, "_openvegas_compositor", None) is not None or session.app.is_running:
            raise RuntimeError("The prompt already has a terminal owner")
        if type(dock_rows) is not int or not 1 <= dock_rows <= 16:
            raise ValueError("dock_rows must be within 1-16")
        self._loop = asyncio.get_running_loop()
        self.session, self.console = session, console
        self.session_id, self._clock = session_id, clock
        self._access_guard, self.voice_active = access_guard, voice_active
        self._replay_on_close, self._dock_rows = replay_on_close, dock_rows
        self._companion_enabled = companion_enabled
        self._access_cleanup: Callable[[], None] | None = None
        self._original_file = console.file
        self._writes: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._sanitizer = _SafeAnsi()
        self._raw: list[str] = []
        self._rows: list[list[tuple[str, str]]] = [[]]
        self._history_metrics_width = None
        self._history_selection_key = None
        self._history_dirty_from = 0
        self._history_offsets = [0, 1]
        # Keep the SGR parser alive between chunks/lines. Its decoded buffer is
        # drained after each write; it never receives terminal control sequences.
        self._ansi = ANSI("")
        self._ansi_parser = self._ansi._parse_corot()
        next(self._ansi_parser)
        self.follow_tail = True
        self._closed = False
        self._suspended = False
        self._app_task = self._tick_task = self._work_task = None
        self._pending: asyncio.Future[str] | None = None
        self._cancel_work: Callable[[], object] | None = None
        self._user_cancelled = False
        self._message, self._rprompt, self._toolbar = "chat: ", "", ""
        self._extra_bindings = None
        self._style = None
        self._ready = asyncio.Event()
        self.default_buffer = session.default_buffer
        self._original_accept = self.default_buffer.accept_handler
        self.history_buffer = Buffer(read_only=True)
        self.history_control = _HistoryControl(
            self, buffer=self.history_buffer, lexer=_HistoryLexer(self), focusable=True,
        )
        self.history_window = _HistoryWindow(
            self, content=self.history_control, wrap_lines=True,
        )
        self.input_control = BufferControl(
            buffer=self.default_buffer,
            input_processors=[BeforeInput(lambda: self._message)],
            focusable=True, focus_on_click=True,
        )
        self.input_window = Window(
            self.input_control, height=Dimension(min=1, max=4), wrap_lines=True,
        )
        self.dock_window = Window(
            FormattedTextControl(self._dock_fragments),
            height=lambda: self.dock_height, style="bg:#15191e", dont_extend_height=True,
        )
        self.scrollbar_window = Window(
            FormattedTextControl(self._scrollbar), width=1, style="bg:#20242a",
        )
        layout = Layout(HSplit([
            VSplit([self.history_window, self.scrollbar_window]),
            Window(FormattedTextControl(self._history_status), height=1),
            self.dock_window,
            self.input_window,
            Window(FormattedTextControl(lambda: self._rprompt), height=1),
            Window(FormattedTextControl(lambda: self._toolbar), height=1, style="class:bottom-toolbar"),
        ]), focused_element=self.input_control)
        base_style = Style.from_dict({
            "bottom-toolbar": "bg:#202020 #d0d0d0",
            "bottom-toolbar.voice-chip": "bg:#2b2f36 #f5f7fa bold",
            "bottom-toolbar.voice-chip-active": "bg:#123a24 #caffdb bold",
        })
        self.app = Application(
            layout=layout, full_screen=True, mouse_support=True,
            key_bindings=merge_key_bindings([
                DynamicKeyBindings(lambda: self._extra_bindings), self._bindings(),
            ]),
            style=DynamicStyle(lambda: merge_styles([base_style, self._style]) if self._style else base_style),
            input=input if input is not None else session.app.input,
            output=output if output is not None else session.app.output,
            refresh_interval=None,
        )
        self._reduced_motion = (
            not motion_allowed(is_tty=console.is_terminal)
            if reduced_motion is None else reduced_motion
        )
        self.controller: EmoteController | None = None
        self._enabled = False
        self._has_pack = pack is not None
        self.set_packs(pack, completion_pack, access_guard=access_guard)
        self.default_buffer.accept_handler = self._accept
        session._openvegas_compositor = self

    def _bindings(self):
        bindings = KeyBindings()

        @bindings.add("enter", filter=Condition(lambda: self.app.layout.has_focus(self.input_control)), eager=True)
        def accept(event):
            if self._pending is not None and not self._pending.done():
                self.default_buffer.validate_and_handle()

        @bindings.add("pageup", eager=True)
        def up(event):
            self.scroll(-max(1, self._history_height() - 1))

        @bindings.add("pagedown", eager=True)
        def down(event):
            self.scroll(max(1, self._history_height() - 1))

        @bindings.add("c-end", eager=True)
        def latest(event):
            self.jump_to_latest()

        @bindings.add("c-c", eager=True)
        def interrupt(event):
            if event.current_buffer.selection_state:
                self.app.clipboard.set_data(event.current_buffer.copy_selection())
                return
            if self._work_task is not None and not self._work_task.done():
                self.cancel_turn()
            elif self._pending is not None and not self._pending.done():
                self._pending.set_exception(KeyboardInterrupt())

        @bindings.add("c-d", filter=Condition(lambda: not self.default_buffer.text), eager=True)
        def eof(event):
            self.cancel_turn()
            if self._pending is not None and not self._pending.done():
                self._pending.set_exception(EOFError())

        @bindings.add("escape")
        def focus_input(event):
            self.app.layout.focus(self.input_control)

        return bindings

    async def start(self):
        if self._closed:
            raise RuntimeError("Compositor is closed")
        if self._app_task is not None:
            return self
        active = type(self)._active_owner
        if active is not None and active() is not None and active() is not self:
            raise RuntimeError("Another OpenVegas compositor owns the terminal")
        type(self)._active_owner = weakref.ref(self)
        self.console.file = _ConsoleSink(self, self._original_file)
        self._app_task = asyncio.create_task(self.app.run_async(
            pre_run=self._ready.set, set_exception_handler=False,
        ))
        ready = asyncio.create_task(self._ready.wait())
        try:
            await asyncio.wait({self._app_task, ready}, return_when=asyncio.FIRST_COMPLETED)
            if self._app_task.done():
                await self._app_task
                raise EOFError("Terminal closed before compositor startup")
            self._app_task.add_done_callback(self._application_finished)
            self._tick_task = asyncio.create_task(self._tick_loop())
        except BaseException:
            await self.close()
            raise
        finally:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)
        return self

    def _application_finished(self, task):
        if not self._closed:
            self.cancel_turn()
            if self.controller is not None:
                self.controller.close()
            if self._tick_task is not None:
                self._tick_task.cancel()
            if self._pending is not None and not self._pending.done():
                self._pending.set_exception(EOFError("Terminal disconnected"))

    async def prompt_async(
        self, message="chat: ", *, key_bindings=None, multiline=False,
        wrap_lines=True, rprompt=None, mouse_support=True, bottom_toolbar=None,
        style=None, refresh_interval=None, default="",
    ) -> str:
        if multiline or not wrap_lines or refresh_interval is not None:
            raise ValueError("Owned chat requires single-line submission, wrapped input, and one refresh owner")
        await self.start()
        if self._pending is not None:
            raise RuntimeError("Only one prompt may wait for input")
        if self._app_task.done():
            raise EOFError("Terminal disconnected")
        self._message, self._rprompt, self._toolbar = message, rprompt or "", bottom_toolbar or ""
        self._extra_bindings, self._style = key_bindings, style
        self.app.mouse_support = Condition(lambda: bool(mouse_support))
        # A draft typed while tools were running wins over an empty next-prompt
        # default. Explicit voice prefill merges at the cursor, never replaces it.
        if default:
            self.default_buffer.insert_text(default)
        self._pending = self._loop.create_future()
        self.app.invalidate()
        try:
            return await self._pending
        finally:
            self._pending = None

    def _accept(self, buffer):
        if self._pending is None or self._pending.done():
            return True
        self._pending.set_result(buffer.text)
        return False

    def request_command(self, command: str) -> bool:
        """Dispatch a toolbar/slash action without replacing the user's draft."""
        if self._pending is None or self._pending.done() or not command.startswith("/"):
            return False
        self._pending.set_result(command)
        return True

    def insert_voice(self, text: str) -> bool:
        """Insert into the real input buffer, even while the user selects history."""
        text = str(text).strip()
        if self._closed or not text:
            return False
        prefix = self.default_buffer.document.text_before_cursor
        suffix = self.default_buffer.document.text_after_cursor
        self.default_buffer.insert_text(
            (" " if prefix and not prefix[-1].isspace() else "") + text
            + (" " if suffix and not suffix[0].isspace() else "")
        )
        self.app.invalidate()
        return True

    async def run_turn(self, work: Awaitable[T], *, on_cancel: Callable[[], object]) -> T:
        """Keep input/history alive during work; never infer successful completion."""
        if self._work_task is not None or self._closed:
            if hasattr(work, "close"):
                work.close()
            raise RuntimeError("An owned turn is already running or closed")
        self._work_task = asyncio.ensure_future(work)
        self._cancel_work, self._user_cancelled = on_cancel, False
        try:
            return await self._work_task
        except asyncio.CancelledError:
            if self._user_cancelled:
                raise TurnCancelled() from None
            on_cancel()
            raise
        finally:
            self._work_task = self._cancel_work = None

    def cancel_turn(self) -> bool:
        if self._work_task is None or self._work_task.done() or self._user_cancelled:
            return False
        self._user_cancelled = True
        try:
            if self._cancel_work is not None:
                self._cancel_work()
        finally:
            self._work_task.cancel()
        return True

    async def run_external(self, function: Callable[[], T]) -> T:
        """Suspend this owner for a synchronous approval/other terminal prompt."""
        await self.start()
        if self._suspended:
            raise RuntimeError("A terminal handoff is already active")

        async def suspended():
            self._suspended = True
            try:
                async with in_terminal():
                    self._drain_output()
                    sink = self.console.file
                    self.console.file = self._original_file
                    try:
                        return function()
                    finally:
                        self.console.file = sink
            finally:
                self._suspended = False
                self.app.invalidate()

        # in_terminal must execute in this Application's context, not whichever
        # unrelated prompt last happened to be current in the caller's task.
        task = self.app.context.run(asyncio.create_task, suspended())
        return await task

    def _allowed(self):
        try:
            return self._access_guard() is True
        except Exception:  # noqa: BLE001 - access failures deny cosmetic rendering
            return False

    def set_packs(self, pack, completion_pack=None, *, access_guard):
        if self._closed:
            return
        if self.controller is not None:
            # Do not recreate lifecycle state: retired turns cannot resurrect.
            if pack is not None:
                self.controller.replace_pack(pack)
            self.controller.replace_completion_pack(completion_pack)
        elif pack is not None:
            self.controller = EmoteController(
                pack, source="openvegas", session_id=self.session_id, clock=self._clock,
                reduced_motion=self._reduced_motion, completion_pack=completion_pack,
            )
        self._access_guard = access_guard
        self._has_pack = pack is not None
        self._enabled = pack is not None and self._allowed()
        if self.controller is not None:
            self.controller.set_enabled(self._enabled)
        self.app.invalidate()

    def _sync_access(self) -> bool:
        enabled = self._has_pack and self._allowed()
        if enabled != self._enabled and self.controller is not None:
            self._enabled = enabled
            self.controller.set_enabled(enabled)
            return True
        return False

    def publish(self, event: Event) -> bool:
        if self._closed or self.controller is None:
            return False
        self._sync_access()
        accepted = self.controller.handle(event)
        if accepted and not self._suspended:
            self.app.invalidate()
        return accepted

    def tick(self):
        if self._closed or self._suspended:
            return
        changed = False
        if self.controller is not None:
            changed = self._sync_access()
            changed = self.controller.tick() or changed
        if changed or self.voice_active():
            self.app.invalidate()

    async def _tick_loop(self):
        while True:
            await asyncio.sleep(0.1)
            self.tick()

    @property
    def dock_height(self):
        """At most a third of the screen; history and a four-row draft win.

        Reserve eight history rows, four input rows and three control rows even
        when the current draft happens to fit on one line. Smaller terminals
        get a legible status line rather than severely downsampled moving art.
        """
        size = self.app.output.get_size()
        if self.controller is None or self.controller.current_state == State.OFF or size.rows < 16:
            return 0
        if not self._companion_enabled and self.controller.current_state != State.COMPLETE:
            return 0
        budget = min(self._dock_rows, size.rows // 3, size.rows - 15)
        return budget if budget >= 8 and size.columns >= 48 else 1

    @property
    def dock_mode(self):
        height = self.dock_height
        return "hidden" if not height else "compact" if height == 1 else "art"

    def _dock_label(self):
        state = self.controller.current_state
        pack = self.controller.completion_pack if state == State.COMPLETE else None
        pack = pack or self.controller.pack
        label = {
            State.ACTIVE: "Working", State.PAUSED: "Waiting for you",
            State.COMPLETE: "Completed", State.ERROR: "Failed",
            State.CANCELLED: "Cancelled", State.IDLE: "Ready",
        }.get(state, "Off")
        return pack.manifest.display_name, label

    def _dock_fragments(self):
        if not self.dock_height or not self._allowed():
            return []
        name, status = self._dock_label()
        if self.dock_mode == "compact":
            return [("fg:#c7d0da", f" {name} | {status} (compact)")]
        columns, rows = self.app.output.get_size().columns, self.dock_height
        frame = fit_frame(
            self.controller.current_frame(), max_columns=max(1, columns - 2),
            max_rows=rows,
        )
        if frame is None:
            return []
        lines = list(split_lines(prompt_toolkit_fragments(frame, background=(21, 25, 30))))
        # Keep the authored canvas/aspect ratio, anchor its feet at the bottom,
        # and use surplus horizontal space for a readable name/state caption.
        top = rows - len(lines)
        label_row = max(0, rows // 2 - 1)
        labels = {label_row: name, label_row + 1: status}
        if rows < 12:
            labels[label_row + 3] = "Resize taller for more detail"
        result = []
        for row in range(rows):
            result.append(("", " "))
            if row >= top:
                result.extend(lines[row - top])
            else:
                result.append(("", " " * frame.width))
            label = labels.get(row, "")
            if label and len(label) <= columns - frame.width - 4:
                result.append(("fg:#c7d0da bold" if row == label_row else "fg:#9aabbc", "  " + label))
            if row < rows - 1:
                result.append(("", "\n"))
        return result

    def _drain_output(self):
        chunks = []
        while True:
            try:
                chunks.append(self._writes.get_nowait())
            except queue.Empty:
                break
        if chunks:
            self.append_output("".join(chunks))

    def append_output(self, text: str):
        """Event-loop-only append; preserve transcript cursor and selected text."""
        if self._closed:
            return
        safe = self._sanitizer.feed(text)
        if not safe:
            return
        self._raw.append(safe)
        for char in safe:
            self._ansi_parser.send(char)
        fragments = list(to_formatted_text(self._ansi))
        self._ansi._formatted_text.clear()
        self._history_dirty_from = min(self._history_dirty_from, len(self._rows) - 1)
        for style, char in fragments:
            if char == "\n":
                self._rows.append([])
            else:
                self._rows[-1].append((style, char))
        buffer = self.history_buffer
        selection = buffer.selection_state
        full_text = buffer.text + "".join(char for _, char in fragments)
        cursor = len(full_text) if self.follow_tail and selection is None else buffer.cursor_position
        buffer.set_document(Document(full_text, cursor), bypass_readonly=True)
        buffer.selection_state = selection
        self.app.invalidate()

    def _history_height(self):
        info = self.history_window.render_info
        return info.window_height if info is not None else max(1, self.app.output.get_size().rows - 10)

    def _selection_key(self):
        selection = self.history_buffer.selection_state
        return None if selection is None else (
            selection.original_cursor_position, self.history_buffer.cursor_position, selection.type,
        )

    def _history_line_offsets(self, content, width):
        selection = self._selection_key()
        # The selection processor can add a highlighted cell to empty lines.
        if width != self._history_metrics_width or selection != self._history_selection_key:
            self._history_offsets = [0]
            self._history_dirty_from = 0
            self._history_metrics_width = width
            self._history_selection_key = selection
        start = self._history_dirty_from
        if start < content.line_count:
            del self._history_offsets[start + 1:]
            for line in range(start, content.line_count):
                self._history_offsets.append(
                    self._history_offsets[-1] + _wrapped_height(content.get_line(line), width)
                )
            self._history_dirty_from = content.line_count
        return self._history_offsets

    def _history_scroll_metrics(self):
        """Recount only the appended tail; completed lines change only on resize."""
        info = self.history_window.render_info
        width = max(1, info.window_width if info else self.app.output.get_size().columns - 1)
        if (self._history_metrics_width != width or self._history_dirty_from < len(self._rows)
                or self._selection_key() != self._history_selection_key):
            content = self.history_control.create_content(width, self._history_height())
            self._history_line_offsets(content, width)
        offsets = self._history_offsets
        line = min(self.history_window.vertical_scroll, len(offsets) - 2)
        subrow = min(self.history_window.vertical_scroll_2, offsets[line + 1] - offsets[line] - 1)
        maximum = max(0, offsets[-1] - self._history_height())
        return offsets, min(maximum, offsets[line] + subrow), maximum

    def _set_history_scroll_row(self, row: int):
        offsets, _, maximum = self._history_scroll_metrics()
        row = max(0, min(maximum, row))
        line = bisect_right(offsets, row) - 1
        self.follow_tail = False
        self.history_window.vertical_scroll = line
        self.history_window.vertical_scroll_2 = row - offsets[line]
        self.app.invalidate()

    def scroll(self, lines: int):
        _, current, _ = self._history_scroll_metrics()
        self._set_history_scroll_row(current + lines)

    def jump_to_latest(self):
        self.history_buffer.exit_selection()
        self.history_buffer.cursor_position = len(self.history_buffer.text)
        self.follow_tail = True
        self.app.layout.focus(self.input_control)
        self.app.invalidate()

    def _history_status(self):
        if self.follow_tail:
            return [("dim", " History: wheel / PgUp to read; select and Ctrl+C to copy")]
        return [("bold", " Reading history | Jump to latest ", self._jump_click)]

    def _jump_click(self, event):
        if event.event_type == MouseEventType.MOUSE_UP:
            self.jump_to_latest()

    def _scrollbar(self):
        height = self._history_height()
        _, current, maximum = self._history_scroll_metrics()
        thumb = min(height - 1, round(current / max(1, maximum) * (height - 1)))
        return [
            ("reverse" if row == thumb else "", " " + ("\n" if row < height - 1 else ""), self._scrollbar_mouse)
            for row in range(height)
        ]

    def _scrollbar_mouse(self, event):
        if event.event_type == MouseEventType.MOUSE_MOVE and event.button == MouseButton.NONE:
            return NotImplemented
        if event.event_type in {MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN}:
            self.scroll(-3 if event.event_type == MouseEventType.SCROLL_UP else 3)
        elif event.event_type in {MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_MOVE, MouseEventType.MOUSE_UP}:
            _, _, maximum = self._history_scroll_metrics()
            self._set_history_scroll_row(round(
                max(0, min(1, event.position.y / max(1, self._history_height() - 1)))
                * maximum
            ))

    async def close(self):
        if self._closed:
            return
        self.cancel_turn()
        if self.controller is not None:
            self.controller.close()
        if self._tick_task is not None:
            self._tick_task.cancel()
            await asyncio.gather(self._tick_task, return_exceptions=True)
        self._drain_output()
        self._closed = True
        if self._pending is not None and not self._pending.done():
            self._pending.set_exception(EOFError("Compositor closed"))
        try:
            if self._app_task is not None:
                if self.app.is_running and not self._app_task.done():
                    self.app.exit()
                with contextlib.suppress(asyncio.CancelledError, EOFError):
                    await self._app_task
        finally:
            self.console.file = self._original_file
            self.default_buffer.accept_handler = self._original_accept
            self.session._openvegas_compositor = None
            if self._access_cleanup is not None:
                self._access_cleanup()
                self._access_cleanup = None
            active = type(self)._active_owner
            if active is not None and active() is self:
                type(self)._active_owner = None
            if self._replay_on_close and self._raw:
                self._original_file.write("".join(self._raw) + "\x1b[0m")
                self._original_file.flush()
            self._raw.clear()

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.close()


async def create_owned_chat(
    session, console, *, session_id, voice_active=lambda: False, mode="auto",
    selection=None, library_factory=None, lease_factory=None,
) -> OwnedChatCompositor | None:
    """Activate only equipped, server-verified art (or an explicit empty owner).

    Saved selection is a trigger, never authorization. This path deliberately
    uses an EMPTY base catalog: no public preview/free fallback can accidentally
    authorize private frames after refund, expiry, account switch or revocation.
    Sync/refresh are bounded by RemoteLibrary; frame checks are cached/local.
    """
    from .manifest import PackError
    from .online import LeaseRefresher, remote_library
    from .resources import Catalog, PackRepository
    from .selection import SelectionStore

    mode = str(mode).strip().lower()
    if mode in {"0", "false", "no", "off"}:
        return None
    explicit = mode in {"1", "true", "yes", "on"}
    if mode != "auto" and not explicit:
        raise ValueError("Compositor mode must be auto, on, or off")
    library = lease = owner = None
    try:
        selection = selection if selection is not None else SelectionStore()
        slots, _ = selection.snapshot()
        if not any(slots.values()) and not explicit:
            return None
        packs = {"companion": None, "completion": None}
        guard = lambda: False
        if any(slots.values()):
            console.print("[dim]Checking equipped emotes...[/dim]")
            library = (library_factory or remote_library)(selection)
            await library.sync()
            slots, revision = selection.snapshot()
            if not any(slots.values()):
                raise PackError("No server-authorized emote remains equipped")
            catalog = library.catalog(Catalog())
            repository = library.repository(PackRepository())
            for slot, pack_id in slots.items():
                if pack_id is not None:
                    if not library.authorize(pack_id):
                        raise PackError("Emote authorization required")
                    loaded = repository.load(catalog.resource_for(pack_id))
                    if loaded.manifest.pack_id != pack_id:
                        raise PackError("Emote identity mismatch")
                    if ("completion" in loaded.manifest.tags) != (slot == "completion"):
                        raise PackError("Emote slot mismatch")
                    packs[slot] = loaded

            def guard():
                return selection.revision() == revision and all(
                    library.authorize(pack_id) for pack_id in slots.values() if pack_id
                )

            if not guard():
                raise PackError("Emote selection or access changed")
            lease = (lease_factory or LeaseRefresher)(library).start()
        owner = OwnedChatCompositor(
            session, console, session_id=session_id,
            pack=packs["companion"] or packs["completion"],
            completion_pack=packs["completion"], companion_enabled=packs["companion"] is not None,
            access_guard=guard, voice_active=voice_active,
        )
        owner._access_cleanup = lease.close if lease is not None else None
        await owner.start()
        return owner
    except asyncio.CancelledError:
        if owner is not None:
            await owner.close()
        if lease is not None:
            lease.close()
        elif library is not None:
            library.close()
        raise
    except (OSError, ValueError, RuntimeError, EOFError):
        if owner is not None:
            await owner.close()
        if lease is not None:
            lease.close()
        elif library is not None:
            library.close()
        console.print("[dim]Same-window emotes unavailable; chat remains usable. Run openvegas emote sync to retry.[/dim]")
        return None
