from __future__ import annotations

import pytest

from openvegas.client import APIError
from openvegas.tui.prompt_ui import InlinePromptUI


class _Client401:
    async def get_balance(self):
        raise APIError(401, "Invalid or expired token")


class _ClientOK:
    async def get_balance(self):
        return {"balance": "1.0"}


def test_steps_include_card_games_without_bet_type():
    ui = InlinePromptUI(client=_ClientOK())
    ui.state.action = "Play"
    ui.state.game = "blackjack"
    assert ui._steps_for_state() == ["action", "game", "inputs", "review"]

    ui.state.game = "horse"
    assert ui._steps_for_state() == ["action", "game", "bet_type", "inputs", "review"]


@pytest.mark.asyncio
async def test_auth_preflight_returns_false_on_401():
    ui = InlinePromptUI(client=_Client401())
    ok = await ui._ensure_auth()
    assert ok is False


def test_review_step_supports_back(monkeypatch):
    ui = InlinePromptUI(client=_ClientOK())
    monkeypatch.setattr("openvegas.tui.prompt_ui.Prompt.ask", lambda *a, **kw: "b")
    outcome = ui._run_step("review", allow_back=True)
    assert outcome == "back"
