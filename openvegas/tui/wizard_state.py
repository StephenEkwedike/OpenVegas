"""State and validation helpers for the sequential OpenVegas terminal wizard."""

from __future__ import annotations

from openvegas.integrations.game_result import amount as format_v

from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal, InvalidOperation
from enum import Enum

from openvegas.casino.constants import min_game_wager_v


class Step(str, Enum):
    ACTION = "action"
    GAME = "game"
    BET_TYPE = "bet_type"
    INPUTS = "inputs"
    REVIEW = "review"
    RESULT = "result"


@dataclass
class WizardState:
    action: str = "Balance"
    game: str = "horse"
    bet_type: str = "win"
    amount: str = "1"
    horse: str = "1"
    game_id: str = ""
    horse_quote_id: str = ""
    horse_quote_expires_at: str = ""
    horse_quote_board_hash: str = ""
    horse_quote_rows: list[dict] = field(default_factory=list)
    horse_quote_selected: dict = field(default_factory=dict)


def steps_for_state(state: WizardState) -> list[Step]:
    if state.action == "Play":
        steps = [Step.ACTION, Step.GAME]
        if state.game == "horse":
            steps.append(Step.BET_TYPE)
        steps.extend([Step.INPUTS, Step.REVIEW, Step.RESULT])
        return steps

    if state.action in {"Deposit", "Verify"}:
        return [Step.ACTION, Step.INPUTS, Step.REVIEW, Step.RESULT]

    return [Step.ACTION, Step.REVIEW, Step.RESULT]


def visible_fields_for_state(state: WizardState) -> set[str]:
    if state.action in {"Balance", "History"}:
        return set()
    if state.action == "Deposit":
        return {"amount"}
    if state.action == "Play":
        fields = {"amount", "game"}
        if state.game == "horse":
            fields.update({"horse", "bet_type"})
        return fields
    if state.action == "Verify":
        return {"game_id"}
    return set()


def validate_inputs(state: WizardState) -> str | None:
    """Validate only fields relevant to current action/game.

    Hidden fields are intentionally ignored so action switching does not create
    validation noise.
    """
    try:
        if state.action == "Deposit":
            amt = Decimal(state.amount)
            if amt <= 0:
                return "Amount must be greater than 0."
            return None

        if state.action == "Play":
            stake = Decimal(state.amount)
            if stake <= 0:
                return "Stake must be greater than 0."
            if stake < min_game_wager_v():
                return f"Stake must be at least {format_v(min_game_wager_v())}."
            if state.game == "horse":
                if not state.horse.strip():
                    return "Horse number is required for horse play."
                horse = int(state.horse)
                if horse <= 0:
                    return "Horse number must be a positive integer."
                if not state.horse_quote_id.strip():
                    return "Horse quote is required. Fetch quote before horse play."
            return None

        if state.action == "Verify":
            if not state.game_id.strip():
                return "Game ID is required."
            return None

        return None
    except (InvalidOperation, ValueError):
        if state.action == "Deposit":
            return "Invalid amount. Example: 5 or 2.5"
        if state.action == "Play":
            if state.game == "horse":
                return "Stake must be numeric and horse must be an integer."
            return "Invalid stake amount."
        return "Invalid input."
