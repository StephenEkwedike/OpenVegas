"""Casino Hold'em (heads-up vs dealer)."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from itertools import combinations

from openvegas.casino.base import BaseCasinoGame
from openvegas.rng.provably_fair import ProvablyFairRNG

DECK = [
    (r, s)
    for s in ["S", "H", "D", "C"]
    for r in ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
]

RANK_ORDER = {
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "10": 10,
    "J": 11,
    "Q": 12,
    "K": 13,
    "A": 14,
}

CATEGORY_LABELS = {
    8: "straight_flush",
    7: "four_of_a_kind",
    6: "full_house",
    5: "flush",
    4: "straight",
    3: "three_of_a_kind",
    2: "two_pair",
    1: "pair",
    0: "high_card",
}


def _cards_str(cards: list[tuple[str, str]]) -> list[str]:
    return [f"{rank}{suit}" for rank, suit in cards]


def _straight_high(values_desc: list[int]) -> int | None:
    uniq = sorted(set(values_desc), reverse=True)
    if len(uniq) != 5:
        return None
    if uniq == [14, 5, 4, 3, 2]:
        return 5
    if uniq[0] - uniq[-1] == 4:
        return uniq[0]
    return None


def _score_five(cards: list[tuple[str, str]]) -> tuple[int, ...]:
    values_desc = sorted((RANK_ORDER[c[0]] for c in cards), reverse=True)
    suits = [c[1] for c in cards]
    counts = Counter(values_desc)
    count_pairs = sorted(((count, value) for value, count in counts.items()), reverse=True)
    is_flush = len(set(suits)) == 1
    straight_high = _straight_high(values_desc)

    if is_flush and straight_high is not None:
        return (8, straight_high)

    if count_pairs[0][0] == 4:
        four_val = count_pairs[0][1]
        kicker = max(v for v in values_desc if v != four_val)
        return (7, four_val, kicker)

    if count_pairs[0][0] == 3 and count_pairs[1][0] == 2:
        return (6, count_pairs[0][1], count_pairs[1][1])

    if is_flush:
        return (5, *values_desc)

    if straight_high is not None:
        return (4, straight_high)

    if count_pairs[0][0] == 3:
        trips = count_pairs[0][1]
        kickers = sorted((v for v in values_desc if v != trips), reverse=True)
        return (3, trips, *kickers)

    if count_pairs[0][0] == 2 and count_pairs[1][0] == 2:
        high_pair = max(count_pairs[0][1], count_pairs[1][1])
        low_pair = min(count_pairs[0][1], count_pairs[1][1])
        kicker = max(v for v in values_desc if v not in {high_pair, low_pair})
        return (2, high_pair, low_pair, kicker)

    if count_pairs[0][0] == 2:
        pair = count_pairs[0][1]
        kickers = sorted((v for v in values_desc if v != pair), reverse=True)
        return (1, pair, *kickers)

    return (0, *values_desc)


def _best_of_seven(cards: list[tuple[str, str]]) -> tuple[tuple[int, ...], list[tuple[str, str]]]:
    best_score: tuple[int, ...] | None = None
    best_hand: list[tuple[str, str]] | None = None
    for combo in combinations(cards, 5):
        hand = list(combo)
        score = _score_five(hand)
        if best_score is None or score > best_score:
            best_score = score
            best_hand = hand
    if best_score is None or best_hand is None:
        return (0,), []
    return best_score, best_hand


class PokerGame(BaseCasinoGame):
    game_code = "poker"
    rtp = Decimal("0.9540")

    def initial_state(self, rng: ProvablyFairRNG, client_seed: str, nonce: int) -> dict:
        deck = list(DECK)
        for i in range(len(deck) - 1, 0, -1):
            j = rng.generate_outcome(client_seed, nonce + i, i + 1)
            deck[i], deck[j] = deck[j], deck[i]

        player = [deck.pop(), deck.pop()]
        dealer = [deck.pop(), deck.pop()]
        flop = [deck.pop(), deck.pop(), deck.pop()]
        return {
            "deck": [list(card) for card in deck],
            "player": [list(card) for card in player],
            "dealer": [list(card) for card in dealer],
            "community": [list(card) for card in flop],
            "player_action": None,
            "phase": "decision",
        }

    def apply_action(self, state, action, payload, rng, client_seed, nonce):
        _ = (payload, rng, client_seed, nonce)
        # Opt in only new human rounds; persisted and agent rounds keep their rules.
        if state.get("_human_streets") == 1:
            if action not in self.valid_actions(state):
                return state
            if action == "showdown":
                state["phase"] = "resolved"
                return state
            if action in {"call", "reveal_river"}:
                community = list(state["community"])
                deck = list(state["deck"])
                community.append(deck.pop())
                state.update(community=community, deck=deck, player_action="call",
                             phase="turn" if action == "call" else "river")
                return state
        if state.get("phase") != "decision":
            return state
        if action == "fold":
            state["player_action"] = "fold"
            state["phase"] = "resolved"
            return state
        if action == "call":
            community = list(state.get("community", []))
            deck = list(state.get("deck", []))
            while len(community) < 5 and deck:
                community.append(deck.pop())
            state["community"] = community
            state["deck"] = deck
            state["player_action"] = "call"
            state["phase"] = "resolved"
        return state

    def resolve(self, state):
        player_cards = [tuple(c) for c in state.get("player", [])]
        dealer_cards = [tuple(c) for c in state.get("dealer", [])]
        community_cards = [tuple(c) for c in state.get("community", [])]
        action = str(state.get("player_action") or "")

        if action == "fold":
            return Decimal("0"), {
                "result": "folded",
                "player_cards": _cards_str(player_cards),
                "dealer_cards": _cards_str(dealer_cards),
                "community_cards": _cards_str(community_cards),
            }

        all_player = player_cards + community_cards
        all_dealer = dealer_cards + community_cards
        p_score, p_best = _best_of_seven(all_player)
        d_score, d_best = _best_of_seven(all_dealer)
        p_rank = CATEGORY_LABELS.get(int(p_score[0]), "high_card")
        d_rank = CATEGORY_LABELS.get(int(d_score[0]), "high_card")

        result = "push"
        payout = Decimal("1")
        if p_score > d_score:
            result = "player_wins"
            payout = Decimal("2")
        elif p_score < d_score:
            result = "dealer_wins"
            payout = Decimal("0")

        return payout, {
            "result": result,
            "bet_type": action or "call",
            "player_cards": _cards_str(player_cards),
            "dealer_cards": _cards_str(dealer_cards),
            "community_cards": _cards_str(community_cards),
            "player_rank": p_rank,
            "dealer_rank": d_rank,
            "player_best": _cards_str(p_best),
            "dealer_best": _cards_str(d_best),
        }

    def valid_actions(self, state):
        if state.get("_human_streets") == 1:
            if state.get("phase") == "turn":
                return ["reveal_river"]
            if state.get("phase") == "river":
                return ["showdown"]
        if state.get("phase") == "decision":
            return ["call", "fold"]
        return []

    def is_resolved(self, state):
        return state.get("phase") == "resolved"
