from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from rich.console import Console
from rich.panel import Panel

from openvegas.casino.constants import HIDDEN_CARD_TOKEN
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
        self.demo_payloads = []

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

    async def play_game_demo(self, _game: str, payload: dict):
        self.demo_payloads.append(payload)
        return {
            "game_id": "g123-demo",
            "bet_amount": "1",
            "payout": "2.5",
            "net": "1.5",
            "server_seed_hash": "abc123-demo",
            "provably_fair": False,
            "outcome_data": {"demo_mode": True},
        }


class _FakeClientHorseQuote:
    def __init__(self):
        self.calls = []

    async def play_horse_quote(self, *, quote_id: str, horse: int, idempotency_key: str, demo_mode: bool = False):
        self.calls.append(
            {
                "quote_id": quote_id,
                "horse": horse,
                "idempotency_key": idempotency_key,
                "demo_mode": demo_mode,
            }
        )
        return {
            "game_id": "g-horse-1",
            "quote_id": quote_id,
            "bet_amount": "19.999932",
            "payout": "76.000000",
            "net": "56.000068",
            "server_seed_hash": "seed-hash",
            "provably_fair": True,
            "outcome_data": {"finish_order_nums": [horse, 2, 3]},
        }


@pytest.fixture(autouse=True)
def _disable_win_always_env(monkeypatch):
    monkeypatch.delenv("OPENVEGAS_WIN_ALWAYS", raising=False)
    monkeypatch.delenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", raising=False)


class _FakeClientDepositSaved:
    def __init__(self):
        self.saved_calls = 0
        self.charge_calls = 0
        self.checkout_calls = 0
        self.base_url = "http://localhost:8000"

    async def get_saved_topup_payment_method(self):
        self.saved_calls += 1
        return {
            "available": True,
            "brand": "visa",
            "last4": "4242",
        }

    async def charge_saved_topup(self, amount):
        self.charge_calls += 1
        _ = amount
        return {
            "topup_id": "top_saved_1",
            "status": "paid",
        }

    async def create_topup_checkout(self, amount):
        self.checkout_calls += 1
        _ = amount
        return {
            "topup_id": "top_checkout_1",
            "status": "checkout_created",
            "checkout_url": "https://checkout.example",
        }


