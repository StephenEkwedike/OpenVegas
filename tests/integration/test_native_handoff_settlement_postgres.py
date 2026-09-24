"""Late-mutation rollback through actual SQL settlement, synthetic transport."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from openvegas.contracts.errors import ContractError
from server.services import native_handoff_dispatch as dispatch
from tests.integration.test_native_handoff_dispatch_postgres import first, infer
from tests.integration.test_native_handoff_service_postgres import (
    continuation_db as continuation_db,  # noqa: PLC0414
)
from tests.integration.test_native_handoff_service_postgres import (
    fresh_runtime_flags as fresh_runtime_flags,  # noqa: PLC0414
)
from tests.integration.test_native_handoff_service_postgres import (
    handoff_db as handoff_db,  # noqa: PLC0414
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("phase", ["transaction_entry", "wallet"])
@pytest.mark.parametrize("change", ["drop", "equal_replacement"])
async def test_late_binding_change_rolls_back_usage_and_envelope(handoff_db, monkeypatch, phase, change):
    c = handoff_db
    d = await first(c)
    balance_before = await c.gateway.wallet.get_balance("user:" + c.user)
    finalize = c.gateway._finalize_inference_execution
    changed = []

    def mutate(req):
        changed.append(True)
        req._native_handoff_binding = None if change == "drop" else replace(req._native_handoff_binding)

    async def intercept(ctx, req, result):
        if phase == "transaction_entry":
            original = c.db.transaction

            @asynccontextmanager
            async def transaction():
                async with original() as tx:
                    mutate(req)
                    yield tx

            with patch.object(c.db, "transaction", transaction):
                return await finalize(ctx, req, result)
        settle = c.gateway._settle_preauth

        async def after_wallet(**kwargs):
            await settle(**kwargs)
            mutate(req)

        with patch.object(c.gateway, "_settle_preauth", after_wallet):
            return await finalize(ctx, req, result)

    monkeypatch.setattr(c.gateway, "_finalize_inference_execution", intercept)
    with pytest.raises(ContractError):
        await infer(c, d)
    assert changed == [True]
    assert len(d.calls) == 1
    request_id = await c.db.fetchval("SELECT first_request_id FROM native_task_handoffs")
    assert request_id is not None
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage WHERE request_id=$1::uuid", request_id) == 0
    assert await c.db.fetchval("SELECT count(*) FROM native_generation_envelopes WHERE request_id=$1::uuid", request_id) == 0
    assert await c.db.fetchval("SELECT status FROM inference_requests WHERE id=$1::uuid", request_id) == "failed"
    preauth = await c.db.fetchrow("SELECT status,settled_v FROM inference_preauthorizations WHERE request_id=$1", str(request_id))
    assert preauth["status"] == "voided" and preauth["settled_v"] == 0
    assert await c.gateway.wallet.get_balance("user:" + c.user) == balance_before


async def test_provider_completion_after_deadline_can_still_settle(handoff_db, monkeypatch):
    c = handoff_db
    d = await first(c)
    finalize = c.gateway._finalize_inference_execution

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=1)

    async def intercept(ctx, req, result):
        # Original dispatch completed in time; no new authorization is needed
        # to account for that response after the old deadline has elapsed.
        with patch.object(dispatch, "datetime", Later):
            return await finalize(ctx, req, result)

    monkeypatch.setattr(c.gateway, "_finalize_inference_execution", intercept)
    result = await infer(c, d)
    assert len(d.calls) == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage WHERE request_id=$1::uuid",
                               result.inference_request_id) == 1


async def test_stream_cannot_switch_adapter_after_reservation(handoff_db, monkeypatch):
    c = handoff_db
    d = await first(c)
    prepare = c.gateway._prepare_inference_execution
    reservations = []

    async def substituted(req):
        result = await prepare(req)
        ctx, replay = result
        assert ctx is not None and replay is None
        assert await c.db.fetchval("SELECT status FROM inference_preauthorizations WHERE id=$1", ctx.preauth_id) == "reserved"
        reservations.append(ctx.request_id)
        req.provider, req.model = "openai", "gpt-4.1"
        return result

    async def forbidden_stream(**_kwargs):
        pytest.fail("changed handoff entered another provider")
        yield {}

    adapter = AsyncMock(side_effect=AssertionError("changed handoff entered provider router"))
    monkeypatch.setattr(c.gateway, "_prepare_inference_execution", substituted)
    monkeypatch.setattr(c.gateway, "_prefers_openai_responses_api", lambda _: True)
    monkeypatch.setattr(c.gateway, "_stream_openai_responses", forbidden_stream)
    monkeypatch.setattr(c.gateway, "_route_to_provider", adapter)
    events = []
    with pytest.raises(ContractError):
        async for event in c.gateway.stream_infer(d.request):
            events.append(event)
    assert not events and not d.calls
    assert len(reservations) == 1
    adapter.assert_not_awaited()


async def test_settlement_success_replay_checks_binding_after_row_wait(handoff_db, monkeypatch):
    c = handoff_db
    d = await first(c)
    original = c.gateway._finalize_inference_execution
    captured = []

    async def capture(ctx, req, result):
        captured.append((ctx, req, result))
        return await original(ctx, req, result)

    monkeypatch.setattr(c.gateway, "_finalize_inference_execution", capture)
    result = await infer(c, d)
    ctx, req, _ = captured[0]
    transaction = c.db.transaction
    changed = []

    class Connection:
        def __init__(self, real):
            self.real = real

        def __getattr__(self, name):
            return getattr(self.real, name)

        async def fetchrow(self, sql, *args):
            row = await self.real.fetchrow(sql, *args)
            if sql.strip() == "SELECT * FROM inference_requests WHERE id = $1 FOR UPDATE":
                assert row["status"] == "succeeded"
                req._native_handoff_binding = replace(req._native_handoff_binding)
                changed.append(True)
            return row

    @asynccontextmanager
    async def swapped():
        async with transaction() as tx:
            yield Connection(tx)

    with patch.object(c.db, "transaction", swapped), pytest.raises(ContractError):
        await original(ctx, req, result)
    assert changed == [True] and len(d.calls) == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage WHERE request_id=$1::uuid",
                               result.inference_request_id) == 1
