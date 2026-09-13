"""Real local accounting; AI transport and roulette outcome are deterministic stubs."""

import asyncio
import json
from contextlib import asynccontextmanager
from decimal import Decimal
import uuid

import pytest

from openvegas.casino.human_service import HumanCasinoService
from openvegas.contracts.errors import ContractError
from openvegas.gateway.catalog import ProviderCatalog
from openvegas.gateway.inference import AIGateway, InferenceRequest, InferenceResult
from openvegas.wallet.ledger import InsufficientBalance, WalletService

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def no_provider_network(monkeypatch, integration_environment):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(name, "local-stub-no-network")

    async def denied(*args, **kwargs):
        raise AssertionError("Unexpected provider call in isolated integration test")

    async def denied_stream(*args, **kwargs):
        raise AssertionError("Unexpected provider stream in isolated integration test")
        yield  # Make the fail-if-called stub an async generator.

    for method in ("_route_to_provider", "_call_openai", "_call_anthropic", "_call_gemini"):
        monkeypatch.setattr(AIGateway, method, denied)
    monkeypatch.setattr(AIGateway, "_stream_openai_responses", denied_stream)


def successful_provider(monkeypatch, gateway, *, text="local result"):
    calls = []

    async def route(*args, **kwargs):
        calls.append(1)
        return InferenceResult(text, 10, 5)

    async def stream(*args, **kwargs):
        calls.append(1)
        yield {"type": "completed", "result": InferenceResult(text, 10, 5)}

    monkeypatch.setattr(gateway, "_route_to_provider", route)
    monkeypatch.setattr(gateway, "_stream_openai_responses", stream)
    return calls


async def execute(gateway, req, *, streaming):
    if not streaming:
        return await gateway.infer(req)
    events = [event async for event in gateway.stream_infer(req)]
    return events[-1]["result"]


async def assert_no_outstanding_hold(db):
    assert await db.fetchval("SELECT count(*) FROM inference_preauthorizations WHERE status='reserved'") == 0
    assert await db.fetchval("SELECT count(*) FROM wallet_accounts WHERE account_id LIKE 'escrow:%' AND balance<>0") == 0
    assert await db.fetchval("SELECT sum(balance) FROM wallet_accounts") == 0


async def account(db):
    user = uuid.uuid4()
    await db.execute("INSERT INTO auth.users(id) VALUES ($1)", user)
    wallet = WalletService(db)
    await wallet.fund_from_card("user:" + str(user), Decimal("100"), "test-funding:" + str(user))
    return str(user), wallet


def request(user):
    return InferenceRequest(account_id="user:" + user, provider="openai", model="gpt-5.4",
                            messages=[{"role": "user", "content": "local test"}], max_tokens=100,
                            idempotency_key="local-inference")


async def test_streamed_ai_usage_and_replay_charge_once(database_factory, monkeypatch):
    async with database_factory() as sandbox:
        user, wallet = await account(sandbox.db)
        gateway = AIGateway(sandbox.db, wallet, ProviderCatalog(sandbox.db))
        monkeypatch.setenv("OPENAI_API_KEY", "local-stub-no-network")
        calls = []

        async def provider(**kwargs):
            calls.append(1)
            yield {"type": "text_delta", "text": "hello"}
            yield {"type": "completed", "result": InferenceResult("hello", 10, 5)}

        monkeypatch.setattr(gateway, "_stream_openai_responses", provider)
        first = [event async for event in gateway.stream_infer(request(user))]
        assert first[0]["text"] == "hello"
        cost = first[-1]["result"].v_cost
        assert cost > 0
        assert await wallet.get_balance("user:" + user) == Decimal("100") - cost
        second = [event async for event in gateway.stream_infer(request(user))]
        assert second[-1]["result"].text == "hello" and len(calls) == 1
        assert await sandbox.db.fetchval("SELECT count(*) FROM inference_usage") == 1
        assert await wallet.get_balance("user:" + user) == Decimal("100") - cost


