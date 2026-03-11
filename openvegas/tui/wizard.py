"""Interactive radio-select terminal wizard for common OpenVegas flows."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Button, Footer, Header, Input, RadioButton, RadioSet, Static

from openvegas.client import APIError, OpenVegasClient


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
    RadioSet {
        border: round #1e3a8a;
        padding: 0 1;
        margin-bottom: 1;
    }
    RadioButton.-selected {
        color: #60a5fa;
        text-style: bold;
    }
    #output {
        border: round #1d4ed8;
        padding: 1;
        min-height: 6;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="root"):
            yield Static("OpenVegas Quick Actions", id="title")

            yield Static("What do you want to do?")
            with RadioSet(id="action"):
                yield RadioButton("Balance", value=True)
                yield RadioButton("History")
                yield RadioButton("Deposit")
                yield RadioButton("Play")
                yield RadioButton("Play (Demo Win)")
                yield RadioButton("Verify")
                yield RadioButton("Verify (Demo)")

            yield Static("Game (used for Play actions)")
            with RadioSet(id="game"):
                yield RadioButton("horse", value=True)
                yield RadioButton("skillshot")

            yield Static("Bet type (horse only)")
            with RadioSet(id="bet_type"):
                yield RadioButton("win", value=True)
                yield RadioButton("place")
                yield RadioButton("show")

            yield Input(placeholder="Amount / Stake (e.g. 1.5)", id="amount")
            yield Input(placeholder="Horse number (horse play only)", id="horse")
            yield Input(placeholder="Game ID (verify actions)", id="game_id")
            yield Button("Run", id="run", variant="primary")
            yield Static("Ready.", id="output")
        yield Footer()

    def on_mount(self) -> None:
        self.client = OpenVegasClient()
        for rid in ("action", "game", "bet_type"):
            radio = self.query_one(f"#{rid}", RadioSet)
            if radio.pressed_button is None:
                for child in radio.children:
                    if isinstance(child, RadioButton):
                        child.value = True
                        break

    @staticmethod
    def _selected_label(radio: RadioSet) -> str | None:
        pressed = radio.pressed_button
        if pressed is None:
            return None
        return pressed.label.plain

    def _set_output(self, message: str) -> None:
        self.query_one("#output", Static).update(message)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "run":
            return

        action = self._selected_label(self.query_one("#action", RadioSet))
        game = self._selected_label(self.query_one("#game", RadioSet))
        bet_type = self._selected_label(self.query_one("#bet_type", RadioSet))
        if not action:
            self._set_output("Select an action first.")
            return
        if not game:
            self._set_output("Select a game first.")
            return

        amount_raw = self.query_one("#amount", Input).value.strip()
        horse_raw = self.query_one("#horse", Input).value.strip()
        game_id = self.query_one("#game_id", Input).value.strip()

        try:
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
                try:
                    amount = Decimal(amount_raw)
                except InvalidOperation:
                    self._set_output("Invalid amount. Example: 5 or 2.5")
                    return
                data = await self.client.create_topup_checkout(amount)
                self._set_output(
                    f"Top-up ID: {data.get('topup_id')}\n"
                    f"Status: {data.get('status')}\n"
                    f"Checkout URL: {data.get('checkout_url')}"
                )
                return

            if action in {"Play", "Play (Demo Win)"}:
                try:
                    stake = Decimal(amount_raw)
                except InvalidOperation:
                    self._set_output("Invalid stake amount.")
                    return
                payload: dict = {"amount": float(stake)}
                if game == "horse":
                    if not horse_raw:
                        self._set_output("Horse play requires horse number.")
                        return
                    try:
                        payload["horse"] = int(horse_raw)
                    except ValueError:
                        self._set_output("Horse must be an integer.")
                        return
                    payload["type"] = bet_type or "win"

                if action == "Play (Demo Win)":
                    data = await self.client.play_game_demo(game, payload)
                    self._set_output(
                        f"DEMO MODE (canonical: false)\n"
                        f"Payout: {data.get('payout')} | Net: {data.get('net')}\n"
                        f"Game ID: {data.get('game_id')}"
                    )
                else:
                    data = await self.client.play_game(game, payload)
                    self._set_output(
                        f"Payout: {data.get('payout')} | Net: {data.get('net')}\n"
                        f"Game ID: {data.get('game_id')}"
                    )
                return

            if action in {"Verify", "Verify (Demo)"}:
                if not game_id:
                    self._set_output("Enter game ID for verify.")
                    return
                if action == "Verify (Demo)":
                    data = await self.client.verify_demo_game(game_id)
                    self._set_output(
                        f"DEMO VERIFY\n"
                        f"canonical: {data.get('canonical')}\n"
                        f"server_seed_hash: {str(data.get('server_seed_hash',''))[:20]}..."
                    )
                else:
                    data = await self.client.verify_game(game_id)
                    self._set_output(
                        f"Outcome verified payload received.\n"
                        f"server_seed_hash: {str(data.get('server_seed_hash',''))[:20]}..."
                    )
                return

            self._set_output(f"Unknown action: {action}")
        except APIError as e:
            self._set_output(f"API error {e.status}: {e.detail}")
        except Exception as e:  # pragma: no cover - runtime fallback
            self._set_output(f"Error: {e}")


def run_wizard() -> None:
    OpenVegasWizard().run()
