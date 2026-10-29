"""Foreground-only game screen. Native hosts keep their own terminal ownership."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import ClassVar

import click
from rich.console import Group
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.strip import Strip
from textual.widgets import Footer, Input, OptionList, Static

from openvegas.casino.constants import HIDDEN_CARD_TOKEN
from openvegas.casino.slots import SYMBOLS
from openvegas.client import OpenVegasClient
from openvegas.emotes.spool import _locked
from openvegas.integrations.credentials import IntegrationError, backend_url, profile
from openvegas.integrations.game_result import amount, net_outcome
from openvegas.integrations.game_table import GAMES, cards, label, table
from openvegas.integrations.native_play import Journal, menu_read, run_round
from openvegas.tui.cards import RED_SUITS, SUIT_SYMBOLS, parse_card_str
from openvegas.tui.confetti import FALLING_CONFETTI_SECONDS, falling_confetti_cells
from openvegas.tui.horse_renderer import HORSE_REPLAY_SECONDS, horse_board
from openvegas.tui.roulette_renderer import (
    SPIN_DURATION_SEC,
    SPIN_WAIT_SECTORS_PER_SEC,
    WHEEL_ORDER,
    _build_frame,
    _number_styles,
    spin_position,
)
from openvegas.tui.slots_renderer import render_reels
from openvegas.tui.theme import ascii_safe_mode

RESULT_HOLD_SECONDS = 6.0
WIN_REVEAL_SECONDS = 0.25
CONFETTI_SECONDS = FALLING_CONFETTI_SECONDS
CONFETTI_FRAME_SECONDS = 1 / 20


@lru_cache(maxsize=120)
def _wheel_frame(index, size):
    return Text.from_markup(_build_frame(0, ball_sector=index, window=size))


def wheel(number=None, *, width=60, height=30):
    """Use the original wheel geometry; short terminals get a colored pocket rail."""
    index = WHEEL_ORDER.index(number) if type(number) is int and number in WHEEL_ORDER else None
    if height < 14 or width < 28:
        rail = Text("\n")
        count = max(3, min(7, width // 5))
        count -= 1 if count % 2 == 0 else 0
        for delta in range(-(count // 2), count // 2 + 1):
            n = WHEEL_ORDER[((index or 0) + delta) % 37]
            rail.append(f" ●{n:>2} " if n == number else f" {n:02}  ",
                        style="bold black on yellow" if n == number else _number_styles(n)[0])
        return Group(Text("Compact wheel / enlarge the terminal for the full circle", style="bold"), rail,
                     Text("Yellow = ball" if index is not None else "Ball awaits your spin"))
    size = (13 if width >= 56 and height >= 30 else 9 if width >= 44 and height >= 26
            else 5 if width >= 44 and height >= 20 else 1)
    return _wheel_frame(index, size).copy()


def card_faces(values, width):
    """White card faces, red/black suits; input already passed the public filter."""
    per_row = max(1, min(6, width // 9))
    rows = []
    for start in range(0, len(values), per_row):
        grid = Table.grid(padding=(0, 1))
        faces = []
        for value in values[start:start + per_row]:
            if value == HIDDEN_CARD_TOKEN:
                faces.append(Text("       \n  ? ?  \n  ? ?  \n  ? ?  \n       ", style="white on #304f6a"))
            else:
                rank, suit = parse_card_str(value)
                symbol = SUIT_SYMBOLS[suit]
                faces.append(Text(f"{rank:<7}\n       \n   {symbol}   \n       \n{rank:>7}",
                                  style=("#bb263b" if suit in RED_SUITS else "#142521") + " on #fff5df"))
        grid.add_row(*faces)
        rows.append(grid)
    return Group(*rows)


def board(game, result, *, width=60, height=30, moving_number=None, race_progress=None):
    """Display only public state, never decks, seeds or unmasked dealer cards."""
    if game not in GAMES:
        return Group(Text("OPENVEGAS / LOBBY", style="bold #e6c486"), Text(""),
                     Text("Choose a game from the menu.", style="bold"),
                     Text("Its cards, wheel, reels or race will appear here."), Text(""),
                     Text("Submitting an amount places one wager.\nEsc / Quit returns to coding.", style="#b9c3c8"))
    settled = result.get("current_state") == "resolved"
    state = result.get("outcome" if settled else "state") or {}
    if not isinstance(state, dict):
        state = {}
    if game == 'horse':
        return horse_board(state, width=width, progress=(1 if settled else 0) if race_progress is None else race_progress,
                           settled=settled)
    if game == "roulette":
        number = state.get("result") if settled else None
        number = number if type(number) is int and number in WHEEL_ORDER else None
        notes = [Text("ROULETTE / EUROPEAN", style="bold #e6c486"),
                 wheel(moving_number if moving_number is not None else number, width=width, height=height),
                 Text("Bet: " + (label(state.get("bet_type") or "choose a bet").removeprefix("bet ")))]
        if state.get("bet_type") == "bet_number" and type(state.get("bet_value")) is int:
            notes.append(Text("Selected number: " + str(state["bet_value"])))
        # A server result is known before its replay ends; do not spoil the spin.
        if settled and moving_number is None:
            notes.append(Text(f"Landed: {number}  |  Payout: {amount(result.get('payout_v'))}"
                              f"  |  Net: {amount(result.get('net_v'), signed=True)}", style="bold"))
        return Group(*notes)
    if game in {"blackjack", "poker", "baccarat"}:
        positions = ("dealer", "community", "player") if game == "poker" else (
            ("banker", "player") if game == "baccarat" else ("dealer", "player"))
        groups = [Text(game.upper(), style="bold #e6c486")]
        if game == "poker" and not settled:
            stage = {"decision": "3 OF 5 SHARED CARDS", "turn": "4 OF 5 SHARED CARDS",
                     "river": "5 OF 5 / READY TO SHOW HANDS"}.get(state.get("phase"))
            if stage:
                groups.append(Text(stage + " / single stake", style="bold #e6c486"))
        for position in positions:
            if game == 'blackjack' and position == 'player' and state.get('hands'):
                from openvegas.casino.blackjack import hand_value
                from rich.table import Table
                columns = 2 if width >= 64 and len(state['hands']) > 1 else 1
                grid = Table.grid(*([''] * columns), padding=(0, 1), expand=True)
                panels = []
                for index, hand in enumerate(state['hands'][:4]):
                    active = not settled and index == state.get('active_hand') and not hand.get('done')
                    total = hand.get('total') if settled else hand_value(hand['cards'])
                    title = f"Hand {index+1} / {total}" + (' / YOUR TURN' if active else '')
                    if settled:
                        title += ' / ' + label(hand.get('result', ''))
                    panels.append(Group(Text(title, style='bold #80ddbb' if active else 'bold'),
                                        card_faces(cards(hand['cards']), width // columns - 2), Text('')))
                for offset in range(0, len(panels), columns):
                    grid.add_row(*panels[offset:offset+columns])
                groups.append(grid)
                if state.get('total_wager_v'):
                    groups.append(Text('Total stake: ' + amount(state['total_wager_v'])))
                continue
            values = cards(state.get(position + "_cards" if settled else position))
            if position == "dealer" and not settled:
                values = ([HIDDEN_CARD_TOKEN] * len(values) if game == "poker" else
                          values[:1] + [HIDDEN_CARD_TOKEN] * max(0, len(values) - 1))
            groups += [Text(position.title(), style="bold"),
                       card_faces(values, width) if values else Text("Waiting for deal"), Text("")]
        if settled:
            groups.append(Text("\n".join(line for line in table(game, result).splitlines()
                                         if line.startswith(("Outcome:", "Payout:", "Net:")))))
        return Group(*groups)
    if game == "slots":
        reels = state.get("reels")
        if not isinstance(reels, list) or len(reels) != 3:
            reels = ["?", "?", "?"]
        reels = [v if isinstance(v, str) and v in SYMBOLS else "?" for v in reels]
        notes = [Text("SLOTS / THREE REELS", style="bold #e6c486"), Text(""),
                 Text.from_markup(render_reels(reels, settled and state.get("hit") is True)),
                 Text(" / ".join(reels)), Text("Result from server" if settled else "Awaiting your spin")]
        if settled:
            notes.append(Text("\n".join(line for line in table(game, result).splitlines()
                                         if line.startswith(("Outcome:", "Payout:", "Net:")))))
        return Group(*notes)
    return Text(table(game, result))


@asynccontextmanager
async def live_session(*, ask=None, broker=None):
    backend = backend_url()
    async with OpenVegasClient() as client:
        client.base_url = backend
        client._http_client.follow_redirects = False
        from openvegas.integrations.game_browser import use_game_login,signed_out_menu
        from openvegas.client import APIError
        use_game_login(client,backend)
        try:
            account = await menu_read(client._request("GET", "/integrations/account"))
        except APIError as exc:
            if exc.status not in {401,403} or ask is None or broker is None:raise
            account=await signed_out_menu(ask,client,broker)
            if account is None:raise IntegrationError('Back to coding. No wager placed.')
        if not account.get("user_id"):
            raise IntegrationError("Log in to your full OpenVegas account before playing.")
        identity = hashlib.sha256((backend + "\n" + account["user_id"]).encode()).hexdigest()
        # Identical lock and journal to native forms: only one human surface at a time.
        with _locked(profile() / "native-play" / identity) as directory:
            yield client, Journal(directory), account


class GameTable(Static):
    """Paint celebration pieces into table lines, not an input-blocking overlay."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.confetti_cells = []
        self.confetti_rows = {}
        self.confetti_key = None

    def on_resize(self):
        if self.app.is_mounted:
            self.call_after_refresh(self.app.draw)

    def set_confetti(self, elapsed, seed=""):
        width, height = self.size.width, self.parent.content_size.height
        ascii_safe = ascii_safe_mode()
        key = (int(elapsed / CONFETTI_FRAME_SECONDS), width, height, seed, ascii_safe) if elapsed is not None else None
        if key == self.confetti_key:
            return
        self.confetti_key = key
        self.confetti_cells = (falling_confetti_cells(width=width, height=height, elapsed=elapsed,
                              seed=seed, ascii_safe=ascii_safe) if elapsed is not None else [])
        self.confetti_rows = {}
        for cell in self.confetti_cells:
            self.confetti_rows.setdefault(cell.y, {})[cell.x] = cell
        self.refresh()

    def render_line(self, y):
        line = super().render_line(y).adjust_cell_length(self.size.width, self.visual_style.rich_style)
        row = y - int(self.parent.scroll_offset.y)
        pieces = self.confetti_rows.get(row, {})
        # Protect entire label/receipt rows, including spaces within "50 $V".
        if not pieces or "$V" in line.text or re.search(r"[A-Za-z]{3,}", line.text):
            return line
        background = self.visual_style.rich_style.bgcolor
        strips, offset = [], 0
        for x, cell in sorted(pieces.items()):
            original = line.crop(x, x + 1)
            if original.cell_length != 1 or original.text != " ":
                continue
            style = next(iter(original)).style or Style()
            # Keep labels, card faces, wheel pockets and selected numbers intact.
            if style.bgcolor is not None and style.bgcolor != background:
                continue
            strips.extend((line.crop(offset, x), Strip([Segment(cell.glyph, style + cell.style)], 1)))
            offset = x + 1
        return Strip.join([*strips, line.crop(offset)]) if strips else line


