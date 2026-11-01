"""Shared result banner rendering."""

from __future__ import annotations

from openvegas.tui.theme import ascii_safe_mode, BORDER_HEAVY, BORDER_ASCII


def result_banner(lines: list[str], width: int = 40) -> str:
    """Render a box around result lines."""
    ascii_safe = ascii_safe_mode()
    b = BORDER_ASCII if ascii_safe else BORDER_HEAVY

    box_lines = []
    box_lines.append(f"{b['tl']}{b['h'] * width}{b['tr']}")
    for line in lines:
        # Strip Rich markup for width calculation but keep it in output
        padded = line.ljust(width)[:width]
        box_lines.append(f"{b['v']} {padded} {b['v']}")
    box_lines.append(f"{b['bl']}{b['h'] * width}{b['br']}")
    return "\n".join(box_lines)
