from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openvegas.contracts.errors import ContractError
from openvegas.gateway.catalog import ProviderCatalog
from openvegas.gateway.conversation import ContinuityError
from openvegas.gateway.inference import InferenceResult
from server.services.provider_threads import ProviderThreadService

USER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
KEY = "33333333-3333-4333-8333-333333333333"


class Tx:
    def __init__(self, db):
        self.db = db
        self.acquired = []

    async def fetchval(self, sql, *args):
        assert "pg_try_advisory_xact_lock" in sql
        if args[0] in self.db.locks:
            return False
        self.db.locks.add(args[0])
        self.acquired.append(args[0])
        return True

    async def fetchrow(self, sql, *args):
        if "provider_catalog" in sql:
            return self.db.models.get(tuple(args))
        if "provider_credentials" in sql:
            return {"key_alias": "CANONICAL_TEST_KEY"} if self.db.credentials else None
        if "provider_threads" in sql:
            assert "user_id = $2::uuid" in sql and "FOR UPDATE" in sql
            row = self.db.threads.get(args[0])
            return copy.deepcopy(row) if row and row["user_id"] == args[1] else None
        if "provider_thread_messages" in sql:
            records = self.db.messages.get(args[0], [])
            return next((r for r in records if r["role"] == "system"), None)
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        assert "provider_thread_messages" in sql and "LIMIT 2" in sql
        return copy.deepcopy(self.db.messages.get(args[0], [])[:2])

    async def execute(self, sql, *args):
        self.db.writes.append((sql, args))
        if self.db.fail_insert and "provider_thread_messages" in sql and "INSERT" in sql:
            raise RuntimeError("injected storage failure")
        if "INSERT INTO provider_threads" in sql:
            thread, user, provider, model, source, _ = args
            self.db.threads[thread] = {
                "id": thread,
                "user_id": user,
                "provider": provider,
                "model_id": model,
                "thread_forked_from": source,
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
            }
        elif "INSERT INTO provider_thread_messages" in sql:
            thread, payload = args
            self.db.messages[thread] = [
                {"id": thread, "role": "system", "content": json.loads(payload)}
            ]
        elif "UPDATE provider_thread_messages" in sql:
            record_id, payload, thread = args
            assert self.db.messages[thread][0]["id"] == record_id
            self.db.messages[thread][0]["content"] = json.loads(payload)
        elif "UPDATE provider_threads" in sql:
            assert "user_id = $2::uuid" in sql
        else:
            raise AssertionError(sql)


class DB:
    def __init__(self):
        self.threads, self.messages, self.models = {}, {}, {}
        self.locks, self.writes = set(), []
        self.credentials = True
        self.fail_insert = False
        for provider in ("openai", "anthropic", "mistral", "gemini"):
            self.models[(provider, "reviewed-test")] = {
                "provider": provider,
                "model_id": "reviewed-test",
                "enabled": True,
                "max_tokens": 1024,
                "cost_input_per_1m": "1",
                "cost_output_per_1m": "1",
                "v_price_input_per_1m": "1",
                "v_price_output_per_1m": "1",
            }

    @asynccontextmanager
    async def transaction(self):
        tx = Tx(self)
        before = copy.deepcopy((self.threads, self.messages))
        try:
            yield tx
        except BaseException:
            # Advisory-only transactions have no writes to roll back.
            if not tx.acquired:
                self.threads, self.messages = before
            raise
        finally:
            for lock in tx.acquired:
                self.locks.remove(lock)

    async def fetchrow(self, sql, *args):
        return await Tx(self).fetchrow(sql, *args)


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", "production")
    monkeypatch.setenv("CANONICAL_TEST_KEY", "synthetic-no-provider-network")
    now = datetime.now(UTC)
    review = {
        "reviewed_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "context_window_tokens": 200000,
        "account_access": True,
        "completion_chat": True,
    }
    monkeypatch.setenv(
        "OPENVEGAS_MODEL_REVIEWS_JSON",
        json.dumps(
            {
                f"{provider}:reviewed-test": review
                for provider in ("openai", "anthropic", "mistral", "gemini")
            }
        ),
    )
    db = DB()
    return db, ProviderThreadService(db), ProviderCatalog(db)


