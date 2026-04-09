from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN

import pytest

from openvegas.games.base import GameResult
from openvegas.games.horse_racing import Horse, HorseRacing
from server.routes import games as games_routes


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _IdemRow:
    id: str
    user_id: str
    scope: str
    idempotency_key: str
    payload_hash: str
    response_status: int | None = None
    response_body_text: str | None = None
    response_content_type: str = "application/json"
    resource_id: str | None = None
    created_at: datetime = _now()
    updated_at: datetime = _now()


class _Tx:
    def __init__(self, db: "_FakeDB"):
        self.db = db

    async def __aenter__(self):
        await self.db.lock.acquire()
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        self.db.lock.release()
        return False


class _FakeDB:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.idem: list[_IdemRow] = []
        self.quotes: dict[str, dict] = {}
        self.game_history: dict[str, dict] = {}

    def transaction(self):
        return _Tx(self)

    async def execute(self, query: str, *args):
        q = " ".join(query.split())
        if "INSERT INTO horse_quote_idempotency" in q:
            user_id, scope, idem_key, payload_hash = args
            for row in self.idem:
                if row.user_id == str(user_id) and row.scope == str(scope) and row.idempotency_key == str(idem_key):
                    return "OK"
            self.idem.append(
                _IdemRow(
                    id=str(uuid.uuid4()),
                    user_id=str(user_id),
                    scope=str(scope),
                    idempotency_key=str(idem_key),
                    payload_hash=str(payload_hash),
                )
            )
            return "OK"
        if "UPDATE horse_quote_idempotency" in q:
            status, body, content_type, resource_id, row_id = args
            for row in self.idem:
                if row.id == str(row_id):
                    row.response_status = int(status)
                    row.response_body_text = str(body)
                    row.response_content_type = str(content_type)
                    if resource_id:
                        row.resource_id = str(resource_id)
                    row.updated_at = _now()
                    break
            return "OK"
        if "UPDATE horse_quotes SET consumed_at = now()" in q:
            quote_id, game_id = args
            row = self.quotes[str(quote_id)]
            row["consumed_at"] = _now()
            row["consumed_game_id"] = str(game_id)
            row["updated_at"] = _now()
            return "OK"
        if "INSERT INTO wallet_accounts" in q:
            return "OK"
        if "INSERT INTO game_history" in q:
            game_id = str(args[0])
            self.game_history[game_id] = {"id": game_id}
            return "OK"
        return "OK"

    async def fetchrow(self, query: str, *args):
        q = " ".join(query.split())
        if "SELECT id, payload_hash, response_status, response_body_text, response_content_type" in q:
            user_id, scope, idem_key = args
            for row in self.idem:
                if row.user_id == str(user_id) and row.scope == str(scope) and row.idempotency_key == str(idem_key):
                    return {
                        "id": row.id,
                        "payload_hash": row.payload_hash,
                        "response_status": row.response_status,
                        "response_body_text": row.response_body_text,
                        "response_content_type": row.response_content_type,
                    }
            return None
        if "SELECT COUNT(*) AS c FROM horse_quotes WHERE user_id = $1 AND consumed_at IS NULL AND expires_at > now()" in q:
            user_id = str(args[0])
            count = sum(
                1
                for row in self.quotes.values()
                if row["user_id"] == user_id and row["consumed_at"] is None and row["expires_at"] > _now()
            )
            return {"c": count}
        if "SELECT COUNT(*) AS c FROM horse_quotes WHERE user_id = $1 AND created_at >= now() - interval '1 minute'" in q:
            user_id = str(args[0])
            cutoff = _now() - timedelta(minutes=1)
            count = sum(1 for row in self.quotes.values() if row["user_id"] == user_id and row["created_at"] >= cutoff)
            return {"c": count}
        if "INSERT INTO horse_quotes" in q and "RETURNING id, expires_at" in q:
            user_id, bet_type, budget_v, horses_json, board_hash, ttl = args
            quote_id = str(uuid.uuid4())
            expires_at = _now() + timedelta(seconds=int(ttl))
            self.quotes[quote_id] = {
                "id": quote_id,
                "user_id": str(user_id),
                "bet_type": str(bet_type),
                "budget_v": Decimal(str(budget_v)),
                "horses_json": json.loads(horses_json),
                "board_hash": str(board_hash),
                "expires_at": expires_at,
                "consumed_at": None,
                "consumed_game_id": None,
                "created_at": _now(),
                "updated_at": _now(),
            }
            return {"id": quote_id, "expires_at": expires_at}
        if "SELECT *, (expires_at <= now()) AS is_expired FROM horse_quotes" in q:
            quote_id, user_id = args
            row = self.quotes.get(str(quote_id))
            if not row or row["user_id"] != str(user_id):
                return None
            return {
                **row,
                "is_expired": row["expires_at"] <= _now(),
            }
        if "FROM horse_quote_idempotency" in q and "scope = 'quote_play'" in q and "resource_id = $2::uuid" in q:
            user_id, resource_id, payload_hash = args
            matches = [
                row for row in self.idem
                if row.user_id == str(user_id)
                and row.scope == "quote_play"
                and row.resource_id == str(resource_id)
                and row.payload_hash == str(payload_hash)
                and row.response_status is not None
                and row.response_body_text is not None
            ]
            matches.sort(key=lambda r: r.created_at, reverse=True)
            if not matches:
                return None
            row = matches[0]
            return {
                "response_status": row.response_status,
                "response_body_text": row.response_body_text,
                "response_content_type": row.response_content_type,
            }
        return None


