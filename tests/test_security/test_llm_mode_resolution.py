from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from openvegas.contracts.enums import EffectiveReason
from server.services.llm_mode import LLMModeService


class _FakeTx:
    def __init__(self, db: "_FakeDB"):
        self.db = db

    async def fetchrow(self, query: str, *args):
        return await self.db.fetchrow(query, *args)

    async def execute(self, query: str, *args):
        return await self.db.execute(query, *args)


class _FakeDB:
    def __init__(self):
        self.prefs: dict[str, dict] = {}
        self.org_policies: dict[str, dict] = {}

    @asynccontextmanager
    async def transaction(self):
        yield _FakeTx(self)

    async def fetchrow(self, query: str, *args):
        if "FROM user_runtime_prefs" in query:
            row = self.prefs.get(str(args[0]))
            return dict(row) if row else None
        if "FROM org_runtime_policies" in query:
            row = self.org_policies.get(str(args[0]))
            return dict(row) if row else None
        return None

    async def execute(self, query: str, *args):
        if "INSERT INTO user_runtime_prefs" in query:
            user_id, llm_mode, conversation_mode = args
            self.prefs[str(user_id)] = {
                "llm_mode": str(llm_mode),
                "conversation_mode": str(conversation_mode),
            }
        return "OK"


@pytest.mark.asyncio
async def test_mode_resolution_missing_pref_defaults_wrapper(monkeypatch):
    monkeypatch.delenv("OPENVEGAS_BYOK_ENABLED", raising=False)
    svc = LLMModeService(_FakeDB())

    res = await svc.resolve_for_user(user_id="u1")

    assert res.user_pref_mode == "wrapper"
    assert res.effective_mode == "wrapper"
    assert res.effective_reason == EffectiveReason.USER_PREF_MISSING


@pytest.mark.asyncio
async def test_mode_resolution_invalid_pref_falls_back_and_persists(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_BYOK_ENABLED", "1")
    db = _FakeDB()
    svc = LLMModeService(db)

    res = await svc.resolve_for_user(user_id="u1", requested_mode="wat")

    assert res.user_pref_mode == "wrapper"
    assert res.effective_mode == "wrapper"
    assert res.effective_reason == EffectiveReason.INVALID_USER_PREF_FALLBACK
    assert db.prefs["u1"]["llm_mode"] == "wrapper"


@pytest.mark.asyncio
async def test_mode_resolution_global_byok_disabled_returns_forced_wrapper(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_BYOK_ENABLED", "0")
    db = _FakeDB()
    svc = LLMModeService(db)

    res = await svc.resolve_for_user(user_id="u1", requested_mode="byok")

    assert res.user_pref_mode == "byok"
    assert res.effective_mode == "wrapper"
    assert res.effective_reason == EffectiveReason.GLOBAL_BYOK_DISABLED


@pytest.mark.asyncio
async def test_mode_resolution_org_wrapper_required_forces_wrapper(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_BYOK_ENABLED", "1")
    db = _FakeDB()
    db.org_policies["org-1"] = {"wrapper_required": True, "byok_allowed": False}
    svc = LLMModeService(db)

    res = await svc.resolve_for_user(user_id="u1", requested_mode="byok", org_id="org-1")

    assert res.user_pref_mode == "byok"
    assert res.effective_mode == "wrapper"
    assert res.effective_reason == EffectiveReason.ORG_POLICY_WRAPPER_REQUIRED


@pytest.mark.asyncio
async def test_mode_resolution_byok_applies_when_enabled_and_allowed(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_BYOK_ENABLED", "1")
    db = _FakeDB()
    db.org_policies["org-1"] = {"wrapper_required": False, "byok_allowed": True}
    svc = LLMModeService(db)

    res = await svc.resolve_for_user(user_id="u1", requested_mode="byok", org_id="org-1")

    assert res.user_pref_mode == "byok"
    assert res.effective_mode == "byok"
    assert res.effective_reason == EffectiveReason.USER_PREF_APPLIED
