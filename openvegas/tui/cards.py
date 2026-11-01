"""Shared card rendering for blackjack, poker, baccarat."""

from __future__ import annotations

from openvegas.casino.constants import HIDDEN_CARD_TOKEN
from openvegas.tui.theme import ascii_safe_mode

SUIT_SYMBOLS = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
SUIT_ASCII = {"S": "S", "H": "H", "D": "D", "C": "C"}
RED_SUITS = {"H", "D"}


def render_card(rank: str, suit: str, ascii_safe: bool = False, hidden: bool = False) -> list[str]:
    """Return 3-line card art."""
    if hidden:
        if ascii_safe:
            return ["+---+", "|? ?|", "+---+"]
        return ["┌───┐", "│? ?│", "└───┘"]

    sym = SUIT_ASCII[suit] if ascii_safe else SUIT_SYMBOLS[suit]
    r = rank.rjust(2)

    if ascii_safe:
        return ["+---+", f"|{r}{sym}|", "+---+"]

    color = "red" if suit in RED_SUITS else "white"
    return [
        "┌───┐",
        f"│[{color}]{r}{sym}[/{color}]│",
        "└───┘",
    ]


def parse_card_str(card: str) -> tuple[str, str]:
    """Parse 'KH', '10S', etc. into (rank, suit)."""
    if len(card) == 2:
        return card[0], card[1]
    if len(card) == 3:
        return card[:2], card[2]
    return card[:-1], card[-1]


def render_hand(
    cards: list[str],
    label: str = "",
    value: int | None = None,
    ascii_safe: bool | None = None,
    show_positions: bool = False,
) -> str:
    """Render multiple cards side-by-side with optional label and value.
    cards: list of 'RankSuit' strings (e.g., ['KH', '9S', '10D']).
    """
    if ascii_safe is None:
        ascii_safe = ascii_safe_mode()

    rendered = []
    for c in cards:
        if c == HIDDEN_CARD_TOKEN:
            rendered.append(render_card("?", "S", ascii_safe, hidden=True))
            continue
        rank, suit = parse_card_str(c)
        rendered.append(render_card(rank, suit, ascii_safe))

    lines = []

    # Header
    header = label
    if value is not None:
        header += f" ({value})"
    if header:
        lines.append(f"  {header}")

    # Cards side-by-side (3 rows)
    if rendered:
        for row in range(3):
            line = "  " + " ".join(card[row] for card in rendered)
            lines.append(line)

    # Position labels
    if show_positions:
        positions = "  " + " ".join(f" [{i+1}] " for i in range(len(rendered)))
        lines.append(positions)

    return "\n".join(lines)
