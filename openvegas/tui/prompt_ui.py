"""Inline guided UI for OpenVegas (non-fullscreen terminal flow)."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import uuid
import webbrowser
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.parse import urlparse

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from openvegas.casino.constants import HIDDEN_CARD_TOKEN, min_game_wager_v
from openvegas.client import APIError, OpenVegasClient
from openvegas.compact_uuid import encode_compact_uuid
from openvegas.config import load_config
from openvegas.tui.cards import render_hand
from openvegas.games.base import GameResult
from openvegas.games.horse_racing import HorseRacing
from openvegas.games.skill_shot import SkillShotGame
from openvegas.tui.confetti import render_result_panel
from openvegas.tui.hints import verify_hint_for_result
from openvegas.tui.qr_render import qr_half_block, qr_width
from openvegas.tui.roulette_renderer import animate_spin as animate_roulette_spin
from openvegas.tui.roulette_renderer import render_result as render_roulette_result
from openvegas.tui.slots_renderer import render_reels as render_slots_reels
from openvegas.tui.wizard_state import WizardState, validate_inputs

ACTION_CHOICES = [
    "Balance",
    "History",
    "Deposit",
    "Play",
    "Verify",
]
GAME_CHOICES = ["horse", "skillshot"]
CARD_GAME_CHOICES = ["blackjack", "roulette", "slots", "poker", "baccarat"]
GAME_CHOICES = ["horse", "skillshot", *CARD_GAME_CHOICES]
GAME_LABELS = {
    "horse": "horse",
    "skillshot": "skillshot",
    "blackjack": "blackjack",
    "roulette": "roulette",
    "slots": "slots",
    "poker": "poker (casino hold'em)",
    "baccarat": "baccarat",
}
HORSE_BET_CHOICES = ["win", "place", "show"]
MIN_BILLING_USD = Decimal("10")
MAX_BILLING_USD = Decimal("500")


def _truthy(raw: str) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _win_always_enabled() -> bool:
    raw = str(os.getenv("OPENVEGAS_WIN_ALWAYS", "")).strip()
    if raw:
        return _truthy(raw)
    return _truthy(os.getenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "0"))


def _format_v_amount(value: Any) -> str:
    try:
        return f"{Decimal(str(value)).quantize(Decimal('0.01'))}"
    except Exception:
        return "0.00"


@dataclass
class RenderOptions:
    fast_mode: bool = False
    duration_sec: float | None = None
    no_render: bool = False
    timeout_sec: float = 15.0


def _call_accepts_opts(fn: Callable[..., Any]) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    params = list(sig.parameters.values())
    if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params):
        return True
    positional = [
        p for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    # Bound methods exclude `self`; opts is the 3rd positional arg.
    return len(positional) >= 3


def _invoke_sync_render(fn: Callable[..., Any], result: GameResult, console: Console, opts: RenderOptions) -> None:
    if _call_accepts_opts(fn):
        fn(result, console, opts)
        return
    fn(result, console)


async def _invoke_async_render(
    fn: Callable[..., Any],
    result: GameResult,
    console: Console,
    opts: RenderOptions,
) -> None:
    if _call_accepts_opts(fn):
        await fn(result, console, opts)
        return
    await fn(result, console)


async def execute_render(renderer: Any, result: GameResult, console: Console, opts: RenderOptions) -> dict:
    """Execute renderer exactly once, with timeout protection.

    Note: `asyncio.to_thread()` timeout does not kill the worker thread. Renderers
    used here must stay bounded and short-running.
    """
    if opts.no_render:
        return {"rendered": False, "reason": "disabled"}

    render_async = getattr(renderer, "render_async", None)
    if callable(render_async):
        if inspect.iscoroutinefunction(render_async):
            await asyncio.wait_for(
                _invoke_async_render(render_async, result, console, opts),
                timeout=opts.timeout_sec,
            )
            return {"rendered": True}
        await asyncio.wait_for(
            asyncio.to_thread(_invoke_sync_render, render_async, result, console, opts),
            timeout=opts.timeout_sec,
        )
        return {"rendered": True}

    render = getattr(renderer, "render", None)
    if not callable(render):
        return {"rendered": False, "reason": "missing-renderer"}
    if inspect.iscoroutinefunction(render):
        await asyncio.wait_for(
            _invoke_async_render(render, result, console, opts),
            timeout=opts.timeout_sec,
        )
        return {"rendered": True}

    await asyncio.wait_for(
        asyncio.to_thread(_invoke_sync_render, render, result, console, opts),
        timeout=opts.timeout_sec,
    )
    return {"rendered": True}


def _is_simulated_checkout_url(url: str) -> bool:
    try:
        return urlparse(str(url or "")).netloc.lower() == "checkout.openvegas.local"
    except Exception:
        return False


class InlinePromptUI:
    """Sequential, inline wizard-like prompt flow."""

    def __init__(
        self,
        *,
        client: OpenVegasClient | None = None,
        console: Console | None = None,
        render_options: RenderOptions | None = None,
    ):
        self.client = client or OpenVegasClient()
        self.console = console or Console()
        self.state = WizardState()
        self.render_options = render_options or RenderOptions(
            fast_mode=True,
            duration_sec=10.0,
            no_render=False,
            timeout_sec=15.0,
        )
        self._last_is_win = False

    def _clear_horse_quote_state(self) -> None:
        self.state.horse_quote_id = ""
        self.state.horse_quote_expires_at = ""
        self.state.horse_quote_board_hash = ""
        self.state.horse_quote_rows = []
        self.state.horse_quote_selected = {}

    def _steps_for_state(self) -> list[str]:
        steps = ["action"]
        if self.state.action == "Play":
            steps.append("game")
            if self.state.game == "horse":
                steps.append("bet_type")
            steps.append("inputs")
        elif self.state.action in {"Deposit", "Verify"}:
            steps.append("inputs")
        steps.append("review")
        return steps

    def _ask_choice(
        self,
        label: str,
        choices: list[str],
        current: str,
        *,
        allow_back: bool,
        display_labels: dict[str, str] | None = None,
    ) -> tuple[str | None, str | None]:
        self.console.print(f"[bold blue]{label}[/bold blue]")
        for i, choice in enumerate(choices, start=1):
            marker = "●" if choice == current else "○"
            show = display_labels.get(choice, choice) if display_labels else choice
            self.console.print(f"  [{i}] {marker} {show}")
        footer = "Type number"
        if allow_back:
            footer += ", b=Back"
        footer += ", q=Quit"
        self.console.print(f"[dim]{footer}[/dim]")
        default_idx = str(choices.index(current) + 1) if current in choices else "1"
        allowed = [str(i) for i in range(1, len(choices) + 1)] + ["q"]
        if allow_back:
            allowed.append("b")
        picked = Prompt.ask(
            "Select option",
            choices=allowed,
            default=default_idx,
        )
        if picked == "q":
            return None, "quit"
        if picked == "b":
            return None, "back"
        return choices[int(picked) - 1], None

    def _ask_input(self, label: str, *, default: str, allow_back: bool) -> tuple[str | None, str | None]:
        hint = "[dim]Type value"
        if allow_back:
            hint += ", b=Back"
        hint += ", q=Quit[/dim]"
        self.console.print(hint)
        val = Prompt.ask(label, default=default).strip()
        if val.lower() == "q":
            return None, "quit"
        if allow_back and val.lower() == "b":
            return None, "back"
        return val, None

    def _run_step(self, step: str, *, allow_back: bool) -> str:
        if step == "action":
            if self.state.action not in ACTION_CHOICES:
                self.state.action = ACTION_CHOICES[0]
            value, signal = self._ask_choice("Action", ACTION_CHOICES, self.state.action, allow_back=allow_back)
            if signal:
                return signal
            self.state.action = str(value)
            if self.state.action != "Play":
                self._clear_horse_quote_state()
            return "next"

        if step == "game":
            value, signal = self._ask_choice(
                "Game",
                GAME_CHOICES,
                self.state.game,
                allow_back=allow_back,
                display_labels=GAME_LABELS,
            )
            if signal:
                return signal
            self.state.game = str(value)
            if self.state.game != "horse":
                self._clear_horse_quote_state()
            return "next"

        if step == "bet_type":
            value, signal = self._ask_choice(
                "Bet type",
                HORSE_BET_CHOICES,
                self.state.bet_type,
                allow_back=allow_back,
            )
            if signal:
                return signal
            self.state.bet_type = str(value)
            self._clear_horse_quote_state()
            return "next"

        if step == "inputs":
            if self.state.action == "Deposit":
                value, signal = self._ask_input("Amount (USD)", default=self.state.amount, allow_back=allow_back)
                if signal:
                    return signal
                self.state.amount = str(value)
                try:
                    amount = Decimal(self.state.amount)
                except Exception:
                    self.console.print("[red]Invalid amount. Use a number like 10 or 20.50.[/red]")
                    return "stay"
                if amount < MIN_BILLING_USD or amount > MAX_BILLING_USD:
                    self.console.print(
                        f"[red]Amount must be between ${MIN_BILLING_USD:.2f} and ${MAX_BILLING_USD:.2f}. Try again.[/red]"
                    )
                    return "stay"
                return "next"

            if self.state.action == "Play":
                amount_label = "Budget cap ($V)" if self.state.game == "horse" else "Stake ($V)"
                value, signal = self._ask_input(amount_label, default=self.state.amount, allow_back=allow_back)
                if signal:
                    return signal
                self.state.amount = str(value)
                try:
                    stake = Decimal(self.state.amount)
                except Exception:
                    self.console.print("[red]Invalid stake. Use a number like 50 or 125.5.[/red]")
                    return "stay"
                if stake < min_game_wager_v():
                    self.console.print(f"[red]Stake must be at least {min_game_wager_v():.2f} $V. Try again.[/red]")
                    return "stay"
                if self.state.game == "horse":
                    quote = asyncio.run(self._fetch_horse_quote())
                    if quote is None:
                        return "stay"
                    self._print_horse_quote_board(quote)
                    value, signal = self._ask_input("Horse number", default=self.state.horse, allow_back=allow_back)
                    if signal:
                        return signal
                    self.state.horse = str(value)
                    selected = self._selected_horse_row()
                    if not selected:
                        self.console.print("[red]Selected horse is not present in quote board.[/red]")
                        return "stay"
                    if not bool(selected.get("selectable", False)):
                        self.console.print("[red]Selected horse is not selectable for this budget.[/red]")
                        return "stay"
                    self.state.horse_quote_selected = selected
                return "next"

            if self.state.action == "Verify":
                value, signal = self._ask_input("Game ID", default=self.state.game_id, allow_back=allow_back)
                if signal:
                    return signal
                self.state.game_id = str(value)
                return "next"
            return "next"

        if step == "review":
            self._print_review()
            answer = Prompt.ask(
                "Confirm [c], Back [b], Quit [q]",
                choices=["c", "b", "q"],
                default="c",
            )
            if answer == "q":
                return "quit"
            if answer == "b":
                return "back"
            return "confirm"

        return "next"

    def _print_review(self) -> None:
        lines = [f"Action: {self.state.action}"]
        if self.state.action == "Play":
            lines.append(f"Game: {GAME_LABELS.get(self.state.game, self.state.game)}")
            if self.state.game == "horse":
                selected = self.state.horse_quote_selected or self._selected_horse_row() or {}
                lines.append(f"Bet type: {self.state.bet_type}")
                lines.append(f"Horse: {self.state.horse}")
                lines.append(f"Quote ID: {self.state.horse_quote_id}")
                lines.append(f"Odds: {selected.get('odds', '-')}")
                lines.append(f"Unit price: {selected.get('unit_price_v', '-')}")
                lines.append(f"Max units: {selected.get('max_units', '-')}")
                lines.append(f"Debit: {selected.get('debit_v', '-')}")
                lines.append(f"Payout if hit: {selected.get('payout_if_hit_v', '-')}")
                lines.append(f"Board hash: {self.state.horse_quote_board_hash}")
                lines.append(f"Quote expires: {self.state.horse_quote_expires_at}")
                lines.append(f"Budget cap: {self.state.amount}")
            else:
                lines.append(f"Stake: {self.state.amount}")
        elif self.state.action == "Deposit":
            lines.append(f"Amount: {self.state.amount}")
        elif self.state.action == "Verify":
            lines.append(f"Game ID: {self.state.game_id}")
        self.console.print(Panel("\n".join(lines), title="Review"))

    @staticmethod
    def _to_game_result(data: dict, stake_fallback: Decimal) -> GameResult:
        return GameResult(
            game_id=str(data.get("game_id", "")),
            player_id="",
            bet_amount=Decimal(str(data.get("bet_amount", stake_fallback))),
            payout=Decimal(str(data.get("payout", "0"))),
            net=Decimal(str(data.get("net", "0"))),
            outcome_data=data.get("outcome_data", {}) or {},
            server_seed="",
            server_seed_hash=str(data.get("server_seed_hash", "")),
            client_seed="",
            nonce=0,
            provably_fair=bool(data.get("provably_fair", True)),
        )

    def _renderer_for(self):
        if self.state.game == "horse":
            duration = self.render_options.duration_sec if self.render_options.fast_mode else None
            return HorseRacing(render_duration_sec=duration)
        if self.state.game == "skillshot":
            return SkillShotGame()
        return None

    async def _ensure_auth(self) -> bool:
        try:
            await self.client.get_balance()
            return True
        except APIError as e:
            if e.status == 401:
                self.console.print("[red]Session missing/expired. Run: openvegas login[/red]")
                return False
            self.console.print(f"[red]Unable to start UI: API error {e.status}: {e.detail}[/red]")
            return False
        except Exception:
            self.console.print("[red]Session missing/expired. Run: openvegas login[/red]")
            return False

    @staticmethod
    def _looks_like_auth_runtime_failure(exc: Exception) -> bool:
        text = str(exc or "").strip().lower()
        return any(
            token in text
            for token in (
                "expired",
                "refresh token",
                "touch id",
                "_refresh_token_timer",
                "keychain",
                "unauthorized",
                "forbidden",
                "[errno 2] no such file or directory",
            )
        )

    @staticmethod
    def _try_parse_json(raw: str) -> dict:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _selected_horse_row(self) -> dict | None:
        try:
            horse_num = int(self.state.horse)
        except Exception:
            return None
        for row in self.state.horse_quote_rows:
            try:
                if int(row.get("number", -1)) == horse_num:
                    return row
            except Exception:
                continue
        return None

    def _print_horse_quote_board(self, quote: dict) -> None:
        table = Table(title=f"Horse Board ({self.state.bet_type})")
        table.add_column("#", justify="right")
        table.add_column("Horse")
        table.add_column("Odds", justify="right")
        table.add_column("Eff Mult", justify="right")
        table.add_column("Unit Price", justify="right")
        table.add_column("Max Units", justify="right")
        table.add_column("Debit", justify="right")
        table.add_column("Payout If Hit", justify="right")
        table.add_column("Selectable", justify="right")
        for row in quote.get("horses", []):
            selectable = bool(row.get("selectable", False))
            table.add_row(
                str(row.get("number", "")),
                str(row.get("name", "")),
                str(row.get("odds", "")),
                str(row.get("effective_multiplier", "")),
                str(row.get("unit_price_v", "")),
                str(row.get("max_units", "")),
                str(row.get("debit_v", "")),
                str(row.get("payout_if_hit_v", "")),
                "[green]yes[/green]" if selectable else "[red]no[/red]",
            )
        self.console.print(table)

    async def _fetch_horse_quote(self) -> dict | None:
        try:
            budget = Decimal(self.state.amount)
        except Exception:
            self.console.print("[red]Invalid budget amount.[/red]")
            return None

        try:
            quote = await self.client.create_horse_quote(
                bet_type=self.state.bet_type,
                budget_v=budget,
                idempotency_key=f"ui-horse-quote-{uuid.uuid4()}",
            )
        except APIError as e:
            payload = self._try_parse_json(str(e.detail))
            if payload:
                code = payload.get("error", "request_failed")
                detail = payload.get("detail", str(e.detail))
                self.console.print(f"[red]{code}: {detail}[/red]")
            else:
                self.console.print(f"[red]API error {e.status}: {e.detail}[/red]")
            return None

        self.state.horse_quote_id = str(quote.get("quote_id", ""))
        self.state.horse_quote_expires_at = str(quote.get("expires_at", ""))
        self.state.horse_quote_board_hash = str(quote.get("board_hash", ""))
        self.state.horse_quote_rows = list(quote.get("horses", []) or [])
        self.state.horse_quote_selected = {}
        return quote

    def _render_card_outcome(self, game_code: str, outcome: dict, wager: Decimal, payout: Decimal) -> str:
        if game_code == "blackjack":
            return (
                f"{render_hand(outcome.get('player_cards', []), label='PLAYER', value=outcome.get('player'))}\n"
                f"{render_hand(outcome.get('dealer_cards', []), label='DEALER', value=outcome.get('dealer'))}\n"
                f"Result: {outcome.get('result')}"
            )
        if game_code == "poker":
            player_cards = list(outcome.get("player_cards", []) or [])
            dealer_cards = list(outcome.get("dealer_cards", []) or [])
            community_cards = list(outcome.get("community_cards", []) or [])
            result = str(outcome.get("result", ""))
            player_rank = str(outcome.get("player_rank", ""))
            dealer_rank = str(outcome.get("dealer_rank", ""))
            return (
                f"{render_hand(player_cards, label='PLAYER')}\n"
                f"{render_hand(dealer_cards, label='DEALER')}\n"
                f"{render_hand(community_cards, label='BOARD')}\n"
                f"Ranks: PLAYER={player_rank} | DEALER={dealer_rank}\n"
                f"Result: {result}"
            )
        if game_code == "baccarat":
            player_total = outcome.get("player_total")
            banker_total = outcome.get("banker_total")
            bet_side = str(outcome.get("bet_type", "")).replace("bet_", "").upper() or "N/A"
            result = str(outcome.get("result", ""))
            winner = "PLAYER" if "player" in result else "BANKER" if "banker" in result else "TIE"
            verdict = "WIN" if (
                (bet_side == "PLAYER" and winner == "PLAYER")
                or (bet_side == "BANKER" and winner == "BANKER")
                or (bet_side == "TIE" and winner == "TIE")
            ) else "LOSS"
            return (
                f"{render_hand(outcome.get('player_cards', []), label='PLAYER', value=outcome.get('player_total'))}\n"
                f"{render_hand(outcome.get('banker_cards', []), label='BANKER', value=outcome.get('banker_total'))}\n"
                f"Totals: PLAYER={player_total} | BANKER={banker_total}\n"
                f"Your bet: {bet_side} | Winner: {winner} | {verdict}"
            )
        if game_code == "roulette":
            payout_mult = "0"
            if wager > 0:
                payout_mult = str((payout / wager).quantize(Decimal("0.01")))
            return render_roulette_result(
                int(outcome.get("result", 0)),
                str(outcome.get("bet_type", "")),
                bool(outcome.get("hit", False)),
                payout_mult,
            )
        if game_code == "slots":
            return render_slots_reels(outcome.get("reels", []), bool(outcome.get("hit", False)))
        return json.dumps(outcome, indent=2)

    @staticmethod
    def _state_cards_to_strings(raw_cards: Any) -> list[str]:
        out: list[str] = []
        if not isinstance(raw_cards, list):
            return out
        for item in raw_cards:
            if isinstance(item, list) and len(item) >= 2:
                out.append(f"{item[0]}{item[1]}")
            elif isinstance(item, tuple) and len(item) >= 2:
                out.append(f"{item[0]}{item[1]}")
            elif isinstance(item, str) and item:
                out.append(item)
        return out

    def _render_round_state(self, game_code: str, state: dict, current_state: str) -> str:
        if not isinstance(state, dict):
            return ""
        if game_code == "blackjack":
            from openvegas.casino.blackjack import hand_value

            player_cards = self._state_cards_to_strings(state.get("player", []))
            dealer_cards = self._state_cards_to_strings(state.get("dealer", []))
            if current_state != "resolvable" and len(dealer_cards) >= 2:
                dealer_cards = [dealer_cards[0], HIDDEN_CARD_TOKEN]
            player_total = None
            if player_cards:
                try:
                    player_total = hand_value([tuple(c) for c in (state.get("player", []) or [])])
                except Exception:
                    player_total = None
            return (
                f"{render_hand(player_cards, label='PLAYER', value=player_total)}\n"
                f"{render_hand(dealer_cards, label='DEALER')}"
            )
        if game_code == "poker":
            player_cards = self._state_cards_to_strings(state.get("player", []))
            community_cards = self._state_cards_to_strings(state.get("community", []))
            return (
                f"{render_hand(player_cards, label='PLAYER')}\n"
                f"{render_hand(community_cards, label='BOARD')}"
            )
        if game_code == "baccarat":
            player_cards = self._state_cards_to_strings(state.get("player", []))
            banker_cards = self._state_cards_to_strings(state.get("banker", []))
            player_total = state.get("player_total")
            banker_total = state.get("banker_total")
            return (
                f"{render_hand(player_cards, label='PLAYER', value=player_total)}\n"
                f"{render_hand(banker_cards, label='BANKER', value=banker_total)}\n"
                f"Totals: PLAYER={player_total if player_total is not None else '-'} | BANKER={banker_total if banker_total is not None else '-'}"
            )
        if game_code == "roulette":
            bet_type = str(state.get("bet_type") or "none")
            _ = current_state
            return f"Bet selected: {bet_type.replace('bet_', '').upper() if bet_type != 'none' else 'NONE'}"
        if game_code == "slots":
            return "Ready to spin."
        return ""

    async def _animate_resolve(self, game_code: str) -> None:
        if not bool(load_config().get("animation", True)):
            return
        delay = 0.8 if game_code in {"roulette", "slots"} else 0.45
        with self.console.status(f"Resolving {game_code}...", spinner="dots"):
            await asyncio.sleep(delay)

    async def _animate_game_phase(self, game_code: str) -> None:
        if not bool(load_config().get("animation", True)):
            return
        if game_code == "roulette":
            message = "Submitting spin..."
        elif game_code == "slots":
            message = "Spinning reels..."
        else:
            message = f"Running {game_code}..."
        with self.console.status(message, spinner="line"):
            await asyncio.sleep(0.45)

    def _choose_round_action(self, game_code: str, valid_actions: list[str], state: dict) -> tuple[str, dict] | None:
        self.console.print("[bold yellow]Round started: actions cannot be undone.[/bold yellow]")
        if not valid_actions:
            return None
        if game_code == "poker" and {"call", "fold"}.issubset(set(valid_actions)):
            self.console.print("[dim]Actions: call (continue) or fold (forfeit hand).[/dim]")
        self.console.print("[bold blue]Choose next action[/bold blue]")
        action_labels = {
            "bet_red": "Bet Red",
            "bet_black": "Bet Black",
            "bet_odd": "Bet Odd",
            "bet_even": "Bet Even",
            "bet_number": "Bet Number",
            "spin": "Spin",
            "call": "Call",
            "fold": "Fold",
            "hit": "Hit",
            "stand": "Stand",
            "bet_player": "Bet Player",
            "bet_banker": "Bet Banker",
            "bet_tie": "Bet Tie",
        }
        for i, act in enumerate(valid_actions, start=1):
            self.console.print(f"  [{i}] {action_labels.get(act, act)}")
        self.console.print("  [q] quit view")
        picked = Prompt.ask(
            "Action",
            choices=[str(i) for i in range(1, len(valid_actions) + 1)] + ["q"],
            default="1",
        )
        if picked == "q":
            return None
        action = valid_actions[int(picked) - 1]
        payload: dict = {}
        if game_code == "roulette" and action == "bet_number":
            num_raw = Prompt.ask("Pick number 0-36", default="7")
            try:
                payload["number"] = max(0, min(36, int(num_raw)))
            except Exception:
                payload["number"] = 7
        _ = state
        return action, payload

    async def _run_human_card_round(self, *, stake: Decimal, force_win_mode: bool = False) -> str:
        session = await self.client.human_casino_start_session(
            max_loss_v=max(Decimal("100"), stake * Decimal("5")),
            max_rounds=100,
            idempotency_key=f"ui-session-{uuid.uuid4()}",
        )
        session_id = str(session.get("casino_session_id", ""))
        if not session_id:
            return f"Unable to start casino session: {session}"

        if force_win_mode:
            preferred_action: str | None = None
            preferred_payload: dict = {}
            if self.state.game == "roulette":
                roulette_actions = ["bet_red", "bet_black", "bet_odd", "bet_even", "bet_number"]
                selected = self._choose_round_action(self.state.game, roulette_actions, {})
                if selected is None:
                    return "Round view exited before resolve."
                preferred_action, preferred_payload = selected
            resolved = await self.client.human_casino_demo_autoplay(
                casino_session_id=session_id,
                game_code=self.state.game,
                wager_v=stake,
                idempotency_key=f"ui-demo-autoplay-{uuid.uuid4()}",
                preferred_action=preferred_action,
                preferred_payload=preferred_payload,
            )
            if resolved.get("error"):
                return (
                    f"{resolved.get('error')}\n"
                    f"state={resolved.get('current_state')}\n"
                    f"valid_actions={resolved.get('valid_actions', [])}"
                )
            outcome = resolved.get("outcome", {}) or {}
            payout = Decimal(str(resolved.get("payout_v", "0")))
            net = Decimal(str(resolved.get("net_v", "0")))
            round_id = str(resolved.get("round_id", ""))
            if self.state.game == "roulette" and bool(load_config().get("animation", True)):
                try:
                    await animate_roulette_spin(self.console, result_number=int(outcome.get("result", 0)))
                except Exception:
                    await self._animate_resolve(self.state.game)
            else:
                await self._animate_resolve(self.state.game)
            self._last_is_win = net > 0
            visual = self._render_card_outcome(self.state.game, outcome, stake, payout)
            return (
                f"{visual}\n"
                f"LIVE MODE\n"
                f"Payout: {payout} | Net: {net}\n"
                f"Round ID: {round_id}\n"
                f"Verify (API): /casino/human/rounds/{round_id}/verify"
            )

        started = await self.client.human_casino_start_round(
            casino_session_id=session_id,
            game_code=self.state.game,
            wager_v=stake,
            idempotency_key=f"ui-round-{uuid.uuid4()}",
        )
        if started.get("error"):
            return f"{started.get('error')}\nstate={started.get('current_state')}\nvalid_actions={started.get('valid_actions', [])}"

        round_id = str(started.get("round_id", ""))
        current_state = str(started.get("current_state", "awaiting_action"))
        state = started.get("state", {}) or {}
        valid_actions = list(started.get("valid_actions", []) or [])
        last_state_text: str | None = None

        def _print_round_state_once(state_text: str) -> None:
            nonlocal last_state_text
            if not state_text:
                return
            if state_text == last_state_text:
                return
            self.console.print(Panel(state_text, title="Round State"))
            last_state_text = state_text

        state_text = self._render_round_state(self.state.game, state, current_state)
        _print_round_state_once(state_text)

        while True:
            if current_state == "resolvable":
                resolved = await self.client.human_casino_resolve(
                    round_id=round_id,
                    idempotency_key=f"ui-resolve-{uuid.uuid4()}",
                )
                if resolved.get("error"):
                    return (
                        f"{resolved.get('error')}\n"
                        f"state={resolved.get('current_state')}\n"
                        f"valid_actions={resolved.get('valid_actions', [])}"
                    )
                outcome = resolved.get("outcome", {})
                payout = Decimal(str(resolved.get("payout_v", "0")))
                net = Decimal(str(resolved.get("net_v", "0")))
                if self.state.game == "roulette" and bool(load_config().get("animation", True)):
                    try:
                        await animate_roulette_spin(self.console, result_number=int(outcome.get("result", 0)))
                    except Exception:
                        await self._animate_resolve(self.state.game)
                else:
                    await self._animate_resolve(self.state.game)
                self._last_is_win = net > 0
                visual = self._render_card_outcome(self.state.game, outcome, stake, payout)
                return (
                    f"{visual}\n"
                    f"LIVE MODE\n"
                    f"Payout: {payout} | Net: {net}\n"
                    f"Round ID: {round_id}\n"
                    f"Verify (API): /casino/human/rounds/{round_id}/verify"
                )

            if self.state.game in {"roulette", "slots"} and valid_actions == ["spin"]:
                await self._animate_game_phase(self.state.game)
                try:
                    acted = await self.client.human_casino_action(
                        round_id=round_id,
                        action="spin",
                        payload={},
                        idempotency_key=f"ui-action-{uuid.uuid4()}",
                    )
                except APIError as e:
                    parsed = self._try_parse_json(str(e.detail))
                    if parsed:
                        return (
                            f"{parsed.get('error')}\n"
                            f"state={parsed.get('current_state')}\n"
                            f"valid_actions={parsed.get('valid_actions', [])}"
                        )
                    return f"API error {e.status}: {e.detail}"
                if acted.get("error"):
                    current_state = str(acted.get("current_state", current_state))
                    valid_actions = list(acted.get("valid_actions", []) or [])
                    continue
                state = acted.get("state", {}) or {}
                current_state = str(acted.get("current_state", current_state))
                valid_actions = list(acted.get("valid_actions", []) or [])
                state_text = self._render_round_state(self.state.game, state, current_state)
                _print_round_state_once(state_text)
                continue

            selected = self._choose_round_action(self.state.game, valid_actions, state)
            if selected is None:
                return (
                    "Round view exited before resolve.\n"
                    f"Round ID: {round_id}\n"
                    f"Current state: {current_state}\n"
                    f"Valid actions: {valid_actions}"
                )
            action, payload = selected
            try:
                acted = await self.client.human_casino_action(
                    round_id=round_id,
                    action=action,
                    payload=payload,
                    idempotency_key=f"ui-action-{uuid.uuid4()}",
                )
            except APIError as e:
                parsed = self._try_parse_json(str(e.detail))
                if parsed:
                    return (
                        f"{parsed.get('error')}\n"
                        f"state={parsed.get('current_state')}\n"
                        f"valid_actions={parsed.get('valid_actions', [])}"
                    )
                return f"API error {e.status}: {e.detail}"

            if acted.get("error"):
                current_state = str(acted.get("current_state", current_state))
                valid_actions = list(acted.get("valid_actions", []) or [])
                continue
            state = acted.get("state", {}) or {}
            current_state = str(acted.get("current_state", current_state))
            valid_actions = list(acted.get("valid_actions", []) or [])
            state_text = self._render_round_state(self.state.game, state, current_state)
            _print_round_state_once(state_text)

    async def _run_action(self) -> str:
        action = self.state.action
        self._last_is_win = False

        if action == "Balance":
            data = await self.client.get_balance()
            return f"Balance: {_format_v_amount(data.get('balance', '0'))} $V"

        if action == "History":
            data = await self.client.get_billing_activity()
            entries = data.get("entries", [])
            if not entries:
                return "No transactions yet."
            lines: list[str] = []
            for e in entries[:8]:
                kind = str(e.get("type") or e.get("entry_type") or "")
                status = str(e.get("status") or "")
                if kind == "gameplay":
                    amount = str(e.get("amount_v_2dp") or e.get("amount_v") or "0.00")
                    amount_label = f"{amount} $V"
                else:
                    usd = str(e.get("amount_usd") or "0.00")
                    amount_v = str(e.get("amount_v_2dp") or e.get("amount_v") or "0.00")
                    amount_label = f"${usd} · +{amount_v} $V"
                lines.append(
                    f"{kind} {amount_label} {status} ref={str(e.get('reference_id', ''))[:12]}".strip()
                )
            return "\n".join(lines)

        if action == "Deposit":
            amount = Decimal(self.state.amount)
            saved_info: dict[str, Any] = {}
            try:
                with self.console.status("Checking saved payment method...", spinner="dots"):
                    saved_info = await self.client.get_saved_topup_payment_method()
            except Exception:
                saved_info = {}

            if bool(saved_info.get("available")):
                brand = str(saved_info.get("brand") or "card").upper()
                last4 = str(saved_info.get("last4") or "****")
                confirm_saved = Confirm.ask(
                    f"Charge saved {brand} ••••{last4} for ${amount.quantize(Decimal('0.01'))}?",
                    default=True,
                )
                if confirm_saved:
                    try:
                        with self.console.status("Charging saved card via Stripe...", spinner="dots"):
                            charged = await self.client.charge_saved_topup(amount)
                        topup_id = str(charged.get("topup_id") or "").strip()
                        return (
                            f"Top-up ID: {topup_id}\n"
                            f"Status: {charged.get('status')}\n"
                            f"Charged amount: ${amount.quantize(Decimal('0.01'))}\n"
                            "Saved card charged successfully."
                        )
                    except APIError as e:
                        return f"Saved-card charge failed: API error {e.status}: {e.detail}"
                    except Exception as e:
                        return f"Saved-card charge failed: {e}"

            data = await self.client.create_topup_checkout(amount)
            checkout_url = str(data.get("checkout_url") or "")
            topup_id = str(data.get("topup_id") or "").strip()
            status_page = f"/ui/topup/{topup_id}" if topup_id else ""
            status_page_url = (
                f"{str(self.client.base_url).rstrip('/')}{status_page}"
                if status_page
                else ""
            )
            compact_topup = encode_compact_uuid(topup_id) if topup_id else None
            qr_status_short_url = (
                f"{str(self.client.base_url).rstrip('/')}/r/{compact_topup}"
                if compact_topup
                else ""
            )
            target_url = checkout_url
            auto_open_message = "Checkout URL not available."
            if checkout_url:
                if _is_simulated_checkout_url(checkout_url):
                    target_url = f"{str(self.client.base_url).rstrip('/')}/ui/payments"
                try:
                    opened = webbrowser.open(target_url, new=2)
                except Exception:
                    opened = False
                auto_open_message = (
                    f"Opened browser: {target_url}"
                    if opened
                    else f"Open manually: {target_url}"
                )
                if _is_simulated_checkout_url(checkout_url):
                    auto_open_message += " (simulated checkout URL detected)"
            qr_block = ""
            # Use short redirect URL to reduce QR payload and printed size in terminal.
            qr_value = qr_status_short_url or status_page_url or checkout_url
            if qr_value:
                try:
                    border = 0
                    width = qr_width(qr_value, border=border)
                    if width + 4 > self.console.width:
                        qr_value = status_page_url or checkout_url
                        width = qr_width(qr_value, border=0)
                    qr_block = "\nScan QR:\n" + "\n".join(
                        f"  {line}" for line in qr_half_block(qr_value, border=0).splitlines()
                    )
                except Exception as exc:
                    detail = str(exc).strip().replace("\n", " ")
                    if len(detail) > 180:
                        detail = detail[:180]
                    qr_block = (
                        "\nQR unavailable in this runtime "
                        f"({detail or 'missing dependency `qrcode[pil]`'})."
                    )
            return (
                f"Top-up ID: {topup_id}\n"
                f"Status: {data.get('status')}\n"
                f"Checkout URL: {checkout_url}\n"
                f"{auto_open_message}\n"
                f"Status page: {status_page}{qr_block}"
            )

        if action == "Play":
            stake = Decimal(self.state.amount)
            force_win_mode = _win_always_enabled()
            balance_before = ""
            try:
                bal = await self.client.get_balance()
                balance_before = _format_v_amount(bal.get("balance", "0"))
            except Exception:
                balance_before = ""
            if self.state.game in CARD_GAME_CHOICES:
                out = await self._run_human_card_round(
                    stake=stake,
                    force_win_mode=force_win_mode,
                )
                if balance_before:
                    return f"Balance before play: {balance_before} $V\n{out}"
                return out
            if self.state.game == "horse":
                selected = self.state.horse_quote_selected or self._selected_horse_row()
                if not self.state.horse_quote_id:
                    return "Horse quote missing. Go back and fetch quote again."
                if not selected:
                    return "Selected horse missing from quote board. Go back and choose again."
                if not bool(selected.get("selectable", False)):
                    return "Selected horse is not selectable for this budget."

                try:
                    data = await self.client.play_horse_quote(
                        quote_id=self.state.horse_quote_id,
                        horse=int(self.state.horse),
                        idempotency_key=f"ui-horse-play-{uuid.uuid4()}",
                        demo_mode=force_win_mode,
                    )
                except APIError as e:
                    payload = self._try_parse_json(str(e.detail))
                    if payload.get("error") == "quote_expired":
                        quote = await self._fetch_horse_quote()
                        if quote:
                            self._print_horse_quote_board(quote)
                            self.state.horse = ""
                            self.state.horse_quote_selected = {}
                            return (
                                "Quote expired. Refreshed horse board.\n"
                                "Select horse again and re-confirm."
                            )
                    if payload:
                        return f"{payload.get('error')}: {payload.get('detail', '')}"
                    raise
            else:
                payload: dict = {"amount": float(stake)}
                if self.state.game == "skillshot" and not self.render_options.no_render:
                    self.console.print(
                        "[dim]Skillshot: press Enter to lock your stop point. "
                        "Gold pays highest, green pays standard.[/dim]"
                    )
                    try:
                        stop_position = await SkillShotGame().render_interactive(self.console)
                        payload["stop_position"] = max(0, min(SkillShotGame.BAR_WIDTH - 1, int(stop_position)))
                    except Exception:
                        # Fallback to deterministic center stop if interactive render is unavailable.
                        payload["stop_position"] = SkillShotGame.BAR_WIDTH // 2
                if force_win_mode:
                    data = await self.client.play_game_demo(self.state.game, payload)
                else:
                    data = await self.client.play_game(self.state.game, payload)

            gr = self._to_game_result(data, stake)
            renderer = self._renderer_for()
            render_note = ""
            if renderer is not None and not self.render_options.no_render:
                try:
                    await execute_render(renderer, gr, self.console, self.render_options)
                except asyncio.TimeoutError:
                    render_note = "Render timed out\nShowing result summary only\n"
                except Exception:
                    render_note = "Render skipped\nShowing result summary only\n"

            if Decimal(str(data.get("net", "0"))) > 0:
                self._last_is_win = True

            prefix = f"Balance before play: {balance_before} $V\n" if balance_before else ""
            return prefix + (
                f"{render_note}LIVE MODE\n"
                f"Payout: {data.get('payout')} | Net: {data.get('net')}\n"
                f"Game ID: {data.get('game_id')}\n"
                f"Verify: {verify_hint_for_result(str(data.get('game_id', '')), False)}"
            )

        if action == "Verify":
            data = await self.client.verify_game(self.state.game_id)
            return (
                "Outcome verified payload received.\n"
                f"server_seed_hash: {str(data.get('server_seed_hash', ''))[:20]}..."
            )

        return f"Unknown action: {action}"

    async def run_once(self) -> str:
        self._last_is_win = False
        err = validate_inputs(self.state)
        if err:
            return err
        try:
            auth_preflight = getattr(self.client, "auth_preflight", None)
            if callable(auth_preflight):
                await auth_preflight()
            return await self._run_action()
        except APIError as e:
            if e.status == 401:
                return "Session missing/expired. Run: openvegas login"
            return f"API error {e.status}: {e.detail}"
        except (InvalidOperation, ValueError):
            return "Invalid input values for selected action."
        except Exception as e:  # pragma: no cover - runtime fallback
            if isinstance(e, FileNotFoundError) or self._looks_like_auth_runtime_failure(e):
                return "Session missing/expired. Run: openvegas login"
            return "Something went wrong. Please try again."

    def run(self) -> None:
        self.console.print("[bold cyan]OpenVegas Inline UI[/bold cyan] [dim](Ctrl+C to exit)[/dim]")
        if not asyncio.run(self._ensure_auth()):
            return
        while True:
            try:
                step_index = 0
                while True:
                    steps = self._steps_for_state()
                    step_index = max(0, min(step_index, len(steps) - 1))
                    step = steps[step_index]
                    outcome = self._run_step(step, allow_back=step_index > 0)
                    if outcome == "quit":
                        return
                    if outcome == "back":
                        step_index = max(0, step_index - 1)
                        continue
                    if outcome == "stay":
                        continue
                    if outcome == "confirm":
                        break
                    step_index += 1

                message = asyncio.run(self.run_once())
                render_result_panel(
                    self.console,
                    message,
                    is_win=self._last_is_win,
                    animation_enabled=load_config().get("animation", True),
                    title="Result",
                )
                if not Confirm.ask("Run another action?", default=True):
                    return
            except (KeyboardInterrupt, EOFError):
                self.console.print("\n[dim]Exiting UI.[/dim]")
                return


def run_prompt_ui(*, no_render: bool = False, render_timeout_sec: float = 15.0) -> None:
    ui = InlinePromptUI(
        render_options=RenderOptions(
            fast_mode=True,
            duration_sec=10.0,
            no_render=no_render,
            timeout_sec=max(1.0, float(render_timeout_sec)),
        )
    )
    ui.run()