class GameScreen(App):
    """One round, human input only; alternate-screen restoration belongs to Textual."""
    CSS = """
    Screen { background: #171b1d; color: #ece9e2; }
    #heading { height: 2; padding: 0 1; text-style: bold; color: #e6c486; }
    #result { height: auto; max-height: 9; padding: 0 1; }
    #body { height: 1fr; }
    #canvas { width: 1fr; padding: 0 1; background: #153a35; }
    #table { height: auto; min-height: 100%; }
    #paper-confetti { display: none; width: 100%; height: 100%; }
    #controls { width: 38; padding: 0 1; }
    #prompt { height: auto; max-height: 12; margin-bottom: 1; }
    #choices { height: 1fr; border: none; background: #242a2e; }
    #amount { height: 3; }
    #status { height: auto; max-height: 3; padding: 0 1; color: #b9c3c8; }
    .compact #body { layout: vertical; }
    .compact #canvas { width: 1fr; height: 1fr; }
    .compact #controls { width: 1fr; height: auto; max-height: 55%; }
    .compact #prompt { max-height: 5; }
    .compact #choices { height: auto; max-height: 7; }
    .compact.wager-entry #canvas { display: none; }
    .compact.wager-entry #controls { height: 1fr; max-height: 100%; }
    .compact.wager-entry #prompt { max-height: 12; }
    .compact.result-visible #canvas { display: block; height: 1fr; min-height: 3; }
    .compact.result-visible #controls { height: auto; max-height: 50%; }
    .lobby-menu #prompt { max-height: 3; }
    .lobby-menu #choices { height: 7; min-height: 7; max-height: 7; }
    .lobby-menu #status { max-height: 2; }
    .compact.lobby-menu #canvas, .short.lobby-menu #canvas { display: none; }
    .compact.lobby-menu #controls, .short.lobby-menu #controls { width: 1fr; height: 1fr; max-height: 100%; }
    """
    BINDINGS: ClassVar = [("escape", "leave", "Back to shell"), ("ctrl+c", "leave", "Quit"),
                ("q", "leave", "Quit"), ("m", "motion", "Motion on/off")]

    def __init__(self, *, game=None, preview=False, reduced_motion=False, session_factory=live_session):
        super().__init__()
        self._paper_widget = None
        # Image capability/cell-size discovery must finish before Textual owns
        # input. This constructor runs in the foreground game PTY, not Codex.
        if sys.stdin.isatty() and sys.stdout.isatty():
            if os.getenv('TERM') == 'openvegas-sixel':
                from textual_image.widget import SixelImage
                self._paper_widget = SixelImage
            elif os.getenv('KITTY_WINDOW_ID'):
                from textual_image.widget import TGPImage
                self._paper_widget = TGPImage
        self.game = "roulette" if preview else None
        self.initial_game = game
        self.preview = preview
        self.reduced_motion = reduced_motion
        self.session_factory = session_factory
        self.journal = None
        self.waiting = None
        self.schema = None
        self.options = []
        self.field = "choice"
        self.spinning = False
        self.spin_started = 0.0
        self.result = {}
        self.finished = False
        self.replay_started = None
        self.replay_start_sector = 0
        self.replay_target_sector = 0
        self.active_replay_started = None
        self.last_ball_sector = 0
        self.preview_number = None
        self.settlement_key = None
        self.result_shown_at = None
        self.feedback_frame = None
        self.confetti_active = False
        self.server_wait_started = None
        self.server_wait_message = None

    def compose(self) -> ComposeResult:
        yield Static("OPENVEGAS / TERMINAL TABLE", id="heading", markup=False)
        yield Static(id="result", markup=False)
        with Horizontal(id="body"):
            with VerticalScroll(id="canvas"):
                yield GameTable(id="table", markup=False)
                if self._paper_widget is not None and not self.is_headless:
                    yield self._paper_widget(id='paper-confetti', disabled=True)
            with Vertical(id="controls"):
                yield Static("Loading game account...", id="prompt", markup=False)
                yield Input(placeholder="Amount in $V", id="amount")
                yield OptionList(id="choices")
        yield Static("Quit ends the game and forfeits an unfinished stake. No refunds.", id="status", markup=False)
        yield Footer()

    def on_mount(self):
        self.query_one("#amount").display = False
        self.query_one("#result").display = False
        self.query_one("#choices").disabled = True
        self.set_class(self.size.width < 88, "compact")
        self.set_class(self.size.height < 22, "short")
        self.draw()
        self.set_interval(1 / 20, self.tick)
        if self.preview:
            self.query_one("#heading", Static).update("OPENVEGAS / ROULETTE PREVIEW / NO WALLET OR NETWORK")
            self.query_one("#prompt", Static).update("Animation preview only. No wager, winnings or server result.\nEnter: spin demo. Esc / q: quit.")
            self.show_options(["Spin demo", "Quit"])
        else:
            self.run_worker(self.run_game(), name="one-human-round")

    def on_resize(self, event):
        self.set_class(event.size.width < 88, "compact")
        self.set_class(event.size.height < 22, "short")
        if self.is_mounted:
            self.call_after_refresh(self.draw)

    def draw(self, moving=None):
        if not self.query('#canvas'):
            return
        choosing_game = self.schema is not None and self.schema.__name__ in {"ChooseGame", "ChooseMode"}
        race_progress = None
        if self.preview and moving is None:
            moving = self.preview_number
        # Form/resize redraws must retain the moving ball, not flash the result.
        if not choosing_game and self.game == "roulette" and self.replay_started is not None and not self.reduced_motion:
            if self.active_replay_started != self.replay_started:
                # An already-running relay may still set replay_started directly.
                # Its fresh game child must use the real pocket, not a default.
                outcome = self.result.get("outcome")
                number = outcome.get("result") if isinstance(outcome, dict) else None
                if type(number) is int and number in WHEEL_ORDER:
                    self.replay_target_sector = WHEEL_ORDER.index(number)
                self.replay_start_sector = self.last_ball_sector
                self.active_replay_started = self.replay_started
            elapsed = time.monotonic() - self.replay_started
            if elapsed >= SPIN_DURATION_SEC:
                self.replay_started = None
                self.last_ball_sector = self.replay_target_sector
                moving = WHEEL_ORDER[self.replay_target_sector] if self.preview else None
            else:
                sector = int(spin_position(elapsed, self.replay_target_sector,
                                           start=self.replay_start_sector)) % len(WHEEL_ORDER)
                moving = WHEEL_ORDER[sector]
        if self.game == 'horse' and self.replay_started is not None:
            race_progress = min(1.0, max(0.0, (time.monotonic() - self.replay_started) / HORSE_REPLAY_SECONDS))
            if race_progress >= 1 or self.reduced_motion:
                self.replay_started = None
                race_progress = 1
        if moving is not None:
            self.last_ball_sector = WHEEL_ORDER.index(moving)
        elif self.game == "roulette" and self.result.get("current_state") == "resolved":
            outcome = self.result.get("outcome")
            number = outcome.get("result") if isinstance(outcome, dict) else None
            if type(number) is int and number in WHEEL_ORDER:
                self.last_ball_sector = WHEEL_ORDER.index(number)
        canvas = self.query_one("#canvas")
        self.query_one("#table", Static).update(board(None if choosing_game else self.game, self.result,
            width=max(24, canvas.size.width - 2), height=canvas.size.height, moving_number=moving,
            race_progress=race_progress))
        self.update_feedback()

    def update_feedback(self):
        """Presentation clock only: never choose a move or write to the ledger."""
        # A queued timer/resize can arrive after the terminal view unmounts.
        if not self.query('#result') or not self.query('#table'):
            return
        settled = not self.preview and self.result.get('current_state') == 'resolved' and not self.result.get('error')
        if not settled:
            self.confetti_active = False
            self.set_confetti(None)
            self.set_class(False, 'result-visible')
            self.query_one('#result').display = False
            if self.server_wait_started is not None:
                elapsed = max(0, int(time.monotonic() - self.server_wait_started))
                self.query_one('#status', Static).update(f'{self.server_wait_message} Waiting for server: {elapsed}s. No automatic retry.')
            return
        key = (self.game, json.dumps(self.result, sort_keys=True))
        if key != self.settlement_key:
            self.settlement_key = key
            self.result_shown_at = None
            self.feedback_frame = None
        self.server_wait_started = None
        if self.replay_started is not None:
            self.confetti_active = False
            self.set_confetti(None)
            self.set_class(False, 'result-visible')
            self.query_one('#result').display = False
            horse = self.game == 'horse'
            remaining = max(1, math.ceil((HORSE_REPLAY_SECONDS if horse else SPIN_DURATION_SEC) - (time.monotonic() - self.replay_started)))
            self.query_one('#prompt', Static).update(('Finishing the race.' if horse else 'Finishing the spin.') +
                                                     f' Result in {remaining}s.\nDone / Esc skips to coding.')
            self.query_one('#status', Static).update('Settlement received. Replaying server race checkpoints.' if horse else
                                                     'Settlement received. Landing on the server-confirmed pocket.')
            return
        now = time.monotonic()
        if self.result_shown_at is None:
            self.result_shown_at = now
        elapsed = max(0, now - self.result_shown_at)
        verdict = net_outcome(self.result)
        self.confetti_active = (verdict == 'win' and not self.reduced_motion
                                and WIN_REVEAL_SECONDS <= elapsed < WIN_REVEAL_SECONDS + CONFETTI_SECONDS)
        title = {'win': 'YOU WIN', 'loss': 'YOU LOST THIS ROUND', 'push': 'PUSH / STAKE RETURNED'}.get(verdict, 'ROUND SETTLED')
        content = Text(title + '\n', style='bold bright_green' if verdict == 'win' else 'bold')
        content.append('Payout: ' + amount(self.result.get('payout_v')) +
                       '  |  Net: ' + amount(self.result.get('net_v'), signed=True), style='bold')
        if self.game == 'roulette':
            outcome = self.result.get('outcome') or {}
            number = outcome.get('result') if isinstance(outcome, dict) else None
            if type(number) is int and number in WHEEL_ORDER:
                content.append(f'\nLanded: {number}', style='bold')
        width = max(4, self.size.width - 2)
        frame_key = (key, width)
        panel = self.query_one('#result', Static)
        panel.display = True
        self.set_class(True, 'result-visible')
        if self.feedback_frame != frame_key:
            panel.update(content)
            self.feedback_frame = frame_key
        self.set_confetti(
            elapsed - WIN_REVEAL_SECONDS if self.confetti_active else None, seed=str(key))
        remaining = max(0, math.ceil(RESULT_HOLD_SECONDS - elapsed))
        self.query_one('#prompt', Static).update(f'Returning to coding in {remaining}s.\nDone / Esc returns now. No repeat wager.')
        self.query_one('#status', Static).update('Result confirmed by the ledger. No further action or wager will be submitted.')
        if remaining == 0:
            if self.schema is not None and self.schema.__name__ == 'RoundResult':
                self.answer('done')  # Display acknowledgement only, never RoundAction.
            elif self.finished:
                self.exit()

    def set_confetti(self, elapsed, seed=''):
        table = self.query_one('#table', GameTable)
        images = self.query('#paper-confetti')
        if not images:
            table.set_confetti(elapsed, seed)
            return
        image = images.first()
        active = elapsed is not None and not self.reduced_motion
        image.display = active
        table.display = not active
        table.set_confetti(None)
        if not active:
            if getattr(self, '_paper_key', None) is not None:
                image.image = None
            self._paper_key = None
            return
        from .paper_confetti import paper_frame
        canvas = self.query_one('#canvas')
        width, height = max(1, canvas.content_size.width * 8), max(1, canvas.content_size.height * 16)
        ratio = min(1, 768 / width, 512 / height)
        width, height = max(1, round(width * ratio)), max(1, round(height * ratio))
        key = (int(elapsed / CONFETTI_FRAME_SECONDS), width, height, seed)
        if key != getattr(self, '_paper_key', None):
            self._paper_key = key
            image.image = paper_frame(width, height, elapsed, seed)

    def present_table(self, game, result):
        duplicate = game == self.game and result == self.result
        self.game, self.result = game, result
        settled = result.get('current_state') == 'resolved'
        # A resolvable table is not settlement: keep waiting motion/progress alive.
        if settled and not duplicate:
            outcome = result.get('outcome')
            number = outcome.get('result') if isinstance(outcome, dict) else None
            self.start_result_replay(number)
        self.draw()

    def tick(self):
        self.update_feedback()
        if (self.reduced_motion
                or (self.schema is not None and self.schema.__name__ == "ChooseGame")):
            return
        if self.replay_started is not None:
            self.draw()
            return
        if self.game != 'roulette' or not self.spinning:
            return
        elapsed = time.monotonic() - self.spin_started
        self.draw(WHEEL_ORDER[int(elapsed * SPIN_WAIT_SECTORS_PER_SEC) % len(WHEEL_ORDER)])

    def start_result_replay(self, number):
        self.spinning = False
        self.replay_started = None
        if self.game == "roulette" and not self.reduced_motion and type(number) is int and number in WHEEL_ORDER:
            self.replay_start_sector = self.last_ball_sector
            self.replay_target_sector = WHEEL_ORDER.index(number)
            self.replay_started = time.monotonic()
        elif self.game == 'horse' and not self.reduced_motion and self.result.get('outcome', {}).get('checkpoints'):
            self.replay_started = time.monotonic()

    def action_motion(self):
        self.reduced_motion = not self.reduced_motion
        if self.reduced_motion:
            self.replay_started = None
        self.draw()

    def show_options(self, options):
        picker = self.query_one("#choices", OptionList)
        self.options = options
        picker.clear_options()
        picker.add_options([Text("Confirm above" if v is True else "Cancel / back" if v is False
                                else 'Quick Play       (vs House)' if v == 'quick_play'
                                else 'Arena            (Coming Soon)' if v == 'arena'
                                else 'Sign in/up       (Browser)' if v == 'sign_in'
                                else 'Purchase $V      (Browser)' if v == 'purchase_v'
                                else 'Horse Racing' if v == 'horse'
                                else str(v).capitalize() + ' / add ' + amount((self.result.get('state') or {}).get('base_wager_v'))
                                if self.game == 'blackjack' and v in {'split', 'double'}
                                else {"call": "Continue", "fold": "Fold / give up hand",
                                      "reveal_river": "Reveal last card", "showdown": "Show hands"}.get(v, str(v).capitalize())
                                if self.game == "poker" and v in {"call", "fold", "reveal_river", "showdown"}
                                else str(v).replace("_", " ").capitalize()) for v in options])
        picker.disabled = False
        picker.display = True
        picker.highlighted = 0
        picker.focus()

    async def ask(self, message, schema):
        self.spinning = False
        name = schema.__name__
        self.server_wait_started = None
        self.set_class(name == "Wager", "wager-entry")
        self.set_class(name in {"ChooseGame", "ChooseMode"}, "lobby-menu")
        if name in {"ChooseGame", "ChooseMode"}:
            self.game = None
            self.result = {}
            self.replay_started = None
            self.active_replay_started = None
            self.last_ball_sector = 0
            self.settlement_key = None
            self.result_shown_at = None
            self.server_wait_started = None
            if name == 'ChooseGame':
                message = 'Quick Play / vs House\nChoose a game. Esc / Quit returns to coding.'
        elif self.journal and self.journal.data:
            self.game = self.journal.data["game"]
            self.result = self.journal.data.get("result", {})
        elif name == 'Wager' and self.game is None and self.initial_game in GAMES:
            # A named command selected the game already; catalog validation has
            # completed before the wager form is requested.
            self.game = self.initial_game
        self.draw()
        # The public table is on the left; keep only instructions beside it.
        if name == "RoundAction":
            message = "Choose your next move.\nQuit ends this game and forfeits the unfinished stake."
            if self.game == 'blackjack':
                message = ('Hit: another card.\nStand: finish this hand.\n'
                           'Split: matching pair only, adds a stake.\n'
                           'Double: two-card hand only, adds a stake and takes one card.\n'
                           'Quit forfeits the total unfinished stake.')
                if self.result.get('notice'):
                    message += '\n' + str(self.result['notice'])
            if self.game == "poker":
                phase = (self.result.get("state") or {}).get("phase")
                guidance = {
                    "decision": "Three shared cards are down. Continue to see the next card, or fold.",
                    "turn": "One card left. Inspect your hand, then reveal the last card.",
                    "river": "All five shared cards are down. Show hands to see who wins.",
                }.get(phase)
                if guidance:
                    message = guidance + "\nNo additional bet. Quit forfeits the unfinished stake."
            if schema.model_json_schema()["properties"]["choice"].get("enum") == ["spin", "quit"]:
                message = "Bet selected. Choose Spin to request the result.\nQuit ends this game and forfeits the stake."
        prompt = Text(message)
        if name == "Wager":
            # Keep wire messages plain; style validation feedback only in our screen.
            prompt = Text()
            for index, line in enumerate(message.splitlines()):
                if index:
                    prompt.append("\n")
                if line.startswith("Error: "):
                    prompt.append(line.removeprefix("Error: "), style="bold #ff7373")
                else:
                    prompt.append(line)
        self.query_one("#prompt", Static).update(prompt)
        self.query_one("#status", Static).update("" if name == "Wager" else
            "Your turn / no automatic move. Esc or quit returns to the shell.")
        self.schema = schema
        properties = schema.model_json_schema()["properties"]
        self.field = next(iter(properties))
        spec = properties[self.field]
        self.waiting = asyncio.get_running_loop().create_future()
        amount = self.query_one("#amount", Input)
        picker = self.query_one("#choices", OptionList)
        amount.display = name == "Wager"
        if name == "Wager":
            self.query_one("#choices").display = False
            amount.disabled = False
            amount.value = ""
            amount.focus()
        else:
            options = [False, True] if name == "Confirm" else spec.get("enum", [spec.get("const")])
            if name == "ChooseGame" and self.initial_game in options:
                options = [self.initial_game, *(v for v in options if v != self.initial_game)]
            self.show_options(options)
        self.update_feedback()
        try:
            return await self.waiting
        finally:
            self.waiting = None
            self.schema = None
            picker.disabled = True
            amount.display = False

    def answer(self, value):
        if self.waiting is not None and not self.waiting.done():
            if self.schema.__name__ == "ChooseGame" and value in GAMES:
                self.game = value
                self.result = {}
                self.replay_started = None
                self.draw()
            self.waiting.set_result(self.schema(**{self.field: value}))
            self.query_one("#choices").disabled = True
            self.query_one("#amount").disabled = True

    def on_option_list_option_selected(self, event):
        if self.preview:
            if event.option_index == 1:
                self.exit()
            else:
                # Same presentation as gameplay, but no account or invented payout.
                self.preview_number = 17
                self.start_result_replay(17)
                self.draw()
        elif self.finished:
            self.exit()
        elif event.option_index < len(self.options):
            self.answer(self.options[event.option_index])

    def on_input_submitted(self, event):
        if self.schema is not None and self.schema.__name__ == "Wager":
            self.answer(event.value)

    def action_leave(self):
        if (self.waiting is not None and not self.waiting.done()
                and self.schema.__name__ in {"RoundAction", "Number"}):
            self.answer("quit")
            return
        # During an in-flight write, cancellation must retain its recovery key.
        self.exit()

    async def progress(self, message):
        self.query_one("#status", Static).update(message)
        pending = message.startswith(('Requesting the spin result', 'Settling the server result', 'Sending your chosen move',
                                      'Sending your chosen horse'))
        self.server_wait_message = message.split('. Server response limit:')[0] + '.'
        self.server_wait_started = time.monotonic() if pending else None
        if pending and self.game == 'roulette' and message.startswith(('Requesting the spin result', 'Settling the server result')):
            if not self.spinning:
                self.spin_started = time.monotonic()
            self.spinning = True
        self.update_feedback()

    async def run_game(self):
        broker=None
        try:
            from openvegas.integrations.broker import Broker
            from openvegas.integrations.game_browser import browser_action
            if self.session_factory is live_session:
                broker=Broker()
                session=self.session_factory(ask=self.ask,broker=broker)
            else:
                session=self.session_factory()
            async with session as (client, journal, account):
                self.journal = journal
                self.query_one("#heading", Static).update("OPENVEGAS / TERMINAL TABLE / Entry balance: " + amount(account["balance"]))
                async def present(game, result):
                    self.present_table(game, result)
                async def browser_control(action):
                    return await browser_action(action,self.ask,client,broker)
                outcome = await run_round(client, self.ask, journal, account, progress=self.progress,
                                          wager_on_submit=True, present=present, requested_game=self.initial_game,
                                          show_lobby=True,browser_action=browser_control if broker else None)
                self.spinning = False
                if journal.data:
                    self.game = journal.data["game"]
                    self.result = journal.data.get("result", {})
                    if outcome["status"] == "resolved":
                        self.result = {**self.result, "current_state": "resolved"}
                        number = self.result.get("outcome", {}).get("result")
                        if self.game == "roulette" and self.replay_started is None:
                            self.start_result_replay(number)
                self.draw()
                self.query_one("#prompt", Static).update(outcome["message"])
        except IntegrationError as exc:
            self.query_one("#prompt", Static).update(str(exc))
        except Exception:  # noqa: BLE001 - Never print API payloads/credentials in the screen.
            self.query_one("#prompt", Static).update("Game stopped safely. Check login/connectivity. Recovery is retained; no automatic retry or refund.")
        finally:
            if broker is not None:
                await broker.close()
            self.spinning = False
            self.server_wait_started = None
        self.finished = True
        self.query_one("#status", Static).update("No repeat wager. Enter / Esc: return to shell; fg resumes a paused host.")
        self.show_options(["Back to shell"])
        self.update_feedback()


