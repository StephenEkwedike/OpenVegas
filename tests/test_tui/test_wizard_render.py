from __future__ import annotations

import pytest

pytest.importorskip("textual")

from openvegas.tui.wizard import OpenVegasWizard
from openvegas.tui.wizard_state import WizardState


class _ClientPlay:
    async def play_game(self, game: str, payload: dict) -> dict:
        return {
            "game_id": "g123",
            "bet_amount": "1",
            "payout": "2.5",
            "net": "1.5",
            "server_seed_hash": "abc123",
            "provably_fair": True,
            "outcome_data": {},
        }


class _ClientDemo:
    async def play_game_demo(self, game: str, payload: dict) -> dict:
        return {
            "game_id": "g-demo",
            "bet_amount": "1",
            "payout": "1.8",
            "net": "0.8",
            "server_seed_hash": "demo123",
            "provably_fair": False,
            "demo_mode": True,
            "outcome_data": {},
        }


class _FakeRenderer:
    async def render(self, _result, _console):
        return None


@pytest.mark.asyncio
async def test_play_invokes_horse_renderer(monkeypatch):
    app = OpenVegasWizard()
    app.state = WizardState(action="Play", game="horse", amount="1", horse="1", bet_type="win")
    app.client = _ClientPlay()

    calls: dict = {}

    async def _fake_render(renderer_cls, gr):
        calls["renderer"] = renderer_cls
        calls["result"] = gr

    monkeypatch.setattr(app, "_renderer_for", lambda _game: _FakeRenderer)
    monkeypatch.setattr(app, "_render_game", _fake_render)
    monkeypatch.setattr(app, "_set_output", lambda message: calls.setdefault("output", message))

    await app._run_action()

    assert calls["renderer"] is _FakeRenderer
    assert calls["result"].game_id == "g123"
    assert "LIVE MODE" in calls["output"]


@pytest.mark.asyncio
async def test_demo_play_invokes_horse_renderer_and_marks_noncanonical(monkeypatch):
    app = OpenVegasWizard()
    app.state = WizardState(action="Play (Demo Win)", game="horse", amount="1", horse="1", bet_type="win")
    app.client = _ClientDemo()

    calls: dict = {}

    async def _fake_render(renderer_cls, gr):
        calls["renderer"] = renderer_cls
        calls["result"] = gr

    monkeypatch.setattr(app, "_renderer_for", lambda _game: _FakeRenderer)
    monkeypatch.setattr(app, "_render_game", _fake_render)
    monkeypatch.setattr(app, "_set_output", lambda message: calls.setdefault("output", message))

    await app._run_action()

    assert calls["renderer"] is _FakeRenderer
    assert calls["result"].game_id == "g-demo"
    assert "DEMO MODE (canonical: false)" in calls["output"]
    assert "--demo" in calls["output"]


@pytest.mark.asyncio
async def test_play_win_triggers_confetti_when_animation_enabled(monkeypatch):
    app = OpenVegasWizard()
    app.state = WizardState(action="Play", game="horse", amount="1", horse="1", bet_type="win")
    app.client = _ClientPlay()

    calls: dict = {"confetti": 0}

    async def _fake_render(_renderer_cls, _gr):
        return None

    async def _fake_confetti():
        calls["confetti"] += 1

    monkeypatch.setattr(app, "_renderer_for", lambda _game: _FakeRenderer)
    monkeypatch.setattr(app, "_render_game", _fake_render)
    monkeypatch.setattr(app, "_render_confetti", _fake_confetti)
    monkeypatch.setattr("openvegas.tui.wizard.load_config", lambda: {"animation": True})
    monkeypatch.setattr(app, "_set_output", lambda _message: None)

    await app._run_action()
    assert calls["confetti"] == 1