async def test_stream_disconnect_releases_reservation(database_factory, monkeypatch):
    async with database_factory() as sandbox:
        user, wallet = await account(sandbox.db)
        gateway = AIGateway(sandbox.db, wallet, ProviderCatalog(sandbox.db))
        monkeypatch.setenv("OPENAI_API_KEY", "local-stub-no-network")

        async def provider(**kwargs):
            yield {"type": "text_delta", "text": "partial"}
            raise RuntimeError("must not consume after disconnect")

        monkeypatch.setattr(gateway, "_stream_openai_responses", provider)
        stream = gateway.stream_infer(request(user))
        assert (await anext(stream))["text"] == "partial"
        assert await wallet.get_balance("user:" + user) < Decimal("100")
        await stream.aclose()
        assert await wallet.get_balance("user:" + user) == Decimal("100")
        assert await sandbox.db.fetchval("SELECT count(*) FROM inference_usage") == 0
        assert await sandbox.db.fetchval("SELECT status FROM inference_requests") == "failed"


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_ai_low_balance_never_calls_provider(database_factory, monkeypatch, provider, streaming):
    async with database_factory() as sandbox:
        user = uuid.uuid4()
        await sandbox.db.execute("INSERT INTO auth.users(id) VALUES ($1)", user)
        await sandbox.db.execute(
            """INSERT INTO provider_catalog(provider,model_id,display_name,cost_input_per_1m,
               cost_output_per_1m,v_price_input_per_1m,v_price_output_per_1m)
               VALUES ($1,'local-model','Local test',1,1,100,100)""", provider,
        )
        gateway = AIGateway(sandbox.db, WalletService(sandbox.db), ProviderCatalog(sandbox.db))
        req = request(str(user))
        req.provider, req.model = provider, "local-model"
        with pytest.raises(InsufficientBalance):
            await asyncio.wait_for(execute(gateway, req, streaming=streaming), timeout=3)
        assert await sandbox.db.fetchval("SELECT count(*) FROM inference_requests") == 0


async def test_roulette_settlement_and_retry_are_atomic(database_factory, monkeypatch):
    from openvegas.rng.provably_fair import ProvablyFairRNG
    monkeypatch.setattr(ProvablyFairRNG, "generate_outcome", lambda *a: 1)  # red wins
    async with database_factory() as sandbox:
        user, wallet = await account(sandbox.db)
        game = HumanCasinoService(sandbox.db, wallet)
        session = await game.start_session(user_id=user, max_loss_v=Decimal("100"),
                                           max_rounds=2, idempotency_key="test-session")
        session_id = json.loads(session.body_text)["casino_session_id"]
        started = await game.start_round(user_id=user, session_id=session_id, game_code="roulette",
                                          wager_v=Decimal("50"), idempotency_key="test-round")
        round_id = json.loads(started.body_text)["round_id"]
        assert await wallet.get_balance("user:" + user) == Decimal("50")
        for action in ("bet_red", "spin"):
            response = await game.apply_action(user_id=user, round_id=round_id, action=action,
                                               payload={}, idempotency_key=action)
            assert response.status_code == 200
        settled = await game.resolve_round(user_id=user, round_id=round_id, idempotency_key="settle")
        assert json.loads(settled.body_text)["payout_v"] == "100.000000"
        assert await wallet.get_balance("user:" + user) == Decimal("150")
        replay = await game.resolve_round(user_id=user, round_id=round_id, idempotency_key="settle")
        assert replay.body_text == settled.body_text
        assert await sandbox.db.fetchval("SELECT count(*) FROM human_casino_payouts") == 1
        assert await wallet.get_balance("user:" + user) == Decimal("150")


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("phase", ["before_settlement", "after_settlement"])
async def test_cancel_during_finalize_rolls_back_then_same_key_retry_succeeds(
    database_factory, monkeypatch, streaming, phase,
):
    async with database_factory() as sandbox:
        db = sandbox.db
        user, wallet = await account(db)
        gateway = AIGateway(db, wallet, ProviderCatalog(db))
        calls = successful_provider(monkeypatch, gateway)
        entered = asyncio.Event()
        settle = gateway._settle_preauth

        async def interrupted_settlement(**kwargs):
            if phase == "after_settlement":
                await settle(**kwargs)
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(gateway, "_settle_preauth", interrupted_settlement)
        task = asyncio.create_task(execute(gateway, request(user), streaming=streaming))
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert await wallet.get_balance("user:" + user) == Decimal("100")
        assert await db.fetchval("SELECT status FROM inference_requests") == "failed"
        assert await db.fetchval("SELECT count(*) FROM inference_usage") == 0
        await assert_no_outstanding_hold(db)
        old_hold = await db.fetchval("SELECT id FROM inference_preauthorizations")

        monkeypatch.setattr(gateway, "_settle_preauth", settle)
        result = await asyncio.wait_for(execute(gateway, request(user), streaming=streaming), timeout=3)
        assert result.text == "local result" and len(calls) == 2
        assert await db.fetchval("SELECT id FROM inference_preauthorizations") != old_hold
        assert await wallet.get_balance("user:" + user) == Decimal("100") - result.v_cost
        assert await db.fetchval("SELECT count(*) FROM inference_usage") == 1
        assert await db.fetchval("SELECT count(DISTINCT reference_id) FROM ledger_entries WHERE entry_type='reserve'") == 2
        await assert_no_outstanding_hold(db)
        db = await sandbox.reconnect()
        replay = await asyncio.wait_for(
            execute(AIGateway(db, WalletService(db), ProviderCatalog(db)), request(user), streaming=streaming),
            timeout=3,
        )
        assert replay.text == result.text and replay.v_cost == result.v_cost
        assert await db.fetchval("SELECT count(*) FROM inference_usage") == 1