async def create(setup):
    _db, service, catalog = setup
    plan = await service.create_canonical_thread(
        user_id=USER,
        provider="openai",
        model_id="reviewed-test",
        catalog=catalog,
    )
    return plan


async def infer(setup, created, **changes):
    _db, service, catalog = setup
    gateway = changes.pop(
        "gateway", SimpleNamespace(infer=AsyncMock(return_value=InferenceResult("Answer", 3, 2, completion_status="complete")))
    )
    args = {
        "user_id": USER,
        "thread_id": created.thread_id,
        "provider": "openai",
        "model_id": "reviewed-test",
        "expected_revision": created.revision,
        "prompt": "Hello",
        "idempotency_key": KEY,
        "catalog": catalog,
        "gateway": gateway,
    }
    args.update(changes)
    result = await service.infer_canonical(**args)
    return result, gateway


@pytest.mark.asyncio
async def test_create_infer_fork_continue_all_roles_and_original_unchanged(setup):
    db, service, catalog = setup
    created = await create(setup)
    first, gateway = await infer(setup, created)
    gateway.infer.assert_awaited_once()
    request = gateway.infer.call_args.args[0]
    assert request.messages == [{"role": "user", "content": "Hello"}]
    assert request.account_id == f"user:{USER}" and not request.enable_tools
    assert not request.enable_web_search
    for provider in ("anthropic", "mistral", "openai", "gemini"):
        original = copy.deepcopy(db.messages)
        kwargs = {
            "user_id": USER,
            "thread_id": created.thread_id,
            "provider": provider,
            "model_id": "reviewed-test",
            "catalog": catalog,
        }
        before = len(db.writes)
        preflight = await service.canonical_switch(**kwargs)
        assert preflight.revision == first["revision"]
        assert not preflight.messages and not preflight.context_transferred
        assert len(db.writes) == before
        with pytest.raises(ContinuityError, match="History changed"):
            await service.canonical_switch(
                **kwargs, commit=True, expected_revision=created.revision
            )
        committed = await service.canonical_switch(
            **kwargs, commit=True, expected_revision=preflight.revision
        )
        assert committed.context_transferred and committed.thread_id != created.thread_id
        assert db.messages[created.thread_id] == original[created.thread_id]
        answer, next_gateway = await infer(setup, committed, provider=provider, prompt="Next")
        next_request = next_gateway.infer.call_args.args[0]
        assert [m["role"] for m in next_request.messages] == ["user", "assistant", "user"]
        assert [m["content"] for m in next_request.messages] == ["Hello", "Answer", "Next"]
        assert answer["revision"] != committed.revision
        assert not any("wallet" in sql or "inference_usage" in sql for sql, _ in db.writes)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "switch", "append", "infer"])
