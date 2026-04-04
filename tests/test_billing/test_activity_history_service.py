from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from openvegas.payments.service import BillingService


class _DummyWallet:
    async def get_balance(self, account_id: str) -> Decimal:
        _ = account_id
        return Decimal("0")


class _FakeDB:
    def __init__(self, *, topups: list[dict], human: list[dict], legacy: list[dict]):
        self.topups = topups
        self.human = human
        self.legacy = legacy
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        self.fetch_calls.append((query, args))
        if "FROM fiat_topups" in query:
            return self.topups
        if "FROM human_casino_rounds" in query:
            return self.human
        if "FROM game_history" in query:
            return self.legacy
        return []


def _svc(db: _FakeDB) -> BillingService:
    return BillingService(db=db, wallet=_DummyWallet(), stripe_gateway=type("_G", (), {"mode": "stripe"})())


@pytest.mark.asyncio
async def test_list_activity_history_merges_filters_sorts_and_limits():
    now = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)
    db = _FakeDB(
        topups=[
            {
                "id": "top-paid",
                "status": "paid",
                "amount_usd": Decimal("11.00"),
                "v_credit": Decimal("1100.000000"),
                "updated_at": now - timedelta(minutes=10),
                "created_at": now - timedelta(minutes=11),
            },
            {
                "id": "top-failed",
                "status": "failed",
                "amount_usd": Decimal("20.00"),
                "v_credit": Decimal("2000.000000"),
                "updated_at": now - timedelta(minutes=3),
                "created_at": now - timedelta(minutes=4),
            },
        ],
        human=[
            {
                "round_id": "human-win",
                "game_code": "roulette",
                "net_v": Decimal("25.500000"),
                "ts": now - timedelta(minutes=2),
            },
            {
                "round_id": "human-push",
                "game_code": "slots",
                "net_v": Decimal("0.000000"),
                "ts": now - timedelta(minutes=1),
            },
        ],
        legacy=[
            {
                "id": "legacy-loss",
                "game_type": "blackjack",
                "net_v": Decimal("-50.000000"),
                "created_at": now - timedelta(minutes=5),
            },
            {
                "id": "legacy-push",
                "game_type": "poker",
                "net_v": Decimal("0.000000"),
                "created_at": now - timedelta(minutes=6),
            },
        ],
    )
    out = await _svc(db).list_activity_history(user_id="u1", limit=3)

    assert out["conversion"]["v_per_usd"] == "100.000000"
    assert out["conversion"]["usd_per_v"] == "0.01"

    entries = out["entries"]
    assert len(entries) == 3
    assert [e["reference_id"] for e in entries] == ["human-win", "top-failed", "legacy-loss"]
    assert [e["status"] for e in entries] == ["won", "failed", "lost"]
    assert [e["type"] for e in entries] == ["gameplay", "top_up", "gameplay"]
    assert entries[0]["amount_v"] == "25.500000"
    assert entries[0]["amount_usd"] is None
    assert entries[1]["amount_usd"] == "20.00"
    assert entries[2]["game_code"] == "blackjack"


@pytest.mark.asyncio
async def test_list_activity_history_queries_only_paid_and_failed_topups():
    db = _FakeDB(topups=[], human=[], legacy=[])
    await _svc(db).list_activity_history(user_id="u2", limit=50)
    topup_query = next(q for q, _ in db.fetch_calls if "FROM fiat_topups" in q)
    assert "status IN ('paid', 'failed')" in topup_query


@pytest.mark.asyncio
async def test_list_activity_history_includes_demo_game_history_rows():
    now = datetime(2026, 4, 4, 0, 52, 0, tzinfo=timezone.utc)
    db = _FakeDB(
        topups=[],
        human=[],
        legacy=[
            {
                "id": "demo-horse-win",
                "game_type": "horse",
                "net_v": Decimal("1180.000740"),
                "created_at": now,
                "is_demo": True,
            }
        ],
    )

    out = await _svc(db).list_activity_history(user_id="u3", limit=10)

    assert len(out["entries"]) == 1
    entry = out["entries"][0]
    assert entry["reference_id"] == "demo-horse-win"
    assert entry["type"] == "gameplay"
    assert entry["status"] == "won"
    assert entry["amount_v"] == "1180.000740"
    assert entry["source"] == "legacy_game_demo"


@pytest.mark.asyncio
async def test_list_activity_history_includes_all_human_casino_game_codes():
    now = datetime(2026, 4, 4, 1, 0, 0, tzinfo=timezone.utc)
    db = _FakeDB(
        topups=[],
        human=[
            {"round_id": "r-bj", "game_code": "blackjack", "net_v": Decimal("10.000000"), "ts": now - timedelta(minutes=1)},
            {"round_id": "r-rou", "game_code": "roulette", "net_v": Decimal("-10.000000"), "ts": now - timedelta(minutes=2)},
            {"round_id": "r-slot", "game_code": "slots", "net_v": Decimal("9.000000"), "ts": now - timedelta(minutes=3)},
            {"round_id": "r-pok", "game_code": "poker", "net_v": Decimal("15.000000"), "ts": now - timedelta(minutes=4)},
            {"round_id": "r-bac", "game_code": "baccarat", "net_v": Decimal("-5.000000"), "ts": now - timedelta(minutes=5)},
        ],
        legacy=[],
    )

    out = await _svc(db).list_activity_history(user_id="u4", limit=20)

    entries = out["entries"]
    assert len(entries) == 5
    assert {e["game_code"] for e in entries} == {"blackjack", "roulette", "slots", "poker", "baccarat"}
    assert all(e["type"] == "gameplay" for e in entries)
    assert all(e["source"] == "human_casino" for e in entries)


@pytest.mark.asyncio
async def test_list_activity_history_includes_live_and_demo_legacy_games():
    now = datetime(2026, 4, 4, 1, 10, 0, tzinfo=timezone.utc)
    db = _FakeDB(
        topups=[],
        human=[],
        legacy=[
            {
                "id": "legacy-horse-demo",
                "game_type": "horse",
                "net_v": Decimal("560.000100"),
                "created_at": now,
                "is_demo": True,
            },
            {
                "id": "legacy-skillshot-live",
                "game_type": "skillshot",
                "net_v": Decimal("-1.000000"),
                "created_at": now - timedelta(minutes=1),
                "is_demo": False,
            },
        ],
    )

    out = await _svc(db).list_activity_history(user_id="u5", limit=20)
    entries = out["entries"]
    assert len(entries) == 2
    by_ref = {e["reference_id"]: e for e in entries}
    assert by_ref["legacy-horse-demo"]["source"] == "legacy_game_demo"
    assert by_ref["legacy-horse-demo"]["status"] == "won"
    assert by_ref["legacy-skillshot-live"]["source"] == "legacy_game"
    assert by_ref["legacy-skillshot-live"]["status"] == "lost"
