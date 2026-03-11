from __future__ import annotations

from rich.console import Console

from openvegas.tui.confetti import render_confetti


def test_render_confetti_bounded_frames(monkeypatch):
    monkeypatch.setattr("openvegas.tui.confetti.time.sleep", lambda *_: None)
    console = Console(record=True)
    render_confetti(console, frames=3, width=8)
    out = console.export_text()
    # At least 3 printed lines from bounded frame loop.
    assert len([line for line in out.splitlines() if line.strip()]) >= 3