async def test_every_entrypoint_enforces_tenant_scope(setup, operation):
    db, service, catalog = setup
    created = await create(setup)
    before = copy.deepcopy(db.messages)
    with pytest.raises(ContractError):
        if operation == "read":
            await service.canonical_history(
                user_id=OTHER,
                thread_id=created.thread_id,
                provider="openai",
                model_id="reviewed-test",
            )
        elif operation == "switch":
            await service.canonical_switch(
                user_id=OTHER,
                thread_id=created.thread_id,
                provider="mistral",
                model_id="reviewed-test",
                catalog=catalog,
            )
        elif operation == "append":
            await service.append_canonical_exchange(
                user_id=OTHER,
                thread_id=created.thread_id,
                expected_revision=created.revision,
                prompt="hi",
                response_text="answer",
            )
        else:
            await infer(setup, created, user_id=OTHER)
    assert db.messages == before and not db.locks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "disabled",
        "credential",
        "context",
        "access",
        "gemini_unreviewed",
        "expiry",
        "legacy",
        "version",
        "extra_row",
        "secret",
    ],
)
async def test_switch_failures_leave_source_and_billing_untouched(setup, monkeypatch, failure):
    db, service, catalog = setup
    created = await create(setup)
    provider = "mistral"
    if failure == "disabled":
        db.models[(provider, "reviewed-test")]["enabled"] = False
    elif failure == "credential":
        db.credentials = False
    elif failure in {"context", "access"}:
        monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    elif failure == "gemini_unreviewed":
        provider = "gemini"
        monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    elif failure == "expiry":
        db.threads[created.thread_id]["expires_at"] = datetime.now(UTC) - timedelta(hours=1)
    elif failure == "legacy":
        db.messages[created.thread_id] = [{"role": "user", "content": {"text": "older"}}]
    elif failure == "version":
        db.messages[created.thread_id][0]["content"]["kind"] = "unknown"
    elif failure == "extra_row":
        db.messages[created.thread_id].append({"role": "tool", "content": "untrusted"})
    elif failure == "secret":
        db.messages[created.thread_id][0]["content"]["messages"] = [
            {"role": "user", "content": "sk-" + "a" * 30},
            {"role": "assistant", "content": "answer"},
        ]
    before, writes = copy.deepcopy((db.threads, db.messages)), len(db.writes)
    with pytest.raises((ContinuityError, ContractError)):
        await service.canonical_switch(
            user_id=USER,
            thread_id=created.thread_id,
            provider=provider,
            model_id="reviewed-test",
            catalog=catalog,
            commit=True,
            expected_revision=created.revision,
        )
    assert (db.threads, db.messages) == before and len(db.writes) == writes


@pytest.mark.asyncio
async def test_insert_failure_rolls_back_fork(setup):
    db, service, catalog = setup
    created = await create(setup)
    before = copy.deepcopy((db.threads, db.messages))
    db.fail_insert = True
    with pytest.raises(RuntimeError, match="injected"):
        await service.canonical_switch(
            user_id=USER,
            thread_id=created.thread_id,
            provider="mistral",
            model_id="reviewed-test",
            catalog=catalog,
            commit=True,
            expected_revision=created.revision,
        )
    assert (db.threads, db.messages) == before and not db.locks


@pytest.mark.asyncio
async def test_active_generation_excludes_switch_and_other_turn_then_cancels(setup):
    db, service, catalog = setup
    created = await create(setup)
    started = asyncio.Event()

    async def pending(_request):
        started.set()
        await asyncio.Event().wait()

    gateway = SimpleNamespace(infer=AsyncMock(side_effect=pending))
    task = asyncio.create_task(infer(setup, created, gateway=gateway))
    await asyncio.wait_for(started.wait(), 1)
    with pytest.raises(ContinuityError, match="possibly billed"):
        await service.canonical_switch(
            user_id=USER,
            thread_id=created.thread_id,
            provider="mistral",
            model_id="reviewed-test",
            catalog=catalog,
        )
    second_gateway = SimpleNamespace(infer=AsyncMock())
    with pytest.raises(ContinuityError, match="possibly billed"):
        await infer(setup, created, gateway=second_gateway)
    second_gateway.infer.assert_not_awaited()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not db.locks
    with pytest.raises(ContinuityError, match="possibly billed"):
        await service.canonical_history(
            user_id=USER, thread_id=created.thread_id, provider="openai", model_id="reviewed-test"
        )
    assert db.messages[created.thread_id][0]["content"]["messages"] == []


@pytest.mark.asyncio
async def test_duplicate_completion_and_preflight_revision_race_no_second_charge(setup):
    _db, service, catalog = setup
    created = await create(setup)
    args = {
        "user_id": USER,
        "thread_id": created.thread_id,
        "provider": "mistral",
        "model_id": "reviewed-test",
        "catalog": catalog,
    }
    plan = await service.canonical_switch(**args)
    await infer(setup, created)
    gateway = SimpleNamespace(infer=AsyncMock())
    with pytest.raises(ContinuityError, match="already completed"):
        await infer(setup, created, gateway=gateway)
    gateway.infer.assert_not_awaited()
    with pytest.raises(ContinuityError, match="History changed"):
        await service.canonical_switch(**args, commit=True, expected_revision=plan.revision)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["sk-" + "a" * 30, pytest.param("x" * 64001, id="oversized-prompt"), "<tool>untrusted</tool>"])
