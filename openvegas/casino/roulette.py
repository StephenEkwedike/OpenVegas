"""European Roulette — single-zero wheel."""

from __future__ import annotations

from decimal import Decimal

from openvegas.casino.base import BaseCasinoGame
from openvegas.rng.provably_fair import ProvablyFairRNG

RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


class RouletteGame(BaseCasinoGame):
    game_code = "roulette"
    rtp = Decimal("0.9730")

    def initial_state(self, rng, client_seed, nonce):
        return {"bet_type": None, "bet_value": None, "result": None, "phase": "betting"}

    def apply_action(self, state, action, payload, rng, client_seed, nonce):
        if action in ("bet_red", "bet_black", "bet_odd", "bet_even", "bet_number"):
            state["bet_type"] = action
            state["bet_value"] = payload.get("number")
            return state

        if action == "spin":
            state["result"] = rng.generate_outcome(client_seed, nonce, 37)
            state["phase"] = "resolved"
        return state

    def resolve(self, state):
        r = state["result"]
        bt = state["bet_type"]
        data = {"result": r, "bet_type": bt}

        if bt == "bet_number" and r == state.get("bet_value"):
            return Decimal("36"), {**data, "hit": True}
        if bt == "bet_red" and r in RED_NUMBERS:
            return Decimal("2"), {**data, "hit": True}
        if bt == "bet_black" and r not in RED_NUMBERS and r != 0:
            return Decimal("2"), {**data, "hit": True}
        if bt == "bet_odd" and r != 0 and r % 2 == 1:
            return Decimal("2"), {**data, "hit": True}
        if bt == "bet_even" and r != 0 and r % 2 == 0:
            return Decimal("2"), {**data, "hit": True}
        return Decimal("0"), {**data, "hit": False}

    def valid_actions(self, state):
        if state["phase"] == "resolved":
            return []
        if state["bet_type"] is None:
            return ["bet_red", "bet_black", "bet_odd", "bet_even", "bet_number"]
        return ["spin"]

    def is_resolved(self, state):
        return state["phase"] == "resolved"
