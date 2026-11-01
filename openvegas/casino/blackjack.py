"""Blackjack — single-deck, dealer stands on 17."""

from __future__ import annotations

from decimal import Decimal

from openvegas.casino.base import BaseCasinoGame
from openvegas.rng.provably_fair import ProvablyFairRNG

DECK = [
    (r, s)
    for s in ["S", "H", "D", "C"]
    for r in ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
]


def hand_value(cards: list) -> int:
    total, aces = 0, 0
    for rank, _ in cards:
        if rank in ("J", "Q", "K"):
            total += 10
        elif rank == "A":
            aces += 1
            total += 11
        else:
            total += int(rank)
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def cards_str(cards: list) -> list[str]:
    return [f"{r}{s}" for r, s in cards]


class BlackjackGame(BaseCasinoGame):
    game_code = "blackjack"
    rtp = Decimal("0.9950")

    def initial_state(self, rng: ProvablyFairRNG, client_seed: str, nonce: int) -> dict:
        deck = list(DECK)
        for i in range(len(deck) - 1, 0, -1):
            j = rng.generate_outcome(client_seed, nonce + i, i + 1)
            deck[i], deck[j] = deck[j], deck[i]

        player = [deck.pop(), deck.pop()]
        dealer = [deck.pop(), deck.pop()]

        state = {
            "deck": [list(c) for c in deck],
            "player": [list(c) for c in player],
            "dealer": [list(c) for c in dealer],
            "phase": "player_turn",
        }

        if hand_value(player) == 21:
            state["phase"] = "resolved"

        return state

    def apply_action(self, state, action, payload, rng, client_seed, nonce):
        if state.get('_blackjack_actions_v2'):
            return self._apply_extended(state, action)
        if state["phase"] != "player_turn":
            return state

        if action == "hit":
            state["player"].append(state["deck"].pop())
            if hand_value(state["player"]) > 21:
                state["phase"] = "resolved"
        elif action == "stand":
            while hand_value(state["dealer"]) < 17:
                state["dealer"].append(state["deck"].pop())
            state["phase"] = "resolved"

        return state

    def resolve(self, state):
        if state.get('_blackjack_actions_v2'):
            return self._resolve_extended(state)
        pv = hand_value(state["player"])
        dv = hand_value(state["dealer"])
        pc = cards_str(state["player"])
        dc = cards_str(state["dealer"])
        data = {"player": pv, "dealer": dv, "player_cards": pc, "dealer_cards": dc}

        if pv == 21 and len(state["player"]) == 2:
            if dv == 21 and len(state["dealer"]) == 2:
                return Decimal("1"), {**data, "result": "push"}
            return Decimal("2.5"), {**data, "result": "blackjack"}

        if pv > 21:
            return Decimal("0"), {**data, "result": "bust"}
        if dv > 21:
            return Decimal("2"), {**data, "result": "dealer_bust"}
        if pv > dv:
            return Decimal("2"), {**data, "result": "win"}
        if pv == dv:
            return Decimal("1"), {**data, "result": "push"}
        return Decimal("0"), {**data, "result": "loss"}

    def valid_actions(self, state):
        if state.get('_blackjack_actions_v2'):
            if state['phase'] != 'player_turn':
                return []
            hand = state['hands'][state['active_hand']]
            actions = ['hit', 'stand']
            if len(hand['cards']) == 2 and not hand.get('split_aces'):
                actions.append('double')
                if len(state['hands']) < 4 and hand['cards'][0][0] == hand['cards'][1][0]:
                    actions.append('split')
            return actions
        if state["phase"] == "player_turn":
            return ["hit", "stand"]
        return []

    def is_resolved(self, state):
        return state["phase"] == "resolved"

    def enable_extended(self, state):
        """Version only new funded human rounds; legacy/replay rules stay unchanged."""
        state['_blackjack_actions_v2'] = True
        state['hands'] = [dict(cards=state['player'], units=1, done=False, split=False)]
        state['active_hand'] = 0
        # Peek before accepting any additional wager.
        if hand_value(state['dealer']) == 21 or hand_value(state['player']) == 21:
            state['phase'] = 'resolved'
            state['hands'][0]['done'] = True
        return state

    def extra_stake_units(self, state, action):
        if state.get('_blackjack_actions_v2') and action in {'split', 'double'} and action in self.valid_actions(state):
            return state['hands'][state['active_hand']]['units']
        return 0

    def _advance(self, state):
        for index, hand in enumerate(state['hands']):
            if not hand['done']:
                state['active_hand'] = index
                state['player'] = hand['cards']
                return state
        if any(hand_value(h['cards']) <= 21 for h in state['hands']):
            while hand_value(state['dealer']) < 17:
                state['dealer'].append(state['deck'].pop())
        state['phase'] = 'resolved'
        return state

    def _apply_extended(self, state, action):
        if action not in self.valid_actions(state):
            raise ValueError('Invalid blackjack action')
        hand = state['hands'][state['active_hand']]
        if action == 'split':
            aces = hand['cards'][0][0] == 'A'
            hands = [dict(cards=[card, state['deck'].pop()], units=hand['units'],
                          done=aces, split=True, split_aces=aces) for card in hand['cards']]
            for new_hand in hands:
                new_hand['done'] = aces or hand_value(new_hand['cards']) == 21
            index = state['active_hand']
            state['hands'][index:index+1] = hands
        elif action == 'stand':
            hand['done'] = True
        else:
            if action == 'double':
                hand['units'] *= 2
            hand['cards'].append(state['deck'].pop())
            hand['done'] = action == 'double' or hand_value(hand['cards']) >= 21
        return self._advance(state)

    def _resolve_extended(self, state):
        if not self.is_resolved(state):
            raise ValueError('Finish all blackjack hands before settlement')
        dealer = hand_value(state['dealer'])
        dealer_natural = dealer == 21 and len(state['dealer']) == 2
        outcomes, paid_units, total_units = [], Decimal(0), 0
        for hand in state['hands']:
            value = hand_value(hand['cards'])
            natural = not hand['split'] and len(hand['cards']) == 2 and value == 21
            if natural:
                multiplier, result = (Decimal(1), 'push') if dealer_natural else (Decimal('2.5'), 'blackjack')
            elif value > 21:
                multiplier, result = Decimal(0), 'bust'
            elif dealer_natural:
                multiplier, result = Decimal(0), 'loss'
            elif dealer > 21 or value > dealer:
                multiplier, result = Decimal(2), 'dealer_bust' if dealer > 21 else 'win'
            elif value == dealer:
                multiplier, result = Decimal(1), 'push'
            else:
                multiplier, result = Decimal(0), 'loss'
            paid_units += multiplier * hand['units']
            total_units += hand['units']
            outcomes.append(dict(cards=cards_str(hand['cards']), total=value, units=hand['units'],
                                 result=result, payout_units=str(multiplier * hand['units'])))
        result = outcomes[0]['result'] if len(outcomes) == 1 else (
            'win' if paid_units > total_units else 'push' if paid_units == total_units else 'loss')
        return paid_units / total_units, dict(result=result, hands=outcomes, dealer=dealer,
            dealer_cards=cards_str(state['dealer']), player=outcomes[0]['total'],
            player_cards=outcomes[0]['cards'], total_units=total_units, payout_units=str(paid_units))