class _FakeWallet:
    def __init__(self):
        self.place_bet_calls = 0
        self.settle_win_calls = 0
        self.settle_loss_calls = 0
        self.place_bet_amounts: list[Decimal] = []
        self.settle_win_amounts: list[Decimal] = []
        self.settle_loss_amounts: list[Decimal] = []

    async def place_bet(self, *args, **kwargs):
        self.place_bet_calls += 1
        if len(args) >= 2:
            self.place_bet_amounts.append(Decimal(str(args[1])))

    async def settle_win(self, *args, **kwargs):
        self.settle_win_calls += 1
        if len(args) >= 2:
            self.settle_win_amounts.append(Decimal(str(args[1])))

    async def settle_loss(self, *args, **kwargs):
        self.settle_loss_calls += 1
        if len(args) >= 2:
            self.settle_loss_amounts.append(Decimal(str(args[1])))


async def _fake_resolve_result(**kwargs):
    bet = kwargs["bet"]
    return GameResult(
        game_id=bet["game_id"],
        player_id=bet["player_id"],
        bet_amount=Decimal(str(bet["amount"])),
        payout=Decimal("0"),
        net=Decimal("0"),
        outcome_data={"finish_order_nums": [int(bet["horse"]), 2, 3]},
        server_seed="server",
        server_seed_hash="hash",
        client_seed="client",
        nonce=0,
        provably_fair=True,
    )


async def _noop_record_game_history(**kwargs):
    _ = kwargs
    return None


@pytest.mark.asyncio
async def test_pricing_rows_derive_from_odds_budget_round_down():
    game = HorseRacing(num_horses=2)
    game.horses = [
        Horse(name="A", number=1, odds=Decimal("3.8")),
        Horse(name="B", number=2, odds=Decimal("2.4")),
    ]
    rows = games_routes._build_horse_pricing_board(game=game, budget_v=Decimal("20"), bet_type="win")
    assert len(rows) == 2
    for row in rows:
        eff = Decimal(str(row["effective_multiplier"]))
        unit = Decimal(str(row["unit_price_v"]))
        max_units = int(row["max_units"])
        debit = Decimal(str(row["debit_v"]))
        payout = Decimal(str(row["payout_if_hit_v"]))
        expected_unit = games_routes._q6_down(Decimal("1") / eff)
        assert unit == expected_unit
        assert max_units == int((Decimal("20") / unit).to_integral_value(rounding=ROUND_DOWN))
        assert debit == games_routes._q6_down(unit * Decimal(max_units))
        assert payout == games_routes._q6_down(Decimal(max_units))
        assert debit <= Decimal("20")


