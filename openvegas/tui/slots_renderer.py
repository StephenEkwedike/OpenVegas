"""Slot machine reel display."""

from __future__ import annotations

from openvegas.tui.theme import ascii_safe_mode

SYMBOL_DISPLAY = {
    "7":      {"utf": "[bold red]⑦[/bold red]",    "ascii": "7"},
    "BAR":    {"utf": "[bold white]▬[/bold white]", "ascii": "B"},
    "CHERRY": {"utf": "[red]C[/red]",               "ascii": "C"},
    "LEMON":  {"utf": "[yellow]L[/yellow]",          "ascii": "L"},
    "BELL":   {"utf": "[bold yellow]b[/bold yellow]", "ascii": "b"},
    "STAR":   {"utf": "[bold cyan]★[/bold cyan]",    "ascii": "*"},
}


def _sym(name: str, ascii_safe: bool) -> str:
    # Unknown symbols are clamped to '?' so fixed-width reel borders never break.
    d = SYMBOL_DISPLAY.get(name, {"utf": "[dim]?[/dim]", "ascii": "?"})
    return d["ascii"] if ascii_safe else d["utf"]


def render_reels(reels: list[str], hit: bool) -> str:
    """Render 3-reel slot machine display."""
    ascii_safe = ascii_safe_mode()
    padded = list(reels or [])
    while len(padded) < 3:
        padded.append("?")
    s = [_sym(r, ascii_safe) for r in padded[:3]]

    if ascii_safe:
        border = "+---+---+---+"
        line = f"| {s[0]} | {s[1]} | {s[2]} |"
        return f"{border}\n{line}\n{border}"

    win_style = "[bold on green]" if hit else ""
    end_style = "[/bold on green]" if hit else ""

    return (
        f"╔═══╦═══╦═══╗\n"
        f"║{win_style} {s[0]} {end_style}║{win_style} {s[1]} {end_style}║{win_style} {s[2]} {end_style}║\n"
        f"╚═══╩═══╩═══╝"
    )
