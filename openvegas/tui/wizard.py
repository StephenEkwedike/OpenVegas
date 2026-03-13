"""Interactive guided terminal wizard for common OpenVegas flows."""

from __future__ import annotations

from contextlib import nullcontext
from decimal import Decimal, InvalidOperation

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Button, Footer, Header, Input, RadioButton, RadioSet, Static

from openvegas.client import APIError, OpenVegasClient
from openvegas.config import load_config
from openvegas.tui.wizard_state import (
    Step,
    WizardState,
    steps_for_state,
    validate_inputs,
    visible_fields_for_state,
)


class OpenVegasWizard(App):
    CSS = """
    Screen {
        background: #0b1020;
        color: #dbeafe;
    }
    #root {
        padding: 1 2;
    }
    #title {
        color: #93c5fd;
        text-style: bold;
        margin-bottom: 1;
    }
    #step_title {
        color: #bfdbfe;
        margin-bottom: 1;
    }
    .panel {
        border: round #1e3a8a;
        padding: 0 1;
        margin-bottom: 1;
    }
    RadioButton.-selected {
        color: #60a5fa;
        text-style: bold;
    }
    #nav {
        height: auto;
        margin-bottom: 1;
    }
    #output {
        border: round #1d4ed8;
        padding: 1;
        min-height: 7;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="root"):
            yield Static("OpenVegas Quick Actions", id="title")
            yield Static("Step: Action", id="step_title")

            with Container(id="panel_action", classes="panel"):
                yield Static("What do you want to do?")
                with RadioSet(id="action"):
                    yield RadioButton("Balance", value=True)
                    yield RadioButton("History")
                    yield RadioButton("Deposit")
                    yield RadioButton("Play")
                    yield RadioButton("Play (Demo Win)")
                    yield RadioButton("Verify")
                    yield RadioButton("Verify (Demo)")

            with Container(id="panel_game", classes="panel"):
                yield Static("Select game")
                with RadioSet(id="game"):
                    yield RadioButton("horse", value=True)
                    yield RadioButton("skillshot")

            with Container(id="panel_bet_type", classes="panel"):
                yield Static("Bet type (horse only)")
                with RadioSet(id="bet_type"):
                    yield RadioButton("win", value=True)
                    yield RadioButton("place")
                    yield RadioButton("show")

            with Container(id="panel_inputs", classes="panel"):
                yield Static("Enter required values")
                yield Input(placeholder="Amount / Stake (e.g. 1.5)", id="amount")
                yield Input(placeholder="Horse number (horse play only)", id="horse")
                yield Input(placeholder="Game ID (verify actions)", id="game_id")

            with Container(id="panel_review", classes="panel"):
                yield Static("Review", id="review_text")

            with Container(id="nav"):
                yield Button("Back", id="back")
                yield Button("Next", id="next", variant="primary")
                yield Button("Run", id="run", variant="success")

            yield Static("Ready.", id="output")
        yield Footer()

    def on_mount(self) -> None:
        self.client = OpenVegasClient()
        self.state = WizardState()
        self.current_step = Step.ACTION

        for rid in ("action", "game", "bet_type"):
            radio = self.query_one(f"#{rid}", RadioSet)
            if radio.pressed_button is None:
                for child in radio.children:
                    if isinstance(child, RadioButton):
                        child.value = True
                        break

        self._apply_state_to_form()
        self._refresh_ui()

    @staticmethod
    def _selected_label(radio: RadioSet) -> str | None:
        pressed = radio.pressed_button
        if pressed is None:
            return None
        return pressed.label.plain

    def _set_output(self, message: str) -> None:
        self.query_one("#output", Static).update(message)

    def _set_visible(self, widget_id: str, visible: bool) -> None:
        widget = self.query_one(f"#{widget_id}")
        widget.styles.display = "block" if visible else "none"

    def _capture_form_to_state(self) -> None:
        action = self._selected_label(self.query_one("#action", RadioSet))
        game = self._selected_label(self.query_one("#game", RadioSet))
        bet_type = self._selected_label(self.query_one("#bet_type", RadioSet))
        if action:
            self.state.action = action
        if game:
            self.state.game = game
        if bet_type:
            self.state.bet_type = bet_type

        self.state.amount = self.query_one("#amount", Input).value.strip() or self.state.amount
        self.state.horse = self.query_one("#horse", Input).value.strip() or self.state.horse
        self.state.game_id = self.query_one("#game_id", Input).value.strip() or self.state.game_id

    def _apply_state_to_form(self) -> None:
        self._select_radio("action", self.state.action)
        self._select_radio("game", self.state.game)
        self._select_radio("bet_type", self.state.bet_type)

        self.query_one("#amount", Input).value = self.state.amount
        self.query_one("#horse", Input).value = self.state.horse
        self.query_one("#game_id", Input).value = self.state.game_id

    def _select_radio(self, radio_id: str, label: str) -> None:
        radio = self.query_one(f"#{radio_id}", RadioSet)
        for child in radio.children:
            if isinstance(child, RadioButton):
                child.value = (child.label.plain == label)

    def _steps(self) -> list[Step]:
        return steps_for_state(self.state)

    def _step_index(self) -> int:
        steps = self._steps()
        if self.current_step not in steps:
            self.current_step = steps[0]
        return steps.index(self.current_step)

    def _update_review(self) -> None:
        visible = visible_fields_for_state(self.state)
        lines = [f"Action: {self.state.action}"]
        if "game" in visible:
            lines.append(f"Game: {self.state.game}")
        if "bet_type" in visible:
            lines.append(f"Bet type: {self.state.bet_type}")
        if "amount" in visible:
            lines.append(f"Amount/Stake: {self.state.amount}")
        if "horse" in visible and self.state.game == "horse":
            lines.append(f"Horse: {self.state.horse}")
        if "game_id" in visible:
            lines.append(f"Game ID: {self.state.game_id}")
        lines.append("Press Run to execute.")
        self.query_one("#review_text", Static).update("\n".join(lines))

    def _refresh_ui(self) -> None:
        self._capture_form_to_state()
        steps = self._steps()
        idx = self._step_index()

        title = {
            Step.ACTION: "Step: Action",
            Step.GAME: "Step: Game",
            Step.BET_TYPE: "Step: Bet Type",
            Step.INPUTS: "Step: Inputs",
            Step.REVIEW: "Step: Review",
            Step.RESULT: "Step: Result",
        }
        self.query_one("#step_title", Static).update(title[self.current_step])

        self._set_visible("panel_action", self.current_step == Step.ACTION)
        self._set_visible("panel_game", self.current_step == Step.GAME)
        self._set_visible("panel_bet_type", self.current_step == Step.BET_TYPE)
        self._set_visible("panel_inputs", self.current_step == Step.INPUTS)
        self._set_visible("panel_review", self.current_step == Step.REVIEW)

        self.query_one("#back", Button).disabled = idx == 0
        self.query_one("#next", Button).disabled = idx >= len(steps) - 1 or self.current_step == Step.REVIEW
        self.query_one("#run", Button).disabled = self.current_step != Step.REVIEW

        if self.current_step == Step.INPUTS:
            visible = visible_fields_for_state(self.state)
            self.query_one("#amount", Input).styles.display = "block" if "amount" in visible else "none"
            self.query_one("#horse", Input).styles.display = "block" if "horse" in visible else "none"
            self.query_one("#game_id", Input).styles.display = "block" if "game_id" in visible else "none"

        if self.current_step == Step.REVIEW:
            self._update_review()

    @staticmethod
    def _to_game_result(data: dict, stake_fallback: Decimal):
        from openvegas.games.base import GameResult

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

    @staticmethod
    def _renderer_for(game: str):
        if game == "horse":
            from openvegas.games.horse_racing import HorseRacing

            return HorseRacing
        if game == "skillshot":
            from openvegas.games.skill_shot import SkillShotGame

            return SkillShotGame
        return None

    async def _render_game(self, renderer_cls, gr) -> None:
        from rich.console import Console

        if hasattr(self, "suspend"):
            try:
                with self.suspend():
                    await renderer_cls().render(gr, Console())
                return
            except RuntimeError as e:
                # Happens in non-running app contexts (unit tests); fall through.
                if "generator didn't yield" not in str(e):
                    raise
        with nullcontext():
            await renderer_cls().render(gr, Console())

    async def _render_confetti(self) -> None:
        from rich.console import Console
        from openvegas.tui.confetti import render_confetti

        if hasattr(self, "suspend"):
            try:
                with self.suspend():
                    render_confetti(Console())
                return
            except RuntimeError as e:
                if "generator didn't yield" not in str(e):
                    raise
        with nullcontext():
            render_confetti(Console())

    async def _run_action(self) -> None:
        action = self.state.action

        if action == "Balance":
            data = await self.client.get_balance()
            self._set_output(f"Balance: {data.get('balance', '0')} $V")
            return

        if action == "History":
            data = await self.client.get_history()
            entries = data.get("entries", [])
            if not entries:
                self._set_output("No transactions yet.")
                return
            summary = "\n".join(
                f"{e.get('entry_type')} {e.get('amount')} ref={str(e.get('reference_id',''))[:12]}"
                for e in entries[:8]
            )
            self._set_output(summary)
            return

        if action == "Deposit":
            data = await self.client.create_topup_checkout(Decimal(self.state.amount))
            self._set_output(
                f"Top-up ID: {data.get('topup_id')}\n"
                f"Status: {data.get('status')}\n"
                f"Checkout URL: {data.get('checkout_url')}"
            )
            return

        if action in {"Play", "Play (Demo Win)"}:
            stake = Decimal(self.state.amount)
            payload: dict = {"amount": float(stake)}
            if self.state.game == "horse":
                payload["horse"] = int(self.state.horse)
                payload["type"] = self.state.bet_type

            if action == "Play (Demo Win)":
                data = await self.client.play_game_demo(self.state.game, payload)
            else:
                data = await self.client.play_game(self.state.game, payload)

            renderer_cls = self._renderer_for(self.state.game)
            if renderer_cls is not None:
                gr = self._to_game_result(data, stake)
                await self._render_game(renderer_cls, gr)
            if Decimal(str(data.get("net", "0"))) > 0 and load_config().get("animation", True):
                await self._render_confetti()

            mode = "DEMO MODE (canonical: false)" if data.get("demo_mode") else "LIVE MODE"
            verify_hint = (
                f"openvegas verify {data.get('game_id')} --demo"
                if data.get("demo_mode")
                else f"openvegas verify {data.get('game_id')}"
            )
            self._set_output(
                f"{mode}\n"
                f"Payout: {data.get('payout')} | Net: {data.get('net')}\n"
                f"Game ID: {data.get('game_id')}\n"
                f"Verify: {verify_hint}"
            )
            return

        if action in {"Verify", "Verify (Demo)"}:
            if action == "Verify (Demo)":
                data = await self.client.verify_demo_game(self.state.game_id)
                self._set_output(
                    f"DEMO VERIFY\n"
                    f"canonical: {data.get('canonical')}\n"
                    f"server_seed_hash: {str(data.get('server_seed_hash',''))[:20]}..."
                )
            else:
                data = await self.client.verify_game(self.state.game_id)
                self._set_output(
                    "Outcome verified payload received.\n"
                    f"server_seed_hash: {str(data.get('server_seed_hash',''))[:20]}..."
                )
            return

        self._set_output(f"Unknown action: {action}")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        self._capture_form_to_state()

        steps = self._steps()
        idx = self._step_index()

        if button_id == "back":
            if idx > 0:
                self.current_step = steps[idx - 1]
                self._apply_state_to_form()
                self._refresh_ui()
            return

        if button_id == "next":
            if idx >= len(steps) - 1:
                return

            next_step = steps[idx + 1]
            if next_step == Step.REVIEW:
                err = validate_inputs(self.state)
                if err:
                    self._set_output(err)
                    return

            self.current_step = next_step
            self._apply_state_to_form()
            self._refresh_ui()
            return

        if button_id == "run":
            err = validate_inputs(self.state)
            if err:
                self._set_output(err)
                return
            try:
                await self._run_action()
                self.current_step = Step.RESULT
                self._refresh_ui()
            except APIError as e:
                self._set_output(f"API error {e.status}: {e.detail}")
            except (InvalidOperation, ValueError):
                self._set_output("Invalid input values for selected action.")
            except Exception as e:  # pragma: no cover - runtime fallback
                self._set_output(f"Error: {e}")


def run_wizard() -> None:
    OpenVegasWizard().run()
