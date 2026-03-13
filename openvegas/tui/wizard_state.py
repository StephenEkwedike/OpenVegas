"""State and validation helpers for the sequential OpenVegas terminal wizard."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum


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


def steps_for_state(state: WizardState) -> list[Step]:
    if state.action in {"Play", "Play (Demo Win)"}:
        steps = [Step.ACTION, Step.GAME]
        if state.game == "horse":
            steps.append(Step.BET_TYPE)
        steps.extend([Step.INPUTS, Step.REVIEW, Step.RESULT])
        return steps

    if state.action in {"Deposit", "Verify", "Verify (Demo)"}:
        return [Step.ACTION, Step.INPUTS, Step.REVIEW, Step.RESULT]

    return [Step.ACTION, Step.REVIEW, Step.RESULT]


def visible_fields_for_state(state: WizardState) -> set[str]:
    if state.action in {"Balance", "History"}:
        return set()
    if state.action == "Deposit":
        return {"amount"}
    if state.action in {"Play", "Play (Demo Win)"}:
        fields = {"amount", "game"}
        if state.game == "horse":
            fields.update({"horse", "bet_type"})
        return fields
    if state.action in {"Verify", "Verify (Demo)"}:
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

        if state.action in {"Play", "Play (Demo Win)"}:
            stake = Decimal(state.amount)
            if stake <= 0:
                return "Stake must be greater than 0."
            if state.game == "horse":
                if not state.horse.strip():
                    return "Horse number is required for horse play."
                horse = int(state.horse)
                if horse <= 0:
                    return "Horse number must be a positive integer."
            return None

        if state.action in {"Verify", "Verify (Demo)"}:
            if not state.game_id.strip():
                return "Game ID is required."
            return None

        return None
    except (InvalidOperation, ValueError):
        if state.action == "Deposit":
            return "Invalid amount. Example: 5 or 2.5"
        if state.action in {"Play", "Play (Demo Win)"}:
            if state.game == "horse":
                return "Stake must be numeric and horse must be an integer."
            return "Invalid stake amount."
        return "Invalid input."
