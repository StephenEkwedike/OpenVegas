"""Bounded public-state tables for native forms. No ANSI, I/O or animation waits."""
from __future__ import annotations

import re
from .game_result import amount

from rich.text import Text

from openvegas.casino.constants import HIDDEN_CARD_TOKEN
from openvegas.casino.roulette import RED_NUMBERS
from openvegas.casino.slots import SYMBOLS
from openvegas.tui.cards import render_hand
from openvegas.tui.roulette_renderer import WHEEL_ORDER

GAMES = {"poker", "blackjack", "baccarat", "roulette", "slots", "horse"}
CARD = re.compile(r"(?:[2-9JQKA]|10)[SHDC]\Z")


def label(value, limit=48):
    return re.sub(r"[^a-zA-Z0-9 ._+:/-]", "", str(value))[:limit].replace("_", " ")


def cards(value):
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value[:22]:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            item = "".join(str(part)[:2] for part in item)
        result.append(item if isinstance(item, str) and CARD.fullmatch(item) else HIDDEN_CARD_TOKEN)
    return result


def hand(name, values):
    if not values:
        return [name + ": waiting for deal"]
    lines = [name]
    for offset in range(0, len(values), 6):
        lines.extend(Text.from_markup(render_hand(values[offset:offset + 6], ascii_safe=False)).plain.splitlines())
    return lines


def pocket(number):
    return f"{number:02d}" + ("G" if number == 0 else "R" if number in RED_NUMBERS else "B")


def table(game, result):
    """Whitelist display fields; settlement cards come from the engine's outcome."""
    if game not in GAMES or not isinstance(result, dict):
        return "OpenVegas / Table unavailable"
    settled = result.get("current_state") == "resolved"
    state = result.get("outcome" if settled else "state") or {}
    if not isinstance(state, dict):
        state = {}
    lines = ["OPENVEGAS / " + game.upper(), "Round settled" if settled else "Your move"]
    if game == "poker" and not settled:
        stage = {"decision": "3 of 5 shared cards", "turn": "4 of 5 shared cards",
                 "river": "5 of 5 shared cards / ready to show hands"}.get(state.get("phase"))
        if stage:
            lines.append(stage + " / single stake, no additional bet")
    if game in {"poker", "blackjack", "baccarat"}:
        positions = ("dealer", "community", "player") if game == "poker" else (
            ("dealer", "player") if game == "blackjack" else ("banker", "player"))
        for position in positions:
            if game == 'blackjack' and position == 'player' and state.get('hands'):
                from openvegas.casino.blackjack import hand_value
                for index, player_hand in enumerate(state['hands'][:4]):
                    active = not settled and index == state.get('active_hand') and not player_hand.get('done')
                    total = player_hand.get('total') if settled else hand_value(player_hand['cards'])
                    title = f'Hand {index+1} / {total}' + (' / YOUR TURN' if active else '')
                    lines.extend(hand(title, cards(player_hand['cards'])))
                    if settled:
                        lines.append(label(player_hand.get('result', '')))
                if state.get('total_wager_v'):
                    lines.append('Total stake: ' + amount(state['total_wager_v']))
                continue
            values = cards(state.get(position + "_cards" if settled else position))
            # Defense in depth even if a malformed upstream response bypasses
            # the public-state filter. Never use outcome fields before settlement.
            if not settled and position == "dealer":
                values = ([HIDDEN_CARD_TOKEN] * len(values) if game == "poker" else
                          values[:1] + [HIDDEN_CARD_TOKEN] * max(0, len(values) - 1))
            lines.extend(hand(position.title(), values))
            if settled and state.get(position + "_rank"):
                lines.append("  " + label(state[position + "_rank"]))
        if not settled and game in {"poker", "blackjack"}:
            lines.append("? = face-down dealer card")
    elif game == "roulette":
        number = state.get("result")
        valid_number = type(number) is int and 0 <= number <= 36
        if valid_number:
            index = WHEEL_ORDER.index(number)
            neighbors = [WHEEL_ORDER[(index + n) % 37] for n in range(-2, 3)]
            lines += ["Wheel / landed: " + pocket(number),
                      "  " + "  ".join(("[" + pocket(n) + "]") if n == number else pocket(n) for n in neighbors)]
        else:
            lines += ["European wheel / single zero", "Ball: awaiting your spin"]
        lines += ["Layout / R red, B black, G green", "00G"]
        for start in (1, 19):
            for row in (2, 1, 0):
                lines.append("  " + " ".join(pocket(n) for n in range(start + row, start + 18, 3)))
        lines.append("RED / BLACK / ODD / EVEN / NUMBER")
        bet = state.get("bet_type")
        lines.append("Bet: " + (label(bet).removeprefix("bet ") if bet else "choose below"))
        if bet == "bet_number" and type(state.get("bet_value")) is int:
            lines.append("Selected number: " + str(state["bet_value"]))
    elif game == 'horse':
        lines.append('Horse Racing')
        for horse in state.get('horses', [])[:8]:
            lines.append(f"{label(horse.get('number'))}. {label(horse.get('name'))}")
        if settled:
            lines.append('Winner: ' + label(state.get('winner', 'see finish order')))
            lines.append('Finish: ' + ' / '.join(label(n) for n in state.get('finish_order_nums', [])[:8]))
    else:
        reels = state.get("reels")
        if not isinstance(reels, list) or len(reels) != 3:
            reels = ["?", "?", "?"]
        reels = [v if isinstance(v, str) and v in SYMBOLS else "?" for v in reels]
        lines += ["PAYLINE / 3 reels", "  +--------+--------+--------+",
                  "  |" + "|".join(v.center(8) for v in reels) + "|",
                  "  +--------+--------+--------+",
                  "Result from server" if settled else "Choose spin below"]
    if settled:
        if isinstance(state.get("result"), str):
            lines.append("Outcome: " + label(state["result"]))
        for key, title in (("payout_v", "Payout"), ("net_v", "Net")):
            if key in result:
                lines.append(title + ": " + amount(result[key], signed=key == 'net_v'))
    return "\n".join(lines)
