from __future__ import annotations

import pytest

from server.routes import wallet as wallet_routes


class _FakeDB:
    def __init__(self, projection_rows=None):
        self.last_query = ""
        self.last_args = ()
        self.projection_rows = projection_rows if projection_rows is not None else []

    async def fetch(self, query: str, *args):
        self.last_query = query
        self.last_args = args
        if "FROM wallet_history_projection" in query:
            return self.projection_rows
        return [
            {
                "entry_type": "win",
                "amount": "1.000000",
                "reference_id": "game-1",
                "created_at": "2026-01-01T00:00:00Z",
            }
        ]


@pytest.mark.asyncio
async def test_history_excludes_demo_entries_by_default(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(wallet_routes, "get_db", lambda: db)
    out = await wallet_routes.get_history(user={"user_id": "abc"})
    assert "demo_human_casino_play" in db.last_query
    assert "demo_human_casino_win" in db.last_query
    assert "demo_human_casino_loss" in db.last_query
    assert "debit_account <> 'demo_reserve'" in db.last_query
    assert "credit_account <> 'demo_reserve'" in db.last_query
    assert out["entries"][0]["entry_type"] == "win"


@pytest.mark.asyncio
async def test_history_can_include_demo_entries(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(wallet_routes, "get_db", lambda: db)
    await wallet_routes.get_history(user={"user_id": "abc"}, include_demo=True)
    assert "demo_human_casino_play" not in db.last_query


@pytest.mark.asyncio
async def test_history_uses_projection_when_available(monkeypatch):
    db = _FakeDB(
        projection_rows=[
            {
                "event_type": "ai_usage_charge",
                "display_amount_v": "-0.123456",
                "request_id": "req-1",
                "occurred_at": "2026-01-01T00:00:00Z",
                "display_status": "completed",
                "metadata_json": {"provider": "openai"},
            }
        ]
    )
    monkeypatch.setattr(wallet_routes, "get_db", lambda: db)
    out = await wallet_routes.get_history(user={"user_id": "abc"})
    assert out["entries"][0]["entry_type"] == "ai_usage_charge"
    assert out["entries"][0]["status"] == "completed"