class CommitAckFailureDB:
    """Lose a local acknowledgement after the actual PostgreSQL transaction commits."""

    def __init__(self, db, *, boundary, error):
        self.db, self.boundary, self.error = db, boundary, error
        self.armed = True

    def __getattr__(self, name):
        return getattr(self.db, name)

    @asynccontextmanager
    async def transaction(self):
        owner = self

        class Connection:
            def __init__(self, conn):
                self.conn = conn
                self.hit_boundary = False

            def __getattr__(self, name):
                return getattr(self.conn, name)

            async def execute(self, query, *args):
                if owner.boundary == "prepare":
                    self.hit_boundary |= "INSERT INTO inference_preauthorizations" in query
                else:
                    self.hit_boundary |= "UPDATE inference_requests" in query and "SET status = 'succeeded'" in query
                return await self.conn.execute(query, *args)

        async with self.db.transaction() as tx:
            conn = Connection(tx)
            yield conn
        if self.armed and conn.hit_boundary:
            self.armed = False
            raise self.error


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_lost_finalize_commit_ack_never_refunds_committed_usage(
    database_factory, monkeypatch, streaming, error_type,
):
    async with database_factory() as sandbox:
        db = sandbox.db
        user, wallet = await account(db)
        gateway = AIGateway(
            CommitAckFailureDB(db, boundary="finalize", error=error_type("injected commit acknowledgement loss")),
            wallet, ProviderCatalog(db),
        )
        calls = successful_provider(monkeypatch, gateway)
        with pytest.raises(error_type):
            await asyncio.wait_for(execute(gateway, request(user), streaming=streaming), timeout=3)
        row = await db.fetchrow("SELECT * FROM inference_requests")
        assert row["status"] == "succeeded"
        cost = row["final_charge_v"]
        assert cost > 0 and len(calls) == 1
        assert await wallet.get_balance("user:" + user) == Decimal("100") - cost
        assert await db.fetchval("SELECT status FROM inference_preauthorizations") == "settled"
        assert await db.fetchval("SELECT count(*) FROM inference_usage") == 1
        await assert_no_outstanding_hold(db)
        # Durable result replay must not require enough balance to reserve another call.
        await wallet.redeem("user:" + user, Decimal("100") - cost, "empty-after-success")
        db = await sandbox.reconnect()
        replay = await asyncio.wait_for(
            execute(AIGateway(db, WalletService(db), ProviderCatalog(db)), request(user), streaming=streaming),
            timeout=3,
        )
        assert replay.text == "local result" and replay.v_cost == cost
        assert await WalletService(db).get_balance("user:" + user) == 0
        assert await db.fetchval("SELECT count(*) FROM inference_usage") == 1


async def test_lost_prepare_commit_ack_releases_hold_and_allows_retry(database_factory, monkeypatch):
    async with database_factory() as sandbox:
        db = sandbox.db
        user, wallet = await account(db)
        gateway = AIGateway(
            CommitAckFailureDB(db, boundary="prepare", error=asyncio.CancelledError()), wallet, ProviderCatalog(db),
        )
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(gateway.infer(request(user)), timeout=3)
        assert await wallet.get_balance("user:" + user) == 100
        assert await db.fetchval("SELECT status FROM inference_requests") == "failed"
        await assert_no_outstanding_hold(db)
        successful_provider(monkeypatch, gateway)
        result = await asyncio.wait_for(gateway.infer(request(user)), timeout=3)
        assert await wallet.get_balance("user:" + user) == Decimal("100") - result.v_cost
        assert await db.fetchval("SELECT count(*) FROM inference_usage") == 1