@pytest.mark.asyncio
async def test_concurrent_quote_create_same_key_single_quote_replay(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(games_routes, "get_db", lambda: db)

    req = games_routes.HorseQuoteRequest(bet_type="win", budget_v="20", idempotency_key="same-key")
    user = {"user_id": "11111111-1111-1111-1111-111111111111"}

    r1, r2 = await asyncio.gather(
        games_routes._create_horse_quote(req, user),
        games_routes._create_horse_quote(req, user),
    )
    b1 = json.loads(r1.body_text)
    b2 = json.loads(r2.body_text)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert b1["quote_id"] == b2["quote_id"]
    assert len(db.quotes) == 1


@pytest.mark.asyncio
async def test_same_quote_same_horse_different_keys_concurrent_replays_consumed_path(monkeypatch):
    db = _FakeDB()
    wallet = _FakeWallet()
    monkeypatch.setattr(games_routes, "get_db", lambda: db)
    monkeypatch.setattr(games_routes, "get_wallet", lambda: wallet)
    monkeypatch.setattr(games_routes, "_resolve_result", _fake_resolve_result)
    monkeypatch.setattr(games_routes, "_record_game_history", _noop_record_game_history)

    create_req = games_routes.HorseQuoteRequest(
        bet_type="win",
        budget_v="20",
        idempotency_key="quote-key",
    )
    user = {"user_id": "11111111-1111-1111-1111-111111111111"}
    quote_resp = await games_routes._create_horse_quote(create_req, user)
    quote_id = json.loads(quote_resp.body_text)["quote_id"]

    req1 = games_routes.PlayRequest(quote_id=quote_id, horse=1, idempotency_key="k1")
    req2 = games_routes.PlayRequest(quote_id=quote_id, horse=1, idempotency_key="k2")
    r1, r2 = await asyncio.gather(
        games_routes._play_horse_quote(req=req1, user=user, is_demo=False),
        games_routes._play_horse_quote(req=req2, user=user, is_demo=False),
    )

    b1 = json.loads(r1.body_text)
    b2 = json.loads(r2.body_text)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert b1["game_id"] == b2["game_id"]
    assert wallet.place_bet_calls == 1


@pytest.mark.asyncio
async def test_same_quote_different_horse_concurrent_one_conflicts(monkeypatch):
    db = _FakeDB()
    wallet = _FakeWallet()
    monkeypatch.setattr(games_routes, "get_db", lambda: db)
    monkeypatch.setattr(games_routes, "get_wallet", lambda: wallet)
    monkeypatch.setattr(games_routes, "_resolve_result", _fake_resolve_result)
    monkeypatch.setattr(games_routes, "_record_game_history", _noop_record_game_history)

    create_req = games_routes.HorseQuoteRequest(
        bet_type="win",
        budget_v="20",
        idempotency_key="quote-key",
    )
    user = {"user_id": "11111111-1111-1111-1111-111111111111"}
    quote_resp = await games_routes._create_horse_quote(create_req, user)
    quote_id = json.loads(quote_resp.body_text)["quote_id"]

    req1 = games_routes.PlayRequest(quote_id=quote_id, horse=1, idempotency_key="k1")
    req2 = games_routes.PlayRequest(quote_id=quote_id, horse=2, idempotency_key="k2")
    r1, r2 = await asyncio.gather(
        games_routes._play_horse_quote(req=req1, user=user, is_demo=False),
        games_routes._play_horse_quote(req=req2, user=user, is_demo=False),
    )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 409]
    assert wallet.place_bet_calls == 1


