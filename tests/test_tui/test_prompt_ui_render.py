from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from openvegas.tui.prompt_ui import InlinePromptUI, RenderOptions, execute_render


class _AsyncRenderer:
    def __init__(self):
        self.calls = 0

    async def render_async(self, _result, _console, _opts):
        self.calls += 1
        await asyncio.sleep(0)

    async def render(self, _result, _console, _opts=None):  # pragma: no cover - safety only
        raise AssertionError("render() should not be used when render_async exists")


class _SyncRenderer:
    def __init__(self):
        self.calls = 0

    def render(self, _result, _console, _opts):
        self.calls += 1


class _SlowAsyncRenderer:
    async def render_async(self, _result, _console, _opts):
        await asyncio.sleep(0.2)


class _FakeClientPlay:
    def __init__(self):
        self.payloads = []

    async def play_game(self, _game: str, payload: dict):
        self.payloads.append(payload)
        return {
            "game_id": "g123",
            "bet_amount": "1",
            "payout": "2.5",
            "net": "1.5",
            "server_seed_hash": "abc123",
            "provably_fair": True,
            "outcome_data": {},
        }


@pytest.mark.asyncio
async def test_execute_render_prefers_render_async():
    renderer = _AsyncRenderer()
    result = await execute_render(
        renderer,
        _dummy_result(),
        Console(),
        RenderOptions(timeout_sec=1.0),
    )
    assert result["rendered"] is True
    assert renderer.calls == 1


@pytest.mark.asyncio
async def test_execute_render_sync_fallback_calls_once():
    renderer = _SyncRenderer()
    result = await execute_render(
        renderer,
        _dummy_result(),
        Console(),
        RenderOptions(timeout_sec=1.0),
    )
    assert result["rendered"] is True
    assert renderer.calls == 1


@pytest.mark.asyncio
async def test_execute_render_timeout_for_slow_renderer():
    with pytest.raises(asyncio.TimeoutError):
        await execute_render(
            _SlowAsyncRenderer(),
            _dummy_result(),
            Console(),
            RenderOptions(timeout_sec=0.01),
        )


@pytest.mark.asyncio
async def test_run_once_no_render_skips_renderer(monkeypatch):
    ui = InlinePromptUI(
        client=_FakeClientPlay(),
        console=Console(),
        render_options=RenderOptions(no_render=True, timeout_sec=0.05),
    )
    ui.state.action = "Play"
    ui.state.game = "skillshot"
    ui.state.amount = "1"

    async def _boom(*_args, **_kwargs):
        raise AssertionError("execute_render should not be called in no-render mode")

    monkeypatch.setattr("openvegas.tui.prompt_ui.execute_render", _boom)
    monkeypatch.setattr("openvegas.tui.prompt_ui.load_config", lambda: {"animation": False})

    out = await ui.run_once()
    assert "LIVE MODE" in out
    assert "Payout: 2.5 | Net: 1.5" in out
    assert "openvegas verify g123" in out


@pytest.mark.asyncio
async def test_run_once_timeout_then_next_action_still_works(monkeypatch):
    ui = InlinePromptUI(
        client=_FakeClientPlay(),
        console=Console(),
        render_options=RenderOptions(no_render=False, timeout_sec=0.05, fast_mode=True, duration_sec=0.1),
    )
    ui.state.action = "Play"
    ui.state.game = "skillshot"
    ui.state.amount = "1"
    ui.state.horse = "99"  # hidden for skillshot and should not be sent

    calls = {"n": 0}

    async def _fake_execute_render(_renderer, _result, _console, _opts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.TimeoutError()
        return {"rendered": True}

    monkeypatch.setattr("openvegas.tui.prompt_ui.execute_render", _fake_execute_render)
    monkeypatch.setattr("openvegas.tui.prompt_ui.load_config", lambda: {"animation": False})

    first = await ui.run_once()
    second = await ui.run_once()

    assert "Render timed out" in first
    assert "Showing result summary only" in first
    assert "LIVE MODE" in second
    assert ui.client.payloads[0] == {"amount": 1.0}
    assert ui.client.payloads[1] == {"amount": 1.0}


def _dummy_result():
    from decimal import Decimal
    from openvegas.games.base import GameResult

    return GameResult(
        game_id="g",
        player_id="u",
        bet_amount=Decimal("1"),
        payout=Decimal("0"),
        net=Decimal("-1"),
        outcome_data={},
        server_seed="",
        server_seed_hash="",
        client_seed="",
        nonce=0,
        provably_fair=True,
    )
