"""Inline guided UI for OpenVegas (non-fullscreen terminal flow)."""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from openvegas.client import APIError, OpenVegasClient
from openvegas.config import load_config
from openvegas.tui.cards import render_hand
from openvegas.games.base import GameResult
from openvegas.games.horse_racing import HorseRacing
from openvegas.games.skill_shot import SkillShotGame
from openvegas.tui.confetti import render_confetti
from openvegas.tui.hints import verify_hint_for_result
from openvegas.tui.roulette_renderer import render_result as render_roulette_result
from openvegas.tui.slots_renderer import render_reels as render_slots_reels
from openvegas.tui.wizard_state import WizardState, validate_inputs

ACTION_CHOICES = [
    "Balance",
    "History",
    "Deposit",
    "Play",
    "Play (Demo Win)",
    "Verify",
    "Verify (Demo)",
]
GAME_CHOICES = ["horse", "skillshot"]
CARD_GAME_CHOICES = ["blackjack", "roulette", "slots", "poker", "baccarat"]
GAME_CHOICES = ["horse", "skillshot", *CARD_GAME_CHOICES]
HORSE_BET_CHOICES = ["win", "place", "show"]


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

    def _steps_for_state(self) -> list[str]:
        steps = ["action"]
        if self.state.action in {"Play", "Play (Demo Win)"}:
            steps.append("game")
            if self.state.game == "horse":
                steps.append("bet_type")
            steps.append("inputs")
        elif self.state.action in {"Deposit", "Verify", "Verify (Demo)"}:
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
    ) -> tuple[str | None, str | None]:
        self.console.print(f"[bold blue]{label}[/bold blue]")
        for i, choice in enumerate(choices, start=1):
            marker = "●" if choice == current else "○"
            self.console.print(f"  [{i}] {marker} {choice}")
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
            value, signal = self._ask_choice("Action", ACTION_CHOICES, self.state.action, allow_back=allow_back)
            if signal:
                return signal
            self.state.action = str(value)
            return "next"

        if step == "game":
            value, signal = self._ask_choice("Game", GAME_CHOICES, self.state.game, allow_back=allow_back)
            if signal:
                return signal
            self.state.game = str(value)
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
            return "next"

        if step == "inputs":
            if self.state.action == "Deposit":
                value, signal = self._ask_input("Amount (USD)", default=self.state.amount, allow_back=allow_back)
                if signal:
                    return signal
                self.state.amount = str(value)
                return "next"

            if self.state.action in {"Play", "Play (Demo Win)"}:
                value, signal = self._ask_input("Stake ($V)", default=self.state.amount, allow_back=allow_back)
                if signal:
                    return signal
                self.state.amount = str(value)
                if self.state.game == "horse":
                    value, signal = self._ask_input("Horse number", default=self.state.horse, allow_back=allow_back)
                    if signal:
                        return signal
                    self.state.horse = str(value)
                return "next"

            if self.state.action in {"Verify", "Verify (Demo)"}:
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
        if self.state.action in {"Play", "Play (Demo Win)"}:
            lines.append(f"Game: {self.state.game}")
            if self.state.game == "horse":
                lines.append(f"Bet type: {self.state.bet_type}")
                lines.append(f"Horse: {self.state.horse}")
            lines.append(f"Stake: {self.state.amount}")
        elif self.state.action == "Deposit":
            lines.append(f"Amount: {self.state.amount}")
        elif self.state.action in {"Verify", "Verify (Demo)"}:
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

    @staticmethod
    def _try_parse_json(raw: str) -> dict:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _render_card_outcome(self, game_code: str, outcome: dict, wager: Decimal, payout: Decimal) -> str:
        if game_code == "blackjack":
            return (
                f"{render_hand(outcome.get('player_cards', []), label='PLAYER', value=outcome.get('player'))}\n"
                f"{render_hand(outcome.get('dealer_cards', []), label='DEALER', value=outcome.get('dealer'))}\n"
                f"Result: {outcome.get('result')}"
            )
        if game_code == "poker":
            return (
                f"{render_hand(outcome.get('hand', []), label='FINAL HAND')}\n"
                f"Rank: {outcome.get('rank')}"
            )
        if game_code == "baccarat":
            return (
                f"{render_hand(outcome.get('player_cards', []), label='PLAYER', value=outcome.get('player_total'))}\n"
                f"{render_hand(outcome.get('banker_cards', []), label='BANKER', value=outcome.get('banker_total'))}\n"
                f"Result: {outcome.get('result')}"
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

    def _choose_round_action(self, game_code: str, valid_actions: list[str], state: dict) -> tuple[str, dict] | None:
        self.console.print("[bold yellow]Round started: actions cannot be undone.[/bold yellow]")
        if not valid_actions:
            return None
        if game_code == "poker" and "hold" in valid_actions:
            self.console.print("[dim]Actions: hold, stand[/dim]")
        self.console.print("[bold blue]Choose next action[/bold blue]")
        for i, act in enumerate(valid_actions, start=1):
            self.console.print(f"  [{i}] {act}")
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
        if game_code == "poker" and action == "hold":
            pos_raw = Prompt.ask("Hold positions (e.g. 1,3,5 or none)", default="none").strip().lower()
            if pos_raw != "none" and pos_raw:
                try:
                    payload["positions"] = [max(0, int(x.strip()) - 1) for x in pos_raw.split(",") if x.strip()]
                except Exception:
                    payload["positions"] = []
        if game_code == "roulette" and action == "bet_number":
            num_raw = Prompt.ask("Pick number 0-36", default="7")
            try:
                payload["number"] = max(0, min(36, int(num_raw)))
            except Exception:
                payload["number"] = 7
        _ = state
        return action, payload

    async def _run_human_card_round(self, *, demo_mode: bool, stake: Decimal) -> str:
        session = await self.client.human_casino_start_session(
            max_loss_v=max(Decimal("100"), stake * Decimal("5")),
            max_rounds=100,
            idempotency_key=f"ui-session-{uuid.uuid4()}",
        )
        session_id = str(session.get("casino_session_id", ""))
        if not session_id:
            return f"Unable to start casino session: {session}"

        if demo_mode:
            demo = await self.client.human_casino_demo_autoplay(
                casino_session_id=session_id,
                game_code=self.state.game,
                wager_v=stake,
                idempotency_key=f"ui-demo-{uuid.uuid4()}",
            )
            outcome = demo.get("outcome", {})
            payout = Decimal(str(demo.get("payout_v", "0")))
            net = Decimal(str(demo.get("net_v", "0")))
            visual = self._render_card_outcome(self.state.game, outcome, stake, payout)
            return (
                f"{visual}\n"
                f"DEMO MODE (canonical: false)\n"
                f"Payout: {payout} | Net: {net}\n"
                f"Round ID: {demo.get('round_id')}\n"
                f"Verify (API): /casino/human/rounds/{demo.get('round_id')}/verify"
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
                visual = self._render_card_outcome(self.state.game, outcome, stake, payout)
                if net > 0 and load_config().get("animation", True):
                    render_confetti(self.console)
                return (
                    f"{visual}\n"
                    f"LIVE MODE\n"
                    f"Payout: {payout} | Net: {net}\n"
                    f"Round ID: {round_id}\n"
                    f"Verify (API): /casino/human/rounds/{round_id}/verify"
                )

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

    async def _run_action(self) -> str:
        action = self.state.action

        if action == "Balance":
            data = await self.client.get_balance()
            return f"Balance: {data.get('balance', '0')} $V"

        if action == "History":
            data = await self.client.get_history()
            entries = data.get("entries", [])
            if not entries:
                return "No transactions yet."
            return "\n".join(
                f"{e.get('entry_type')} {e.get('amount')} ref={str(e.get('reference_id',''))[:12]}"
                for e in entries[:8]
            )

        if action == "Deposit":
            data = await self.client.create_topup_checkout(Decimal(self.state.amount))
            return (
                f"Top-up ID: {data.get('topup_id')}\n"
                f"Status: {data.get('status')}\n"
                f"Checkout URL: {data.get('checkout_url')}"
            )

        if action in {"Play", "Play (Demo Win)"}:
            stake = Decimal(self.state.amount)
            if self.state.game in CARD_GAME_CHOICES:
                return await self._run_human_card_round(
                    demo_mode=(action == "Play (Demo Win)"),
                    stake=stake,
                )
            payload: dict = {"amount": float(stake)}
            if self.state.game == "horse":
                payload["horse"] = int(self.state.horse)
                payload["type"] = self.state.bet_type

            if action == "Play (Demo Win)":
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

            if Decimal(str(data.get("net", "0"))) > 0 and load_config().get("animation", True):
                render_confetti(self.console)

            mode = "DEMO MODE (canonical: false)" if data.get("demo_mode") else "LIVE MODE"
            return (
                f"{render_note}{mode}\n"
                f"Payout: {data.get('payout')} | Net: {data.get('net')}\n"
                f"Game ID: {data.get('game_id')}\n"
                f"Verify: {verify_hint_for_result(str(data.get('game_id', '')), bool(data.get('demo_mode')))}"
            )

        if action in {"Verify", "Verify (Demo)"}:
            if action == "Verify (Demo)":
                data = await self.client.verify_demo_game(self.state.game_id)
                return (
                    "DEMO VERIFY\n"
                    f"canonical: {data.get('canonical')}\n"
                    f"server_seed_hash: {str(data.get('server_seed_hash', ''))[:20]}..."
                )
            data = await self.client.verify_game(self.state.game_id)
            return (
                "Outcome verified payload received.\n"
                f"server_seed_hash: {str(data.get('server_seed_hash', ''))[:20]}..."
            )

        return f"Unknown action: {action}"

    async def run_once(self) -> str:
        err = validate_inputs(self.state)
        if err:
            return err
        try:
            return await self._run_action()
        except APIError as e:
            if e.status == 401:
                return "Session missing/expired. Run: openvegas login"
            return f"API error {e.status}: {e.detail}"
        except (InvalidOperation, ValueError):
            return "Invalid input values for selected action."
        except Exception as e:  # pragma: no cover - runtime fallback
            return f"Error: {e}"

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
                    if outcome == "confirm":
                        break
                    step_index += 1

                message = asyncio.run(self.run_once())
                self.console.print(Panel(message, title="Result"))
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