@pytest.mark.asyncio
async def test_quote_play_rejects_tampered_board_hash(monkeypatch):
    db = _FakeDB()
    wallet = _FakeWallet()
    monkeypatch.setattr(games_routes, "get_db", lambda: db)
    monkeypatch.setattr(games_routes, "get_wallet", lambda: wallet)
    monkeypatch.setattr(games_routes, "_resolve_result", _fake_resolve_result)
    monkeypatch.setattr(games_routes, "_record_game_history", _noop_record_game_history)

    create_req = games_routes.HorseQuoteRequest(bet_type="win", budget_v="20", idempotency_key="quote-key")
    user = {"user_id": "11111111-1111-1111-1111-111111111111"}
    quote_resp = await games_routes._create_horse_quote(create_req, user)
    quote_id = json.loads(quote_resp.body_text)["quote_id"]

    db.quotes[str(quote_id)]["board_hash"] = "tampered"

    req = games_routes.PlayRequest(quote_id=quote_id, horse=1, idempotency_key="play-key")
    out = await games_routes._play_horse_quote(req=req, user=user, is_demo=False)
    body = json.loads(out.body_text)
    assert out.status_code == 409
    assert body["error"] == "quote_integrity_error"
    assert wallet.place_bet_calls == 0


@pytest.mark.asyncio
async def test_quote_play_uses_quote_snapshot_for_render_and_settlement(monkeypatch):
    db = _FakeDB()
    wallet = _FakeWallet()
    monkeypatch.setattr(games_routes, "get_db", lambda: db)
    monkeypatch.setattr(games_routes, "get_wallet", lambda: wallet)
    monkeypatch.setattr(games_routes, "_record_game_history", _noop_record_game_history)

    async def _resolve_with_conflicting_runtime_odds(**kwargs):
        bet = kwargs["bet"]
        return GameResult(
            game_id=bet["game_id"],
            player_id=bet["player_id"],
            bet_amount=Decimal(str(bet["amount"])),
            payout=Decimal("0"),
            net=Decimal("0"),
            outcome_data={
                "finish_order_nums": [int(bet["horse"]), 2, 3],
                "horses": [
                    {"number": 1, "name": "A", "odds": "999.0"},
                    {"number": 2, "name": "B", "odds": "999.0"},
                ],
            },
            server_seed="server",
            server_seed_hash="hash",
            client_seed="client",
            nonce=0,
            provably_fair=True,
        )

    monkeypatch.setattr(games_routes, "_resolve_result", _resolve_with_conflicting_runtime_odds)

    create_req = games_routes.HorseQuoteRequest(bet_type="win", budget_v="20", idempotency_key="quote-key")
    user = {"user_id": "11111111-1111-1111-1111-111111111111"}
    quote_resp = await games_routes._create_horse_quote(create_req, user)
    quote_payload = json.loads(quote_resp.body_text)
    quote_id = quote_payload["quote_id"]

    selected = next(h for h in quote_payload["horses"] if int(h["number"]) == 1)

    req = games_routes.PlayRequest(quote_id=quote_id, horse=1, idempotency_key="play-key")
    out = await games_routes._play_horse_quote(req=req, user=user, is_demo=False)
    payload = json.loads(out.body_text)
    assert out.status_code == 200

    outcome = payload["outcome_data"]
    assert outcome["selected_horse_odds"] == str(selected["odds"])
    assert outcome["debit_v"] == str(selected["debit_v"])
    assert outcome["payout_if_hit_v"] == str(selected["payout_if_hit_v"])
    assert outcome["settlement_debit_v"] == str(selected["debit_v"])
    assert outcome["settlement_payout_if_hit_v"] == str(selected["payout_if_hit_v"])
    assert all(str(h.get("odds")) != "999.0" for h in outcome.get("horses", []))

    assert wallet.place_bet_calls == 1
    assert wallet.place_bet_amounts[0] == Decimal(str(selected["debit_v"]))