def require_foreground():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise click.ClickException("Run play in your terminal shell, not an AI tool or captured ! command. "
                                   "Quit the host game form, press Ctrl+Z to pause Codex, then run ov play roulette; fg")
    if os.name == "posix":
        try:
            if os.tcgetpgrp(sys.stdin.fileno()) != os.getpgrp():
                raise click.ClickException("OpenVegas must own the foreground terminal before drawing. No game started.")
        except OSError:
            raise click.ClickException("Cannot verify foreground terminal ownership. No game started.") from None


@click.command("play")
@click.argument("game", required=False, type=click.Choice(sorted(GAMES)))
@click.option("--preview", is_flag=True, help="No-network roulette animation preview; no wallet or wager.")
@click.option("--reduced-motion", is_flag=True, help="Keep the table still; all game controls remain available.")
def play(game, preview, reduced_motion):
    """Colored game screen in this terminal, not a new tab or AI host.

    From idle Codex on macOS/Linux: Ctrl+Z, then ov play roulette; fg
    Esc exits the table and fg resumes the same host. Quit never refunds a wager.
    Native $openvegas play forms remain separate; they cannot animate this screen.
    """
    if preview and game not in {None, "roulette"}:
        raise click.UsageError("The no-network preview currently shows roulette only.")
    require_foreground()
    GameScreen(game=game, preview=preview, reduced_motion=reduced_motion).run(mouse=False)
    click.echo("Table closed. Existing wagers are not refunded. Use fg if you paused your coding host.")