async def test_stale_attempt_cannot_finalize_or_abort_new_attempt(database_factory, monkeypatch):
    async with database_factory() as sandbox:
        db = sandbox.db
        user, wallet = await account(db)
        gateway = AIGateway(db, wallet, ProviderCatalog(db))
        req = request(user)
        old, _ = await gateway._prepare_inference_execution(req)
        await db.execute("UPDATE inference_requests SET updated_at=now()-interval '1 hour'")
        current, _ = await asyncio.wait_for(gateway._prepare_inference_execution(req), timeout=3)
        assert old.request_id == current.request_id
        assert old.preauth_id != current.preauth_id
        assert old.reservation_ref != current.reservation_ref
        with pytest.raises(ContractError, match="no longer owns"):
            await gateway._finalize_inference_execution(old, req, InferenceResult("obsolete", 50, 50))
        await gateway._abort_inference_execution(old)
        assert await db.fetchval("SELECT status FROM inference_requests") == "processing"
        assert await db.fetchval("SELECT status FROM inference_preauthorizations") == "reserved"
        assert await wallet.get_balance(req.account_id) == Decimal("100") - current.reserve_v
        result = await gateway._finalize_inference_execution(current, req, InferenceResult("current", 10, 5))
        replay = await gateway._finalize_inference_execution(old, req, InferenceResult("obsolete", 50, 50))
        assert replay.text == result.text == "current" and replay.v_cost == result.v_cost
        assert await db.fetchval("SELECT count(*) FROM inference_usage") == 1
        assert await wallet.get_balance(req.account_id) == Decimal("100") - result.v_cost
        await assert_no_outstanding_hold(db)


@pytest.mark.parametrize("old_status", ["reserved", "voided"])
async def test_legacy_request_based_reservation_can_retry(database_factory, monkeypatch, old_status):
    async with database_factory() as sandbox:
        db = sandbox.db
        user, wallet = await account(db)
        gateway = AIGateway(db, wallet, ProviderCatalog(db))
        req = request(user)
        rid, _ = await gateway._begin_inference_request(
            user_id=user, idempotency_key=req.idempotency_key, payload_hash=gateway._payload_hash(req),
        )
        async with db.transaction() as tx:
            await tx.execute(
                """INSERT INTO inference_preauthorizations
                   (id,account_id,user_id,request_id,provider,model_id,reserved_v,status)
                   VALUES ($1,$2,$3,$4,'openai','gpt-5.4',1,$5)""",
                uuid.uuid4(), req.account_id, user, rid, old_status,
            )
            await wallet.reserve(req.account_id, Decimal("1"), rid, tx=tx)
            if old_status == "voided":
                await wallet.settle_reservation(req.account_id, rid, Decimal("0"), tx=tx)
            await tx.execute("UPDATE inference_requests SET updated_at=now()-interval '1 hour' WHERE id=$1", rid)
        successful_provider(monkeypatch, gateway)
        result = await asyncio.wait_for(gateway.infer(req), timeout=3)
        assert await db.fetchval("SELECT count(*) FROM inference_preauthorizations") == 1
        assert await db.fetchval("SELECT count(*) FROM inference_usage") == 1
        assert await wallet.get_balance(req.account_id) == Decimal("100") - result.v_cost
        await assert_no_outstanding_hold(db)


async def test_repeated_cancellation_waits_for_atomic_cleanup(database_factory, monkeypatch):
    async with database_factory() as sandbox:
        db = sandbox.db
        user, wallet = await account(db)
        gateway = AIGateway(db, wallet, ProviderCatalog(db))
        provider_entered, cleanup_entered, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def waiting_provider(*args, **kwargs):
            provider_entered.set()
            await asyncio.Event().wait()

        abort = gateway._abort_inference_execution

        async def delayed_abort(ctx):
            cleanup_entered.set()
            await release_cleanup.wait()
            await abort(ctx)

        monkeypatch.setattr(gateway, "_route_to_provider", waiting_provider)
        monkeypatch.setattr(gateway, "_abort_inference_execution", delayed_abort)
        task = asyncio.create_task(gateway.infer(request(user)))
        try:
            await asyncio.wait_for(provider_entered.wait(), timeout=3)
            task.cancel()
            await asyncio.wait_for(cleanup_entered.wait(), timeout=3)
            task.cancel()
            release_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
        finally:
            release_cleanup.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert await wallet.get_balance("user:" + user) == 100
        assert await db.fetchval("SELECT status FROM inference_requests") == "failed"
        await assert_no_outstanding_hold(db)
