from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from openvegas.games.base import GameResult
from server.routes import games as games_routes


@pytest.fixture(autouse=True)
def _clear_win_always_aliases(monkeypatch):
    monkeypatch.delenv("OPENVEGAS_WIN_ALWAYS", raising=False)
    monkeypatch.delenv("OPENVEGAS_WIN_ALWAYS_USER_IDS", raising=False)


class _FakeGame:
    async def validate_bet(self, bet: dict) -> bool:
        return True

    async def resolve(self, bet: dict, rng, client_seed: str, nonce: int) -> GameResult:
        # Force search loop to advance before finding a winning nonce.
        payout = Decimal("2") if nonce >= 2 else Decimal("0")
        bet_amount = Decimal(str(bet["amount"]))
        return GameResult(
            game_id=bet["game_id"],
            player_id=bet["player_id"],
            bet_amount=bet_amount,
            payout=payout,
            net=payout - bet_amount,
            outcome_data={"nonce_used": nonce},
            server_seed="server_seed_x",
            server_seed_hash="server_seed_hash_x",
            client_seed=client_seed,
            nonce=nonce,
            provably_fair=True,
        )


class _FakeWallet:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def ensure_escrow_account(self, game_id: str):
        self.calls.append(("ensure_escrow_account", {"game_id": game_id}))

    async def place_bet(self, account_id, amount, game_id, **kwargs):
        self.calls.append(
            (
                "place_bet",
                {
                    "account_id": account_id,
                    "amount": amount,
                    "game_id": game_id,
                    **kwargs,
                },
            )
        )

    async def settle_win(self, account_id, payout, game_id, **kwargs):
        self.calls.append(
            (
                "settle_win",
                {
                    "account_id": account_id,
                    "payout": payout,
                    "game_id": game_id,
                    **kwargs,
                },
            )
        )

    async def settle_loss(self, game_id, amount, **kwargs):
        self.calls.append(
            (
                "settle_loss",
                {
                    "game_id": game_id,
                    "amount": amount,
                    **kwargs,
                },
            )
        )


class _FakeDB:
    def __init__(self):
        self.execute_calls: list[tuple[str, tuple]] = []
        self.verify_row = None

    async def execute(self, query: str, *args):
        self.execute_calls.append((query, args))
        return "OK"

    async def fetchrow(self, query: str, *args):
        return self.verify_row


@pytest.mark.asyncio
async def test_play_demo_rejects_non_admin(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "admin-user")

    req = games_routes.DemoPlayRequest(amount=1, type="win", horse=1)
    with pytest.raises(HTTPException) as e:
        await games_routes.play_game_demo(
            "horse",
            req,
            user={"user_id": "regular-user"},
        )
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_play_demo_allows_local_when_allowlist_empty(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "")
    monkeypatch.setenv("OPENVEGAS_DEMO_ALLOW_LOCAL_OPEN", "1")
    monkeypatch.setitem(games_routes.GAMES, "horse", _FakeGame)
    monkeypatch.setenv("OPENVEGAS_DEMO_MAX_ATTEMPTS", "5")

    wallet = _FakeWallet()
    db = _FakeDB()
    monkeypatch.setattr(games_routes, "get_wallet", lambda: wallet)
    monkeypatch.setattr(games_routes, "get_db", lambda: db)

    req = games_routes.DemoPlayRequest(amount=1, type="win", horse=1)
    out = await games_routes.play_game_demo("horse", req, user={"user_id": "local-user"})
    assert out["demo_mode"] is True
    assert out["canonical"] is False


@pytest.mark.asyncio
async def test_play_demo_rejects_local_when_allowlist_empty_and_local_open_off(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "")
    monkeypatch.setenv("OPENVEGAS_DEMO_ALLOW_LOCAL_OPEN", "0")

    req = games_routes.DemoPlayRequest(amount=1, type="win", horse=1)
    with pytest.raises(HTTPException) as e:
        await games_routes.play_game_demo("horse", req, user={"user_id": "local-user"})
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_play_demo_uses_demo_ledger_entry_types(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "admin-user")
    monkeypatch.setenv("OPENVEGAS_DEMO_MAX_ATTEMPTS", "10")
    monkeypatch.setitem(games_routes.GAMES, "horse", _FakeGame)

    wallet = _FakeWallet()
    db = _FakeDB()
    monkeypatch.setattr(games_routes, "get_wallet", lambda: wallet)
    monkeypatch.setattr(games_routes, "get_db", lambda: db)

    req = games_routes.DemoPlayRequest(amount=1, type="win", horse=1)
    out = await games_routes.play_game_demo("horse", req, user={"user_id": "admin-user"})
    assert out["demo_mode"] is True
    assert out["canonical"] is False
    assert out["provably_fair"] is False
    assert Decimal(out["net"]) == Decimal("1")

    types = [payload.get("entry_type") for name, payload in wallet.calls if name in {"place_bet", "settle_win", "settle_loss"}]
    assert "demo_play" in types
    assert "demo_win" in types

    # game_history insert includes is_demo=True in final arg.
    assert db.execute_calls
    _, args = db.execute_calls[-1]
    assert args[-1] is True


@pytest.mark.asyncio
async def test_verify_rejects_demo_round(monkeypatch):
    db = _FakeDB()
    db.verify_row = {
        "id": "f932a8a1-6f0a-4cb2-816b-3ea2f2607fc4",
        "is_demo": True,
        "server_seed": "x",
        "server_seed_hash": "y",
        "client_seed": "z",
        "nonce": 0,
        "provably_fair": False,
    }
    monkeypatch.setattr(games_routes, "get_db", lambda: db)
    with pytest.raises(HTTPException) as e:
        await games_routes.verify_game("f932a8a1-6f0a-4cb2-816b-3ea2f2607fc4", user={"user_id": "u1"})
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_play_rejects_below_min_wager_for_real_paths(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_MIN_GAME_WAGER_V", "50")
    monkeypatch.setitem(games_routes.GAMES, "skillshot", _FakeGame)

    class _Fraud:
        async def check_bet(self, _user_id):
            return None

    monkeypatch.setattr(games_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(games_routes, "get_wallet", lambda: _FakeWallet())
    monkeypatch.setattr(games_routes, "get_db", lambda: _FakeDB())

    req = games_routes.PlayRequest(amount=1, type="win")
    with pytest.raises(HTTPException) as e:
        await games_routes.play_game("skillshot", req, user={"user_id": "u-real"})
    assert e.value.status_code == 400
    assert "at least" in str(e.value.detail).lower()
