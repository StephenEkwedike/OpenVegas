"""Baccarat — player/banker/tie with standard third-card rules."""

from __future__ import annotations

from decimal import Decimal

from openvegas.casino.base import BaseCasinoGame
from openvegas.rng.provably_fair import ProvablyFairRNG

DECK = [
    (r, s)
    for s in ["S", "H", "D", "C"]
    for r in ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
]

CARD_VALUES = {
    "A": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
    "8": 8, "9": 9, "10": 0, "J": 0, "Q": 0, "K": 0,
}


def hand_total(cards: list) -> int:
    return sum(CARD_VALUES[c[0]] for c in cards) % 10


def cards_str(cards: list) -> list[str]:
    return [f"{r}{s}" for r, s in cards]


class BaccaratGame(BaseCasinoGame):
    game_code = "baccarat"
    rtp = Decimal("0.9862")

    def initial_state(self, rng, client_seed, nonce):
        shoe = list(DECK) * 6
        for i in range(len(shoe) - 1, 0, -1):
            j = rng.generate_outcome(client_seed, nonce + (i % 10000), i + 1)
            shoe[i], shoe[j] = shoe[j], shoe[i]
        return {
            "shoe": [list(c) for c in shoe],
            "bet_type": None,
            "player": [],
            "banker": [],
            "phase": "betting",
        }

    def apply_action(self, state, action, payload, rng, client_seed, nonce):
        if action in ("bet_player", "bet_banker", "bet_tie"):
            state["bet_type"] = action
            shoe = state["shoe"]

            state["player"] = [shoe.pop(), shoe.pop()]
            state["banker"] = [shoe.pop(), shoe.pop()]

            pt = hand_total(state["player"])
            bt = hand_total(state["banker"])
            state["player_total"] = pt
            state["banker_total"] = bt

            # Natural — no third card
            if pt >= 8 or bt >= 8:
                state["phase"] = "resolved"
                return state

            # Player third card rule
            if pt <= 5:
                state["player"].append(shoe.pop())
                p3_val = CARD_VALUES[state["player"][2][0]]

                # Banker third card rule (depends on player's third card)
                if bt <= 2:
                    state["banker"].append(shoe.pop())
                elif bt == 3 and p3_val != 8:
                    state["banker"].append(shoe.pop())
                elif bt == 4 and p3_val in (2, 3, 4, 5, 6, 7):
                    state["banker"].append(shoe.pop())
                elif bt == 5 and p3_val in (4, 5, 6, 7):
                    state["banker"].append(shoe.pop())
                elif bt == 6 and p3_val in (6, 7):
                    state["banker"].append(shoe.pop())
            else:
                # Player stood — banker draws on 0-5
                if bt <= 5:
                    state["banker"].append(shoe.pop())

            state["player_total"] = hand_total(state["player"])
            state["banker_total"] = hand_total(state["banker"])
            state["phase"] = "resolved"
        return state

    def resolve(self, state):
        pt = hand_total(state["player"])
        bt = hand_total(state["banker"])
        bet = state["bet_type"]
        pc = cards_str(state["player"])
        bc = cards_str(state["banker"])
        data = {
            "bet_type": bet,
            "player_total": pt,
            "banker_total": bt,
            "player_cards": pc,
            "banker_cards": bc,
        }

        if pt == bt:
            if bet == "bet_tie":
                return Decimal("9"), {**data, "result": "tie_win"}
            return Decimal("1"), {**data, "result": "tie_push"}
        if pt > bt:
            if bet == "bet_player":
                return Decimal("2"), {**data, "result": "player_wins"}
            return Decimal("0"), {**data, "result": "player_wins"}
        # banker wins
        if bet == "bet_banker":
            return Decimal("1.95"), {**data, "result": "banker_wins"}
        return Decimal("0"), {**data, "result": "banker_wins"}

    def valid_actions(self, state):
        if state["phase"] == "betting":
            return ["bet_player", "bet_banker", "bet_tie"]
        return []

    def is_resolved(self, state):
        return state["phase"] == "resolved"
