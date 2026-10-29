"""An owned Textual alternate-screen player, not a host-agent overlay."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.geometry import Size
from textual.widgets import Button, Select, Static

from .catalog import catalog
from .render import StoryboardRenderer
from .schema import CHANNEL_IDS, validate_episode


class PlaybackClock:
    """Monotonic playback without timer drift; pausing never consumes story time."""

    def __init__(self, duration: float, now: Callable[[], float] = time.monotonic):
        self.duration = duration
        self._now = now
        self._started = now()
        self._offset = 0.0
        self.paused = duration <= 0

    @property
    def elapsed(self) -> float:
        delta = 0 if self.paused else self._now() - self._started
        return min(self.duration, max(0.0, self._offset + delta))

    def pause(self) -> None:
        self._offset = self.elapsed
        self.paused = True

    def resume(self) -> None:
        if self.duration <= 0 or not self.paused:
            return
        if self._offset >= self.duration:
            self._offset = 0.0
        self._started = self._now()
        self.paused = False

    def toggle(self) -> None:
        self.resume() if self.paused else self.pause()


class TVApp(App[None]):
    """Mouse and keyboard controls, with a 60x18 mini player and fitted expansion."""

    TITLE = "OpenVegas TV"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("q", "quit", "Quit", priority=True),
        Binding("escape", "quit", "Quit"),
        Binding("space", "pause", "Pause", priority=True),
        Binding("e", "expand", "Expand / Contract", priority=True),
        Binding("n", "next_channel", "Next channel", priority=True),
    ]
    CSS = """
    Screen { align: center middle; background: #162d2b; }
    #tv { width: 60; height: 18; max-width: 100%; max-height: 100%;
          border: round #72bbaa; background: #f4ead5; color: #214d4a; }
    #tv.expanded { width: 100%; height: 100%; }
    #heading { height: 1; text-align: center; text-style: bold; }
    #controls { height: 3; }
    #controls Button { height: 3; min-width: 7; margin: 0; padding: 0 1;
                       background: #214d4a; color: #f4ead5; border: tall #214d4a; }
    #controls Button:focus { border: tall #e77945; text-style: bold; }
    #size-button { width: 12; }
    #pause-button { width: 10; }
    #next-button { width: 8; }
    #quit-button { width: 8; }
    #channels { width: 1fr; min-width: 16; height: 3; }
    SelectCurrent { background: #214d4a; color: #f4ead5; border: tall #214d4a; }
    #scene { height: 1; text-align: center; }
    #art { height: 1fr; overflow: hidden; content-align: center middle; }
    #caption { height: 3; padding: 0 1; content-align: center middle; }
    #status { height: 1; text-align: center; color: #326b62; }
    #too-small { display: none; width: 100%; height: 100%;
                 content-align: center middle; color: #f4ead5; }
    """

    def __init__(self, channel_id: str = "01", *, episode: dict | None = None):
        if channel_id not in CHANNEL_IDS:
            raise ValueError("channel_id must be from 01 to 12")
        super().__init__()
        listing = catalog()
        self.channels = listing["channels"]
        self._episodes = {ep["channel_id"]: ep for ep in listing["episodes"]}
        self._draft_channel = None
        if episode is not None:
            episode = validate_episode(episode)
            self._episodes[episode["channel_id"]] = episode
            channel_id = episode["channel_id"]
            self._draft_channel = channel_id
        self.channel_id = channel_id
        self.expanded = False
        self._tv_mounted = False
        self._small = False
        self._last_paint = None
        self.renderer: StoryboardRenderer | None = None
        self.clock = PlaybackClock(0)
        self._load_channel()

    def compose(self) -> ComposeResult:
        with Vertical(id="tv"):
            yield Static(id="heading", markup=False)
            with Horizontal(id="controls"):
                yield Button("Expand", id="size-button")
                yield Button("Pause", id="pause-button")
                yield Button("Next", id="next-button", tooltip="Next channel, including empty slots")
                yield Select(
                    [(f"{c['id']} {c['name']}", c["id"]) for c in self.channels],
                    value=self.channel_id, allow_blank=False, id="channels",
                )
                yield Button("Quit", id="quit-button")
            yield Static(id="scene", markup=False)
            yield Static(id="art", markup=False)
            yield Static(id="caption", markup=False)
            yield Static(id="status", markup=False)
        yield Static("TV needs at least 60 columns x 18 rows.\nResize to continue. Q / Ctrl+C exits.",
                     id="too-small", markup=False)

    def on_mount(self) -> None:
        self._tv_mounted = True
        self._load_channel()
        self._fit()
        self.set_interval(0.1, self._paint)

    def on_resize(self, event: events.Resize) -> None:
        if self._tv_mounted:
            self._fit(event.size)

    def _fit(self, size: Size | None = None) -> None:
        size = self.size if size is None else size
        self._small = size.width < 60 or size.height < 18
        self.query_one("#tv").display = not self._small
        self.query_one("#too-small").display = self._small
        if self._small:
            self.clock.pause()
        self._last_paint = None
        self.call_after_refresh(self._paint)

    def _load_channel(self) -> None:
        episode = self._episodes.get(self.channel_id)
        self.renderer = StoryboardRenderer(episode) if episode else None
        self.clock = PlaybackClock(episode["duration_seconds"] if episode else 0)
        self._last_paint = None

    def change_channel(self, channel_id: str) -> None:
        if channel_id not in CHANNEL_IDS:
            raise ValueError("channel_id must be from 01 to 12")
        if channel_id == self.channel_id:
            return
        self.channel_id = channel_id
        self._load_channel()
        if self._small:
            self.clock.pause()
        if self._tv_mounted:
            self.query_one("#channels", Select).value = channel_id
            self._paint()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "channels" and isinstance(event.value, str):
            self.change_channel(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "size-button": self.action_expand,
            "pause-button": self.action_pause,
            "next-button": self.action_next_channel,
            "quit-button": self.action_quit,
        }
        action = actions.get(event.button.id)
        if action:
            # App.action_quit is async; exit directly rather than discard its coroutine.
            if event.button.id == "quit-button":
                self.exit()
            else:
                action()

    def action_pause(self) -> None:
        if not self._small and self.renderer:
            self.clock.toggle()
            self._last_paint = None
            self._paint()

    def action_expand(self) -> None:
        self.expanded = not self.expanded
        self.query_one("#tv").set_class(self.expanded, "expanded")
        self.query_one("#size-button", Button).label = "Contract" if self.expanded else "Expand"
        self._last_paint = None
        self.call_after_refresh(self._paint)

    def action_next_channel(self) -> None:
        self.change_channel(CHANNEL_IDS[(CHANNEL_IDS.index(self.channel_id) + 1) % len(CHANNEL_IDS)])

    def _paint(self) -> None:
        if not self._tv_mounted or self._small:
            return
        art = self.query_one("#art", Static)
        elapsed = self.clock.elapsed
        if self.renderer and elapsed >= self.clock.duration:
            self.clock.pause()
        key = (self.channel_id, int(elapsed * 10), self.clock.paused, art.size)
        if key == self._last_paint or art.size.width < 1 or art.size.height < 1:
            return
        self._last_paint = key
        channel = self.channels[CHANNEL_IDS.index(self.channel_id)]
        draft = " / LOCAL DRAFT" if self._draft_channel == self.channel_id else ""
        self.query_one("#heading", Static).update(f"OpenVegas TV / {self.channel_id} {channel['name']}{draft}")
        pause = self.query_one("#pause-button", Button)
        pause.disabled = self.renderer is None
        if not self.renderer:
            self.query_one("#scene", Static).update("Reserved channel slot")
            art.update("NO SHOWS YET")
            self._caption("This channel has no playable episodes.")
            self.query_one("#status", Static).update("Local preview / billing disabled / Q exits")
            pause.label = "Pause"
            return
        frame = self.renderer.frame_at(elapsed)
        pause.label = "Replay" if frame["ended"] else "Resume" if self.clock.paused else "Pause"
        self.query_one("#scene", Static).update(frame["scene"]["title"])
        art.update(self.renderer.terminal_at(elapsed, art.size.width, art.size.height))
        self._caption(
            "End of episode. Replay or choose another channel." if frame["ended"] else frame["caption"]
        )
        status = "Ended" if frame["ended"] else "Paused" if self.clock.paused else "Playing"
        self.query_one("#status", Static).update(
            f"{int(elapsed):02d}s / {int(self.clock.duration):02d}s | {status} | Silent animatic | Q exits"
        )

    def _caption(self, value: str) -> None:
        widget = self.query_one("#caption", Static)
        lines = Text(value).wrap(self.console, max(1, widget.content_size.width))
        widget.styles.height = max(3, len(lines))
        widget.update(value)


def watch(channel_id: str = "01", *, episode: dict | None = None) -> None:
    """Run only with a real TTY. Textual restores modes on exit and exceptions."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("TV playback needs an interactive terminal; use tv list --json instead.")
    TVApp(channel_id, episode=episode).run(inline=False, mouse=True)
