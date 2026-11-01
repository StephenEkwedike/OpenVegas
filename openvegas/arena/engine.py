"""Server-authoritative no-limit Hold'em using non-redeemable test chips.

No wallet, inference or external provider calls. Actions return a new state so a
failed validation cannot partially mutate the caller's persisted game.
"""

from copy import deepcopy
import hashlib
import random

from openvegas.casino.poker import DECK, _best_of_seven

VERSION = "arena.holdem.v1"
MAX_SEATS = 10
STREETS = ("preflop", "flop", "turn", "river")


def integer(value, minimum=0, maximum=1_000_000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid_integer")
    return value


def new_hand(seed, stacks, button=0):
    if not 2 <= len(stacks) <= MAX_SEATS:
        raise ValueError("two_to_ten_seats_required")
    stacks = [integer(x, 2) for x in stacks]
    integer(button, 0, len(stacks) - 1)
    deck = [r + s for r, s in DECK]
    random.Random(seed).shuffle(deck)
    n = len(stacks)
    s = {
        "engine_version": VERSION,
        "seed_commitment": hashlib.sha256(str(seed).encode()).hexdigest(),
        "deck": deck,
        "holes": [[deck.pop(), deck.pop()] for _ in stacks],
        "board": [],
        "burned": [],
        "initial": stacks[:],
        "stacks": stacks[:],
        "committed": [0] * n,
        "street_bets": [0] * n,
        "folded": [False] * n,
        "button": button,
        "street": "preflop",
        "current_bet": 2,
        "last_raise": 2,
        "acted_at": {},
        "pending": list(range(n)),
        "version": 0,
        "history": [],
        "resolved": False,
        "payouts": [0] * n,
        "pots": [],
    }
    sb = button if n == 2 else (button + 1) % n
    bb = (sb + 1) % n
    _pay(s, sb, 1)
    _pay(s, bb, 2)
    s["actor"] = (bb + 1) % n
    _advance(s, (s["actor"] - 1) % n)
    return s


def _pay(s, seat, amount):
    s["stacks"][seat] -= amount
    s["committed"][seat] += amount
    s["street_bets"][seat] += amount


def _active(s):
    return [i for i in range(len(s["stacks"])) if not s["folded"][i]]


def legal(s, seat):
    if s["resolved"] or seat != s["actor"]:
        return {}
    due = max(0, s["current_bet"] - s["street_bets"][seat])
    actions = {"fold": {}, "call" if due else "check": {"chips": min(due, s["stacks"][seat])}}
    maximum = s["street_bets"][seat] + s["stacks"][seat]
    last = s["acted_at"].get(str(seat))
    reopened = last is None or s["current_bet"] - last >= s["last_raise"]
    others_can_bet = any(i != seat and s["stacks"][i] for i in _active(s))
    if maximum > s["current_bet"] and reopened and others_can_bet:
        actions["raise"] = {
            "min_to": min(maximum, s["current_bet"] + s["last_raise"]),
            "max_to": maximum,
        }
    return actions


def observation(s, seat):
    integer(seat, 0, len(s["stacks"]) - 1)
    result = {
        k: deepcopy(s[k])
        for k in (
            "engine_version",
            "seed_commitment",
            "board",
            "stacks",
            "committed",
            "street_bets",
            "folded",
            "button",
            "street",
            "version",
            "history",
            "resolved",
            "payouts",
            "pots",
            "actor",
        )
    }
    result.update(
        seat=seat,
        cards=list(s["holes"][seat]),
        pot=sum(s["committed"]),
        legal_actions=legal(s, seat),
        unit="test_chips",
        redeemable=False,
    )
    # Folded hands are never revealed to opponents, even after settlement.
    if s["resolved"] and len(_active(s)) > 1:
        result["showdown"] = {str(i): s["holes"][i][:] for i in _active(s)}
    return result


def step(state, seat, action, amount=None):
    s = deepcopy(state)
    integer(seat, 0, len(s["stacks"]) - 1)
    options = legal(s, seat)
    if action not in options:
        raise ValueError("illegal_action_or_wrong_turn")
    if action != "raise" and amount is not None:
        raise ValueError("amount_only_for_raise")
    paid = 0
    if action == "fold":
        s["folded"][seat] = True
    elif action == "call":
        paid = options[action]["chips"]
        _pay(s, seat, paid)
    elif action == "raise":
        integer(amount, options["raise"]["min_to"], options["raise"]["max_to"])
        increase = amount - s["current_bet"]
        paid = amount - s["street_bets"][seat]
        _pay(s, seat, paid)
        if increase >= s["last_raise"]:
            s["last_raise"] = increase
        s["current_bet"] = amount
        s["pending"] = [i for i in _active(s) if i != seat and s["stacks"][i]]
    s["acted_at"][str(seat)] = s["current_bet"]
    s["pending"] = [i for i in s["pending"] if i != seat]
    s["version"] += 1
    s["history"].append(
        {
            "seat": seat,
            "street": s["street"],
            "action": action,
            "amount_to": amount,
            "paid": paid,
            "version": s["version"],
        }
    )
    _advance(s, seat)
    assert sum(s["stacks"]) + (0 if s["resolved"] else sum(s["committed"])) == sum(s["initial"])
    return s


def _advance(s, after):
    while True:
        live = _active(s)
        if len(live) == 1:
            _settle(s)
            return
        can_act = [i for i in live if s["stacks"][i] > 0]
        s["pending"] = [i for i in s["pending"] if i in can_act]
        # A lone player with chips only acts if still facing an unmatched bet.
        if len(can_act) == 1 and s["street_bets"][can_act[0]] >= s["current_bet"]:
            s["pending"] = []
        if s["pending"]:
            n = len(s["stacks"])
            s["actor"] = next(
                (after + k) % n for k in range(1, n + 1) if (after + k) % n in s["pending"]
            )
            return
        if s["street"] == "river":
            _settle(s)
            return
        s["street"] = STREETS[STREETS.index(s["street"]) + 1]
        s["burned"].append(s["deck"].pop())
        for _ in range(3 if s["street"] == "flop" else 1):
            s["board"].append(s["deck"].pop())
        s.update(
            street_bets=[0] * len(s["stacks"]),
            current_bet=0,
            last_raise=2,
            acted_at={},
            pending=can_act if len(can_act) > 1 else [],
        )
        after = s["button"]


def split_pots(contributions, folded, scores, button=0):
    """Separate contribution tiers; folded chips count but cannot win contested pots."""
    if (
        not 2 <= len(contributions) <= MAX_SEATS
        or len(folded) != len(contributions)
        or len(scores) != len(folded)
    ):
        raise ValueError("invalid_pot_inputs")
    for c in contributions:
        integer(c)
    payouts = [0] * len(contributions)
    pots = []
    previous = 0
    for level in sorted(set(contributions) - {0}):
        contributors = [i for i, c in enumerate(contributions) if c >= level]
        amount = (level - previous) * len(contributors)
        previous = level
        eligible = [i for i in contributors if not folded[i]]
        if len(contributors) == 1:
            winners = contributors  # Uncalled excess is returned, not a prize.
        elif not eligible:
            raise ValueError("pot_without_eligible_player")
        else:
            best = max(scores[i] for i in eligible)
            winners = [i for i in eligible if scores[i] == best]
        winners.sort(key=lambda i: (i - button - 1) % len(contributions))
        for index, i in enumerate(winners):
            payouts[i] += amount // len(winners) + int(index < amount % len(winners))
        pots.append(
            {
                "amount": amount,
                "eligible": eligible,
                "winners": winners,
                "uncalled_return": len(contributors) == 1,
            }
        )
    return payouts, pots


def _settle(s):
    live = _active(s)
    scores = [(0,)] * len(s["stacks"])
    if len(live) > 1:
        for i in live:
            scores[i] = _best_of_seven([(c[:-1], c[-1]) for c in s["holes"][i] + s["board"]])[0]
    s["payouts"], s["pots"] = split_pots(s["committed"], s["folded"], scores, s["button"])
    s["stacks"] = [a + b for a, b in zip(s["stacks"], s["payouts"])]
    s.update(resolved=True, actor=None, pending=[])
