from __future__ import annotations

from openvegas.tui.wizard_state import Step, WizardState, steps_for_state, validate_inputs, visible_fields_for_state


def test_next_back_transitions_are_valid():
    state = WizardState(action="Play", game="horse")
    assert steps_for_state(state) == [
        Step.ACTION,
        Step.GAME,
        Step.BET_TYPE,
        Step.INPUTS,
        Step.REVIEW,
        Step.RESULT,
    ]

    state.game = "skillshot"
    assert steps_for_state(state) == [
        Step.ACTION,
        Step.GAME,
        Step.INPUTS,
        Step.REVIEW,
        Step.RESULT,
    ]

    state.action = "Verify"
    assert steps_for_state(state) == [
        Step.ACTION,
        Step.INPUTS,
        Step.REVIEW,
        Step.RESULT,
    ]


def test_required_fields_block_run():
    state = WizardState(action="Verify", game_id="")
    assert validate_inputs(state) == "Game ID is required."

    state = WizardState(action="Deposit", amount="0")
    assert validate_inputs(state) == "Amount must be greater than 0."

    state = WizardState(action="Play", game="horse", amount="1", horse="")
    assert validate_inputs(state) == "Horse number is required for horse play."


def test_back_next_preserves_entered_values():
    state = WizardState(
        action="Play",
        game="horse",
        bet_type="show",
        amount="2.75",
        horse="4",
        game_id="abc-123",
    )
    _ = steps_for_state(state)

    assert state.amount == "2.75"
    assert state.horse == "4"
    assert state.bet_type == "show"
    assert state.game_id == "abc-123"


def test_action_switch_hides_fields_without_validation_noise_and_restores_values():
    state = WizardState(
        action="Play",
        game="horse",
        bet_type="place",
        amount="3",
        horse="not-an-int",  # invalid for Play, but should be ignored once hidden
    )

    assert validate_inputs(state) == "Stake must be numeric and horse must be an integer."

    state.action = "Verify"
    state.game_id = "game-1"
    assert visible_fields_for_state(state) == {"game_id"}
    assert validate_inputs(state) is None

    state.action = "Play"
    assert state.amount == "3"
    assert state.bet_type == "place"
    assert state.horse == "not-an-int"
