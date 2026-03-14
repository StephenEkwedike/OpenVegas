from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from openvegas.contracts.enums import ConversationMode
from openvegas.contracts.errors import APIErrorCode, ContractError
from server.services.provider_threads import ProviderThreadService


class _FakeTx:
    def __init__(self, db: "_FakeDB"):
        self.db = db

    async def fetchrow(self, query: str, *args):
        if "FROM provider_threads" in query and "FOR UPDATE" in query:
            thread_id, user_id = args
            row = self.db.threads.get(str(thread_id))
            if not row or row["user_id"] != str(user_id):
                return None
            return dict(row)
        return None

    async def execute(self, query: str, *args):
        if "INSERT INTO provider_threads" in query:
            thread_id = str(args[0])
            user_id = str(args[1])
            provider = str(args[2])
            model_id = str(args[3])
            forked = None
            if "thread_forked_from" in query:
                forked = str(args[4])
            self.db.threads[thread_id] = {
                "id": thread_id,
                "user_id": user_id,
                "provider": provider,
                "model_id": model_id,
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=72),
                "thread_forked_from": forked,
            }
            return "INSERT 1"
        if "UPDATE provider_threads" in query:
            thread_id = str(args[0])
            if thread_id in self.db.threads:
                self.db.threads[thread_id]["model_id"] = str(args[1])
            return "UPDATE 1"
        if "INSERT INTO provider_thread_messages" in query:
            self.db.message_inserts += 1
            return "INSERT 2"
        if "DELETE FROM provider_thread_messages" in query:
            return "DELETE 0"
        return "OK"


class _FakeDB:
    def __init__(self):
        self.threads: dict[str, dict] = {}
        self.message_inserts = 0

    @asynccontextmanager
    async def transaction(self):
        yield _FakeTx(self)


@pytest.mark.asyncio
async def test_thread_service_disabled_mode_returns_non_persistent(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "0")
    svc = ProviderThreadService(_FakeDB())

    ctx = await svc.prepare_thread(
        user_id="u1",
        provider="openai",
        model_id="gpt-5",
        thread_id=None,
        conversation_mode="persistent",
    )

    assert ctx.thread_id is None
    assert ctx.thread_status == "disabled"


@pytest.mark.asyncio
async def test_thread_service_provider_mismatch_rejected(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    db = _FakeDB()
    db.threads["t1"] = {
        "id": "t1",
        "user_id": "u1",
        "provider": "openai",
        "model_id": "gpt-5",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    svc = ProviderThreadService(db)

    with pytest.raises(ContractError) as exc:
        await svc.prepare_thread(
            user_id="u1",
            provider="anthropic",
            model_id="claude-sonnet",
            thread_id="t1",
            conversation_mode="persistent",
        )

    assert exc.value.code == APIErrorCode.PROVIDER_THREAD_MISMATCH


@pytest.mark.asyncio
async def test_ephemeral_mode_never_persists_messages(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    db = _FakeDB()
    svc = ProviderThreadService(db)

    ctx = await svc.prepare_thread(
        user_id="u1",
        provider="openai",
        model_id="gpt-5",
        thread_id=None,
        conversation_mode=ConversationMode.EPHEMERAL.value,
    )
    assert ctx.conversation_mode == ConversationMode.EPHEMERAL
    assert ctx.thread_id is None

    await svc.append_exchange(
        thread_ctx=ctx,
        prompt="hello",
        response_text="world",
        input_tokens=1,
        output_tokens=1,
        persist_context=True,
    )
    assert db.message_inserts == 0
