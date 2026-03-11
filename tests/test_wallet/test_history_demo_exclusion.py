from __future__ import annotations

import pytest

from server.routes import wallet as wallet_routes


class _FakeDB:
    def __init__(self):
        self.last_query = ""
        self.last_args = ()

    async def fetch(self, query: str, *args):
        self.last_query = query
        self.last_args = args
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
    assert "entry_type NOT IN ('demo_play', 'demo_win', 'demo_loss')" in db.last_query
    assert out["entries"][0]["entry_type"] == "win"


@pytest.mark.asyncio
async def test_history_can_include_demo_entries(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(wallet_routes, "get_db", lambda: db)
    await wallet_routes.get_history(user={"user_id": "abc"}, include_demo=True)
    assert "entry_type NOT IN ('demo_play', 'demo_win', 'demo_loss')" not in db.last_query
