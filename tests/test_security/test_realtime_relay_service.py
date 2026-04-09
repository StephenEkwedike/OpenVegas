from __future__ import annotations

import pytest

from server.services.realtime_relay import RealtimeRelayService


@pytest.mark.asyncio
async def test_get_session_prunes_expired_active_session(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENVEGAS_REALTIME_SESSION_TTL_SEC", "60")
    monkeypatch.setenv("OPENVEGAS_REALTIME_CLOSED_SESSION_TTL_SEC", "10")
    svc = RealtimeRelayService()

    row = await svc.create_session(user_id="u-1", provider="openai", model="gpt-4o-realtime-preview", voice="alloy")
    row.updated_mono -= 120.0

    loaded = await svc.get_session(relay_id=row.id, user_id="u-1")
    assert loaded is None


@pytest.mark.asyncio
async def test_closed_sessions_prune_on_short_closed_ttl(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENVEGAS_REALTIME_SESSION_TTL_SEC", "600")
    monkeypatch.setenv("OPENVEGAS_REALTIME_CLOSED_SESSION_TTL_SEC", "5")
    svc = RealtimeRelayService()

    row = await svc.create_session(user_id="u-1", provider="openai", model="gpt-4o-realtime-preview", voice="alloy")
    await svc.close(relay_id=row.id, status="closed")
    row.updated_mono -= 10.0

    stats = await svc.stats()
    assert stats["total"] == 0
    assert stats["pruned"] >= 1


@pytest.mark.asyncio
async def test_create_session_enforces_per_user_cap(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENVEGAS_REALTIME_MAX_SESSIONS_PER_USER", "2")
    monkeypatch.setenv("OPENVEGAS_REALTIME_SESSION_TTL_SEC", "600")
    monkeypatch.setenv("OPENVEGAS_REALTIME_CLOSED_SESSION_TTL_SEC", "120")
    svc = RealtimeRelayService()

    first = await svc.create_session(user_id="u-1", provider="openai", model="gpt-4o-realtime-preview", voice="alloy")
    second = await svc.create_session(user_id="u-1", provider="openai", model="gpt-4o-realtime-preview", voice="alloy")
    third = await svc.create_session(user_id="u-1", provider="openai", model="gpt-4o-realtime-preview", voice="alloy")

    # Cap is 2, so oldest should be evicted.
    assert await svc.get_session(relay_id=first.id, user_id="u-1") is None
    assert await svc.get_session(relay_id=second.id, user_id="u-1") is not None
    assert await svc.get_session(relay_id=third.id, user_id="u-1") is not None