class _FakeClientDepositNoSaved:
    def __init__(self):
        self.saved_calls = 0
        self.charge_calls = 0
        self.checkout_calls = 0
        self.base_url = "http://localhost:8000"

    async def get_saved_topup_payment_method(self):
        self.saved_calls += 1
        return {"available": False}

    async def charge_saved_topup(self, amount):
        self.charge_calls += 1
        _ = amount
        return {}

    async def create_topup_checkout(self, amount):
        self.checkout_calls += 1
        _ = amount
        return {
            "topup_id": "top_checkout_2",
            "status": "checkout_created",
            "checkout_url": "https://checkout.example/top2",
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
    ui.state.amount = "50"

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
    ui.state.amount = "50"
    ui.state.horse = "99"  # hidden for skillshot and should not be sent

    calls = {"n": 0}

    async def _fake_execute_render(_renderer, _result, _console, _opts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.TimeoutError()
        return {"rendered": True}

    monkeypatch.setattr("openvegas.tui.prompt_ui.execute_render", _fake_execute_render)
    monkeypatch.setattr("openvegas.tui.prompt_ui.load_config", lambda: {"animation": False})
    async def _fake_interactive(_self, _console):
        return 20

    monkeypatch.setattr("openvegas.games.skill_shot.SkillShotGame.render_interactive", _fake_interactive)

    first = await ui.run_once()
    second = await ui.run_once()

    assert "Render timed out" in first
    assert "Showing result summary only" in first
    assert "LIVE MODE" in second
    assert ui.client.payloads[0] == {"amount": 50.0, "stop_position": 20}
    assert ui.client.payloads[1] == {"amount": 50.0, "stop_position": 20}


@pytest.mark.asyncio
async def test_run_once_horse_uses_quote_play_payload(monkeypatch):
    ui = InlinePromptUI(
        client=_FakeClientHorseQuote(),
        console=Console(),
        render_options=RenderOptions(no_render=True, timeout_sec=0.05),
    )
    ui.state.action = "Play"
    ui.state.game = "horse"
    ui.state.bet_type = "win"
    ui.state.amount = "50"
    ui.state.horse = "1"
    ui.state.horse_quote_id = "q-1"
    ui.state.horse_quote_board_hash = "board-hash"
    ui.state.horse_quote_rows = [
        {
            "number": 1,
            "name": "Thunder Byte",
            "odds": "3.800000",
            "effective_multiplier": "3.800000",
            "unit_price_v": "0.263157",
            "max_units": 76,
            "debit_v": "19.999932",
            "payout_if_hit_v": "76.000000",
            "selectable": True,
        }
    ]
    ui.state.horse_quote_selected = dict(ui.state.horse_quote_rows[0])

    monkeypatch.setattr("openvegas.tui.prompt_ui.load_config", lambda: {"animation": False})

    out = await ui.run_once()
    assert "LIVE MODE" in out
    assert "Game ID: g-horse-1" in out
    assert len(ui.client.calls) == 1
    assert ui.client.calls[0]["quote_id"] == "q-1"
    assert ui.client.calls[0]["horse"] == 1
    assert ui.client.calls[0]["demo_mode"] is False


@pytest.mark.asyncio
async def test_run_once_horse_uses_demo_quote_when_win_always_enabled(monkeypatch):
    ui = InlinePromptUI(
        client=_FakeClientHorseQuote(),
        console=Console(),
        render_options=RenderOptions(no_render=True, timeout_sec=0.05),
    )
    ui.state.action = "Play"
    ui.state.game = "horse"
    ui.state.bet_type = "win"
    ui.state.amount = "50"
    ui.state.horse = "1"
    ui.state.horse_quote_id = "q-1"
    ui.state.horse_quote_rows = [
        {
            "number": 1,
            "name": "Thunder Byte",
            "odds": "3.800000",
            "effective_multiplier": "3.800000",
            "unit_price_v": "0.263157",
            "max_units": 76,
            "debit_v": "19.999932",
            "payout_if_hit_v": "76.000000",
            "selectable": True,
        }
    ]
    ui.state.horse_quote_selected = dict(ui.state.horse_quote_rows[0])

    monkeypatch.setenv("OPENVEGAS_WIN_ALWAYS", "Y")
    monkeypatch.setattr("openvegas.tui.prompt_ui.load_config", lambda: {"animation": False})

    out = await ui.run_once()
    assert "LIVE MODE" in out
    assert len(ui.client.calls) == 1
    assert ui.client.calls[0]["demo_mode"] is True


@pytest.mark.asyncio
async def test_run_once_skillshot_uses_demo_endpoint_when_win_always_enabled(monkeypatch):
    client = _FakeClientPlay()
    ui = InlinePromptUI(
        client=client,
        console=Console(),
        render_options=RenderOptions(no_render=True, timeout_sec=0.05),
    )
    ui.state.action = "Play"
    ui.state.game = "skillshot"
    ui.state.amount = "50"
    monkeypatch.setenv("OPENVEGAS_WIN_ALWAYS", "Y")
    monkeypatch.setattr("openvegas.tui.prompt_ui.load_config", lambda: {"animation": False})

    out = await ui.run_once()
    assert "LIVE MODE" in out
    assert client.payloads == []
    assert client.demo_payloads == [{"amount": 50.0}]


@pytest.mark.asyncio
async def test_deposit_uses_saved_charge_when_available(monkeypatch):
    ui = InlinePromptUI(client=_FakeClientDepositSaved(), console=Console(record=True))
    ui.state.action = "Deposit"
    ui.state.amount = "10"
    monkeypatch.setattr("openvegas.tui.prompt_ui.Confirm.ask", lambda *_args, **_kwargs: True)

    out = await ui.run_once()

    assert "Top-up ID: top_saved_1" in out
    assert "Status: paid" in out
    assert "Saved card charged successfully." in out
    assert "Charged amount: $10.00" in out
    assert ui.client.saved_calls == 1
    assert ui.client.charge_calls == 1
    assert ui.client.checkout_calls == 0


@pytest.mark.asyncio
async def test_deposit_falls_back_to_checkout_when_no_saved(monkeypatch):
    ui = InlinePromptUI(client=_FakeClientDepositNoSaved(), console=Console(record=True))
    ui.state.action = "Deposit"
    ui.state.amount = "10"
    monkeypatch.setattr("openvegas.tui.prompt_ui.webbrowser.open", lambda *_args, **_kwargs: False)

    out = await ui.run_once()

    assert "Top-up ID: top_checkout_2" in out
    assert "Status: checkout_created" in out
    assert "Checkout URL: https://checkout.example/top2" in out
    assert ui.client.saved_calls == 1
    assert ui.client.charge_calls == 0
    assert ui.client.checkout_calls == 1


def test_blackjack_round_state_handles_hidden_token_without_crash():
    ui = InlinePromptUI(client=_FakeClientPlay(), console=Console())
    state = {
        "player": [["10", "H"], ["7", "C"]],
        "dealer": [["9", "S"], HIDDEN_CARD_TOKEN],
    }
    out = ui._render_round_state("blackjack", state, "awaiting_action")
    assert "PLAYER" in out
    assert "DEALER" in out


def test_round_state_hides_internal_state_copy_for_roulette_and_slots():
    ui = InlinePromptUI(client=_FakeClientPlay(), console=Console())
    roulette_out = ui._render_round_state(
        "roulette",
        {"bet_type": "bet_red"},
        "awaiting_action",
    )
    slots_out = ui._render_round_state(
        "slots",
        {},
        "awaiting_action",
    )
    assert "State:" not in roulette_out
    assert "Next:" not in roulette_out
    assert roulette_out == "Bet selected: RED"
    assert "State:" not in slots_out
    assert "Next:" not in slots_out
    assert slots_out == "Ready to spin."


def test_round_state_hides_internal_state_copy_for_blackjack():
    ui = InlinePromptUI(client=_FakeClientPlay(), console=Console())
    state = {
        "player": [["10", "H"], ["7", "C"]],
        "dealer": [["9", "S"], ["5", "D"]],
    }
    out = ui._render_round_state("blackjack", state, "awaiting_action")
    assert "State:" not in out
    assert "PLAYER" in out
    assert "DEALER" in out


class _FakeHumanClientRouletteSpin:
    def __init__(self):
        self.actions = []

    async def human_casino_start_session(self, **kwargs):
        _ = kwargs
        return {"casino_session_id": "s1"}

    async def human_casino_start_round(self, **kwargs):
        _ = kwargs
        return {
            "round_id": "r1",
            "current_state": "awaiting_action",
            "state": {"bet_type": "bet_red"},
            "valid_actions": ["spin"],
        }

    async def human_casino_action(self, **kwargs):
        self.actions.append(kwargs.get("action"))
        return {
            "round_id": "r1",
            "current_state": "resolvable",
            "state": {"bet_type": "bet_red"},
            "valid_actions": [],
        }

    async def human_casino_resolve(self, **kwargs):
        _ = kwargs
        return {
            "outcome": {"result": 12, "bet_type": "bet_red", "hit": True},
            "payout_v": "100.000000",
            "net_v": "50.000000",
        }


class _FakeHumanClientRouletteDemoAutoplay:
    def __init__(self):
        self.demo_calls = []

    async def human_casino_start_session(self, **kwargs):
        _ = kwargs
        return {"casino_session_id": "s1"}

    async def human_casino_demo_autoplay(self, **kwargs):
        self.demo_calls.append(kwargs)
        return {
            "round_id": "demo-r1",
            "outcome": {"result": 9, "bet_type": kwargs.get("preferred_action", "bet_red"), "hit": True},
            "payout_v": "200.000000",
            "net_v": "100.000000",
        }


class _FakeHumanClientCardDemoAutoplayMatrix:
    def __init__(self):
        self.demo_calls = []

    async def human_casino_start_session(self, **kwargs):
        _ = kwargs
        return {"casino_session_id": "s1"}

    async def human_casino_demo_autoplay(self, **kwargs):
        self.demo_calls.append(kwargs)
        game_code = str(kwargs.get("game_code", ""))
        outcome_by_game = {
            "blackjack": {
                "player_cards": ["AH", "KD"],
                "player": 21,
                "dealer_cards": ["9C", "7D", "5H"],
                "dealer": 21,
                "result": "push",
            },
            "roulette": {
                "result": 9,
                "bet_type": kwargs.get("preferred_action", "bet_red"),
                "hit": True,
            },
            "slots": {
                "reels": ["7", "7", "7"],
                "hit": True,
            },
            "poker": {
                "player_cards": ["AS", "AD"],
                "dealer_cards": ["KH", "KD"],
                "community_cards": ["2C", "3D", "4H", "5S", "6C"],
                "player_rank": "pair",
                "dealer_rank": "pair",
                "result": "player",
            },
            "baccarat": {
                "player_cards": ["9H", "2C"],
                "banker_cards": ["7S", "2D"],
                "player_total": 1,
                "banker_total": 9,
                "bet_type": "bet_banker",
                "result": "banker",
            },
        }
        return {
            "round_id": f"demo-{game_code}-1",
            "outcome": outcome_by_game.get(game_code, {}),
            "payout_v": "200.000000",
            "net_v": "100.000000",
        }


@pytest.mark.asyncio
async def test_roulette_autospin_skips_manual_prompt(monkeypatch):
    ui = InlinePromptUI(client=_FakeHumanClientRouletteSpin(), console=Console())
    ui.state.game = "roulette"
    monkeypatch.setattr("openvegas.tui.prompt_ui.load_config", lambda: {"animation": False})

    out = await ui._run_human_card_round(stake=Decimal("50"))
    assert "Round ID: r1" in out
    assert ui.client.actions == ["spin"]


@pytest.mark.asyncio
async def test_roulette_round_state_panel_not_duplicated_for_same_content(monkeypatch):
    ui = InlinePromptUI(client=_FakeHumanClientRouletteSpin(), console=Console(record=True))
    ui.state.game = "roulette"
    monkeypatch.setattr("openvegas.tui.prompt_ui.load_config", lambda: {"animation": False})

    round_state_calls = {"count": 0}
    original_print = ui.console.print

    def _capture_print(*args, **kwargs):
        if args and isinstance(args[0], Panel):
            panel = args[0]
            title = panel.title.plain if hasattr(panel.title, "plain") else str(panel.title or "")
            if title == "Round State":
                round_state_calls["count"] += 1
        return original_print(*args, **kwargs)

    monkeypatch.setattr(ui.console, "print", _capture_print)

    out = await ui._run_human_card_round(stake=Decimal("50"))
    assert "Round ID: r1" in out
    assert round_state_calls["count"] == 1


@pytest.mark.asyncio
async def test_force_win_roulette_prompts_for_bet_and_passes_to_demo_autoplay(monkeypatch):
    ui = InlinePromptUI(client=_FakeHumanClientRouletteDemoAutoplay(), console=Console())
    ui.state.game = "roulette"
    monkeypatch.setattr("openvegas.tui.prompt_ui.load_config", lambda: {"animation": False})
    monkeypatch.setattr("openvegas.tui.prompt_ui.Prompt.ask", lambda *_args, **_kwargs: "2")

    out = await ui._run_human_card_round(stake=Decimal("100"), force_win_mode=True)

    assert "Round ID: demo-r1" in out
    assert len(ui.client.demo_calls) == 1
    assert ui.client.demo_calls[0]["preferred_action"] == "bet_black"


@pytest.mark.asyncio
@pytest.mark.parametrize("game_code", ["blackjack", "roulette", "slots", "poker", "baccarat"])
async def test_force_win_card_games_resolve_without_crash(game_code, monkeypatch):
    ui = InlinePromptUI(client=_FakeHumanClientCardDemoAutoplayMatrix(), console=Console())
    ui.state.game = game_code
    monkeypatch.setattr("openvegas.tui.prompt_ui.load_config", lambda: {"animation": False})
    monkeypatch.setattr("openvegas.tui.prompt_ui.Prompt.ask", lambda *_args, **_kwargs: "1")

    out = await ui._run_human_card_round(stake=Decimal("100"), force_win_mode=True)

    assert f"Round ID: demo-{game_code}-1" in out
    assert "LIVE MODE" in out
    assert "Payout: 200.000000 | Net: 100.000000" in out
    assert len(ui.client.demo_calls) == 1


def test_run_uses_shared_result_renderer_for_win(monkeypatch):
    ui = InlinePromptUI(client=_FakeClientPlay(), console=Console(record=True))
    calls: dict = {"n": 0, "is_win": None, "content": None}

    async def _ok_auth():
        return True

    async def _fake_run_once():
        ui._last_is_win = True
        return "win result"

    monkeypatch.setattr(ui, "_ensure_auth", _ok_auth)
    monkeypatch.setattr(ui, "_run_step", lambda *_args, **_kwargs: "confirm")
    monkeypatch.setattr(ui, "run_once", _fake_run_once)
    monkeypatch.setattr("openvegas.tui.prompt_ui.Confirm.ask", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("openvegas.tui.prompt_ui.load_config", lambda: {"animation": True})

    def _fake_render_result(console, content, *, is_win, animation_enabled, title):
        calls["n"] += 1
        calls["is_win"] = is_win
        calls["content"] = content
        _ = (console, animation_enabled, title)

    monkeypatch.setattr("openvegas.tui.prompt_ui.render_result_panel", _fake_render_result)

    ui.run()

    assert calls["n"] == 1
    assert calls["is_win"] is True
    assert calls["content"] == "win result"


def test_run_uses_shared_result_renderer_for_non_win(monkeypatch):
    ui = InlinePromptUI(client=_FakeClientPlay(), console=Console(record=True))
    calls: dict = {"n": 0, "is_win": None}

    async def _ok_auth():
        return True

    async def _fake_run_once():
        ui._last_is_win = False
        return "non win result"

    monkeypatch.setattr(ui, "_ensure_auth", _ok_auth)
    monkeypatch.setattr(ui, "_run_step", lambda *_args, **_kwargs: "confirm")
    monkeypatch.setattr(ui, "run_once", _fake_run_once)
    monkeypatch.setattr("openvegas.tui.prompt_ui.Confirm.ask", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("openvegas.tui.prompt_ui.load_config", lambda: {"animation": True})

    def _fake_render_result(_console, _content, *, is_win, animation_enabled, title):
        calls["n"] += 1
        calls["is_win"] = is_win
        _ = (animation_enabled, title)

    monkeypatch.setattr("openvegas.tui.prompt_ui.render_result_panel", _fake_render_result)

    ui.run()

    assert calls["n"] == 1
    assert calls["is_win"] is False


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
