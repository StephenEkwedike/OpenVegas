from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from openvegas.contracts.enums import ConversationMode
from openvegas.contracts.errors import APIErrorCode, ContractError
from server.services.provider_threads import ProviderThreadService, ThreadContext


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

    async def fetchval(self, query: str, *args):
        if "COUNT(*)::int" in query and "FROM provider_thread_messages" in query:
            thread_id = str(args[0])
            return len([row for row in self.db.messages if str(row.get("thread_id")) == thread_id])
        return None

    async def fetch(self, query: str, *args):
        if "FROM provider_thread_messages" not in query:
            return []
        thread_id = str(args[0])
        rows = [row for row in self.db.messages if str(row.get("thread_id")) == thread_id]
        if "ORDER BY created_at ASC" in query:
            rows.sort(
                key=lambda row: (
                    row.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
                    str(row.get("id") or ""),
                ),
            )
        else:
            rows.sort(
                key=lambda row: (
                    row.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
                    str(row.get("id") or ""),
                ),
                reverse=True,
            )
        limit = int(args[1]) if len(args) > 1 else len(rows)
        selected = rows[:limit]
        return [
            {"id": row.get("id"), "role": row.get("role"), "content": row.get("content")}
            for row in selected
        ]

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
            if len(args) >= 3:
                thread_id = str(args[0])
                # append_exchange inserts two rows in one query (user/assistant)
                if "'user'" in query and "'assistant'" in query:
                    self.db.messages.append(
                        {
                            "id": f"u{len(self.db.messages)+1}",
                            "thread_id": thread_id,
                            "role": "user",
                            "content": json_load(args[1]),
                            "created_at": datetime.now(timezone.utc),
                        }
                    )
                    self.db.messages.append(
                        {
                            "id": f"a{len(self.db.messages)+1}",
                            "thread_id": thread_id,
                            "role": "assistant",
                            "content": json_load(args[2]),
                            "created_at": datetime.now(timezone.utc),
                        }
                    )
                else:
                    self.db.messages.append(
                        {
                            "id": f"s{len(self.db.messages)+1}",
                            "thread_id": thread_id,
                            "role": "assistant",
                            "content": json_load(args[1]),
                            "created_at": datetime.now(timezone.utc),
                        }
                    )
            return "INSERT 2"
        if "DELETE FROM provider_thread_messages" in query and "ANY" in query:
            ids = {str(v) for v in list(args[0] or [])}
            self.db.messages = [row for row in self.db.messages if str(row.get("id")) not in ids]
            return "DELETE"
        if "DELETE FROM provider_thread_messages" in query:
            return "DELETE 0"
        return "OK"


class _FakeDB:
    def __init__(self):
        self.threads: dict[str, dict] = {}
        self.message_inserts = 0
        self.messages: list[dict] = []

    @asynccontextmanager
    async def transaction(self):
        yield _FakeTx(self)

    async def fetch(self, query: str, *args):
        if "FROM provider_thread_messages" not in query:
            return []
        thread_id = str(args[0])
        limit = int(args[1])
        rows = [row for row in self.messages if str(row.get("thread_id")) == thread_id]
        rows.sort(
            key=lambda row: (
                row.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
                str(row.get("id") or ""),
            ),
            reverse=True,
        )
        return [
            {"role": row.get("role"), "content": row.get("content")}
            for row in rows[:limit]
        ]


def json_load(value: object) -> object:
    import json

    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


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


@pytest.mark.asyncio
async def test_recent_messages_with_stats_filters_non_replayable_rows(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_CONTEXT_MAX_MESSAGES", "200")
    db = _FakeDB()
    now = datetime.now(timezone.utc)
    db.messages = [
        {"id": "1", "thread_id": "t1", "role": "user", "content": {"text": "hello"}, "created_at": now},
        {"id": "2", "thread_id": "t1", "role": "assistant", "content": {"text": "plain answer"}, "created_at": now},
        {"id": "3", "thread_id": "t1", "role": "assistant", "content": '{"tool_name":"fs_read"}', "created_at": now},
        {"id": "4", "thread_id": "t1", "role": "assistant", "content": "<tool>trace</tool>", "created_at": now},
        {"id": "5", "thread_id": "t1", "role": "assistant", "content": "```json\n{\"x\":1}\n```", "created_at": now},
        {"id": "6", "thread_id": "t1", "role": "assistant", "content": {"tool_name": "fs_read"}, "created_at": now},
        {"id": "7", "thread_id": "t1", "role": "system", "content": {"text": "skip role"}, "created_at": now},
        {"id": "8", "thread_id": "t1", "role": "user", "content": {"text": ""}, "created_at": now},
    ]
    svc = ProviderThreadService(db)

    replay, loaded, skipped = await svc.get_recent_messages_with_stats(thread_id="t1", limit=200)

    assert loaded == 8
    assert replay == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "plain answer"},
    ]
    assert skipped == 6


@pytest.mark.asyncio
async def test_recent_messages_with_stats_hard_enforces_server_cap(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_CONTEXT_MAX_MESSAGES", "20")
    db = _FakeDB()
    now = datetime.now(timezone.utc)
    db.messages = [
        {
            "id": str(i),
            "thread_id": "t1",
            "role": "user",
            "content": {"text": f"m{i}"},
            "created_at": now + timedelta(seconds=i),
        }
        for i in range(25)
    ]
    svc = ProviderThreadService(db)

    replay, loaded, skipped = await svc.get_recent_messages_with_stats(thread_id="t1", limit=999)

    assert loaded == 20
    assert len(replay) == 20
    assert skipped == 0
    assert replay[0]["content"] == "m5"
    assert replay[-1]["content"] == "m24"


@pytest.mark.asyncio
async def test_append_exchange_compacts_old_messages(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_CONTEXT_COMPACTION_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_CONTEXT_COMPACTION_TRIGGER_MESSAGES", "6")
    monkeypatch.setenv("OPENVEGAS_CONTEXT_COMPACTION_KEEP_RECENT_MESSAGES", "3")
    monkeypatch.setenv("OPENVEGAS_CONTEXT_MAX_MESSAGES", "20")

    db = _FakeDB()
    now = datetime.now(timezone.utc)
    for i in range(6):
        db.messages.append(
            {
                "id": f"{i+1}",
                "thread_id": "t1",
                "role": "user" if i % 2 == 0 else "assistant",
                "content": {"text": f"m{i+1}"},
                "created_at": now + timedelta(seconds=i),
            }
        )
    svc = ProviderThreadService(db)
    ctx = ThreadContext(thread_id="t1", thread_status="existing", conversation_mode=ConversationMode.PERSISTENT)
    await svc.append_exchange(
        thread_ctx=ctx,
        prompt="new prompt",
        response_text="new answer",
        input_tokens=10,
        output_tokens=10,
        persist_context=True,
    )

    texts = []
    for row in db.messages:
        content = row.get("content")
        if isinstance(content, dict):
            texts.append(str(content.get("text") or ""))
        else:
            texts.append(str(content or ""))
    assert any(t.startswith("conversation_summary_v1") for t in texts)
