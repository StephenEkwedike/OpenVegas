"""3-Reel ASCII Slots."""

from __future__ import annotations

from decimal import Decimal

from openvegas.casino.base import BaseCasinoGame
from openvegas.rng.provably_fair import ProvablyFairRNG

SYMBOLS = ["7", "BAR", "CHERRY", "LEMON", "BELL", "STAR"]

PAYOUT_TABLE = {
    ("7", "7", "7"): Decimal("50"),
    ("BAR", "BAR", "BAR"): Decimal("20"),
    ("BELL", "BELL", "BELL"): Decimal("10"),
    ("STAR", "STAR", "STAR"): Decimal("8"),
    ("CHERRY", "CHERRY", "CHERRY"): Decimal("5"),
}


class SlotsGame(BaseCasinoGame):
    game_code = "slots"
    rtp = Decimal("0.9500")

    def initial_state(self, rng, client_seed, nonce):
        return {"reels": None, "phase": "ready"}

    def apply_action(self, state, action, payload, rng, client_seed, nonce):
        if action == "spin":
            reels = [
                SYMBOLS[rng.generate_outcome(client_seed, nonce + i, len(SYMBOLS))]
                for i in range(3)
            ]
            state["reels"] = reels
            state["phase"] = "resolved"
        return state

    def resolve(self, state):
        reels = tuple(state["reels"])
        data = {"reels": list(reels)}

        if reels in PAYOUT_TABLE:
            return PAYOUT_TABLE[reels], {**data, "hit": True}
        if reels[0] == "CHERRY" and reels[1] == "CHERRY":
            return Decimal("2"), {**data, "hit": True, "partial": "two_cherries"}
        return Decimal("0"), {**data, "hit": False}

    def valid_actions(self, state):
        return ["spin"] if state["phase"] == "ready" else []

    def is_resolved(self, state):
        return state["phase"] == "resolved"