async def test_bad_prompt_rejected_before_gateway(setup, bad):
    created = await create(setup)
    gateway = SimpleNamespace(infer=AsyncMock())
    with pytest.raises(ContinuityError):
        await infer(setup, created, gateway=gateway, prompt=bad)
    gateway.infer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        InferenceResult("sk-" + "a" * 30, 1, 1),
        InferenceResult("x" * 64001, 1, 1),
        InferenceResult("tool", 1, 1, tool_calls=[{"name": "bad"}]),
    ],
)
async def test_unsafe_paid_response_never_retried_and_blocks_future_continuity(setup, bad):
    db, service, catalog = setup
    created = await create(setup)
    gateway = SimpleNamespace(infer=AsyncMock(return_value=bad))
    answer, _ = await infer(setup, created, gateway=gateway)
    gateway.infer.assert_awaited_once()
    assert answer["continuity_blocked"] and answer["revision"] is None and answer["warnings"]
    assert bad.text not in json.dumps(db.messages)
    with pytest.raises(ContinuityError):
        await service.canonical_switch(
            user_id=USER,
            thread_id=created.thread_id,
            provider="mistral",
            model_id="reviewed-test",
            catalog=catalog,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["OPENVEGAS_CONTEXT_ENABLED", "OPENVEGAS_MODEL_SWITCH_ENABLED"])
async def test_operator_switch_off_prevents_creation(setup, monkeypatch, flag):
    monkeypatch.setenv(flag, "0")
    with pytest.raises(ContinuityError):
        await create(setup)
    assert not setup[0].writes


@pytest.mark.asyncio
async def test_legacy_endpoint_cannot_prune_or_silently_lose_canonical_history(setup):
    db, service, _catalog = setup
    created = await create(setup)
    before = copy.deepcopy(db.threads)
    with pytest.raises(ContractError, match="Canonical threads require"):
        await service.prepare_thread(
            user_id=USER,
            provider="openai",
            model_id="other",
            thread_id=created.thread_id,
            conversation_mode="persistent",
        )
    assert db.threads == before


@pytest.mark.asyncio
async def test_write_failure_after_paid_result_blocks_retry_and_preserves_safe_snapshot(
    setup, monkeypatch
):
    db, service, _ = setup
    created = await create(setup)
    monkeypatch.setattr(
        service,
        "append_canonical_exchange",
        AsyncMock(side_effect=RuntimeError("synthetic DB failure")),
    )
    answer, gateway = await infer(setup, created)
    assert answer["text"] == "Answer" and answer["continuity_blocked"]
    gateway.infer.assert_awaited_once()
    record = db.messages[created.thread_id][0]["content"]
    assert record["messages"] == [] and record["pending"] == KEY
    second = SimpleNamespace(infer=AsyncMock())
    with pytest.raises(ContinuityError, match="possibly billed"):
        await infer(setup, created, gateway=second)
    second.infer.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_canonical_providers_require_reviewed_account_access(setup, monkeypatch):
    from openvegas.gateway import providers

    review = providers.get_model_review("openai", "reviewed-test")
    review["account_access"] = False
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({"openai:reviewed-test": review}))
    with pytest.raises(ContinuityError, match="account access review"):
        await create(setup)
    assert not setup[0].writes


@pytest.mark.asyncio
async def test_unknown_gateway_outcome_keeps_pending_and_never_retries(setup):
    db, _service, _catalog = setup
    created = await create(setup)
    gateway = SimpleNamespace(infer=AsyncMock(side_effect=RuntimeError('unknown network outcome')))
    with pytest.raises(ContinuityError, match='outcome is unconfirmed'):
        await infer(setup, created, gateway=gateway)
    gateway.infer.assert_awaited_once()
    assert db.messages[created.thread_id][0]['content']['pending'] == KEY
    with pytest.raises(ContinuityError, match='possibly billed'):
        await infer(setup, created, gateway=gateway)
    gateway.infer.assert_awaited_once()
