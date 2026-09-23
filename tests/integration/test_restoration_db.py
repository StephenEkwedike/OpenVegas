"""Real PostgreSQL proofs; Stripe signatures are local, provider/Auth calls are absent.

Requires the coordinator's disposable cluster and migration runner (see conftest.py).
No production credentials or provider API calls are used.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from openvegas.payments.service import BillingError, BillingService
from openvegas.payments.stripe_gateway import StripeGateway
from openvegas.wallet.ledger import InsufficientBalance, LedgerEntry, WalletService


pytestmark = pytest.mark.asyncio
MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
PRIVATE_TABLES = [
    "user_runtime_prefs", "provider_credentials", "inference_requests",
    "wrapper_reward_events", "wallet_history_projection", "org_runtime_policies",
    "context_retention_policies", "provider_threads", "provider_thread_messages",
    "agent_runs", "agent_run_events", "agent_run_tool_calls", "agent_tool_approvals",
    "agent_run_holds", "agent_run_mutation_leases", "agent_mutation_replays",
    "run_status_projection", "agent_chat_turns", "chat_file_uploads", "schema_migrations",
]


async def _user(db):
    user_id = uuid.uuid4()
    # Local auth.users FK scaffold only; this does not sign up or authenticate anyone.
    await db.execute("INSERT INTO auth.users(id) VALUES ($1)", user_id)
    return user_id


async def _topup(db, *, status="checkout_created", expires_at=None, persist_session=True):
    user_id = await _user(db)
    topup_id = uuid.uuid4()
    session_id = "cs_test_local_" + uuid.uuid4().hex
    customer_id = "cus_local_" + uuid.uuid4().hex
    await db.execute(
        """INSERT INTO fiat_topups
           (id, user_id, amount_usd, v_credit, status, idempotency_key,
            idempotency_payload_hash, mode, stripe_checkout_session_id,
            stripe_customer_id, expires_at)
           VALUES ($1, $2, 10, 1000, $3, $4, $5, 'stripe', $6, $7, $8)""",
        topup_id, user_id, status, "local_" + uuid.uuid4().hex,
        BillingService.canonical_payload_hash({"amount_usd": Decimal("10"), "currency": "usd"}),
        session_id if persist_session else None, customer_id, expires_at,
    )
    return user_id, topup_id, session_id, customer_id


def _checkout_event(topup, *, event_id=None):
    _user_id, topup_id, session_id, customer_id = topup
    return {
        "id": event_id or "evt_local_" + uuid.uuid4().hex,
        "object": "event",
        "created": int(time.time()),
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {"object": {
            "id": session_id, "object": "checkout.session", "mode": "payment",
            "payment_status": "paid", "currency": "usd", "amount_total": 1000,
            "payment_intent": "pi_local_" + str(topup_id), "customer": customer_id,
            "client_reference_id": str(topup_id), "metadata": {"topup_id": str(topup_id)},
        }},
    }


def _signed(event):
    raw = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    secret = os.environ["STRIPE_WEBHOOK_SECRET"].encode()
    digest = hmac.new(secret, timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return {"raw_body": raw, "signature": f"t={timestamp},v1={digest}"}


def _intent_event(topup):
    _, topup_id, _, customer_id = topup
    return {
        "id": "evt_local_" + uuid.uuid4().hex, "object": "event",
        "type": "payment_intent.succeeded", "created": int(time.time()), "livemode": False,
        "data": {"object": {
            "id": "pi_local_" + str(topup_id), "object": "payment_intent", "status": "succeeded",
            "amount": 1000, "amount_received": 1000, "currency": "usd", "customer": customer_id,
            "metadata": {"topup_id": str(topup_id)},
        }},
    }


def _billing(db, wallet=None):
    return BillingService(db, wallet or WalletService(db), StripeGateway())


async def _credit_count(db, topup_id):
    return await db.fetchval(
        "SELECT count(*) FROM ledger_entries WHERE reference_id=$1 AND entry_type='fiat_topup'",
        "fiat_topup:" + str(topup_id),
    )


async def test_fresh_all_migrations_satisfy_runtime_schema(database_factory):
    from server.services.dependencies import FeatureFlags, assert_schema_compatible

    async with database_factory(through=45) as sandbox:
        flags = FeatureFlags(
            store_enabled=True, inference_enabled=True, agent_runtime_enabled=True,
            human_casino_enabled=True, mint_audit_enabled=True, context_enabled=True,
            trusted_proxy_headers_enabled=False, files_enabled=True,
        )
        await assert_schema_compatible(sandbox.db, flags)
        assert await sandbox.db.fetchval("SELECT count(*) FROM provider_catalog") >= 1


async def test_every_migration_is_present_in_application_journal(database_factory):
    async with database_factory(through=45) as sandbox:
        actual = {row["version"] for row in await sandbox.db.fetch("SELECT version FROM schema_migrations")}
        expected = {path.stem for path in MIGRATIONS.glob("[0-9]*.sql")}
        assert expected, "Migration source directory must not be empty"
        assert not expected - actual, f"Unjournaled migrations: {sorted(expected - actual)}"


@pytest.mark.parametrize("legacy_status", ["started", "succeeded"])
async def test_021_upgrade_preserves_existing_tool_rows(database_factory, legacy_status):
    async with database_factory(through=21) as sandbox:
        db = sandbox.db
        user_id = await _user(db)
        run_id, tool_id = uuid.uuid4(), uuid.uuid4()
        await db.execute("INSERT INTO agent_runs(id,user_id,state) VALUES ($1,$2,'running')", run_id, user_id)
        await db.execute(
            """INSERT INTO agent_run_tool_calls
               (id,run_id,run_version,tool_name,tool_class,payload_hash,status,started_at,finished_at)
               VALUES ($1,$2,0,'read_file','read_only',$3,$4,now(),
                       CASE WHEN $4='succeeded' THEN now() ELSE NULL END)""",
            tool_id, run_id, "a" * 64, legacy_status,
        )
        before = dict(await db.fetchrow("SELECT * FROM agent_run_tool_calls WHERE id=$1", tool_id))
        await sandbox.migrate(through=38)
        after = dict(await db.fetchrow("SELECT * FROM agent_run_tool_calls WHERE id=$1", tool_id))
        assert {key: after[key] for key in before} == before
        assert after["claimed_at"] is None
        assert after["terminal_response_status"] is None
        assert after["terminal_response_body_text"] is None
        constraints = await db.fetch(
            "SELECT conname,convalidated FROM pg_constraint WHERE conname=ANY($1::text[])",
            ["ck_tool_started_requires_claim", "ck_tool_terminal_response_payload"],
        )
        assert len(constraints) == 2
        assert all(row["convalidated"] is False for row in constraints)
        import asyncpg

        with pytest.raises(asyncpg.CheckViolationError):
            await db.execute(
                """INSERT INTO agent_run_tool_calls
                   (id,run_id,run_version,tool_name,tool_class,payload_hash,status,started_at,finished_at)
                   VALUES ($1,$2,0,'read_file','read_only',$3,$4,now(),
                           CASE WHEN $4='succeeded' THEN now() ELSE NULL END)""",
                uuid.uuid4(), run_id, "b" * 64, legacy_status,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await db.execute("UPDATE agent_run_tool_calls SET tool_name=tool_name WHERE id=$1", tool_id)


async def test_private_payload_tables_are_rls_protected(database_factory):
    tables = PRIVATE_TABLES
    async with database_factory() as sandbox:
        rows = await sandbox.db.fetch(
            """SELECT c.relname, c.relrowsecurity FROM pg_class c
               JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public' AND c.relname=ANY($1::text[])""", tables,
        )
        state = {row["relname"]: row["relrowsecurity"] for row in rows}
        assert all(state.get(name) for name in tables), {
            name: state.get(name) for name in tables if not state.get(name)
        }


@pytest.mark.parametrize("status,constraint", [
    ("started", "ck_tool_started_requires_claim"),
    ("succeeded", "ck_tool_terminal_response_payload"),
])
async def test_fresh_schema_rejects_new_tool_rows_without_runtime_evidence(database_factory, status, constraint):
    import asyncpg

    async with database_factory() as sandbox:
        db = sandbox.db
        run_id = uuid.uuid4()
        await db.execute(
            "INSERT INTO agent_runs(id,user_id,state) VALUES ($1,$2,'running')", run_id, await _user(db),
        )
        with pytest.raises(asyncpg.CheckViolationError) as error:
            await db.execute(
                """INSERT INTO agent_run_tool_calls
                   (id,run_id,run_version,tool_name,tool_class,payload_hash,status,started_at,finished_at)
                   VALUES ($1,$2,0,'read_file','read_only',$3,$4,now(),
                           CASE WHEN $4='succeeded' THEN now() ELSE NULL END)""",
                uuid.uuid4(), run_id, "b" * 64, status,
            )
        assert error.value.constraint_name == constraint
        assert await db.fetchval("SELECT count(*) FROM agent_run_tool_calls") == 0


@pytest.mark.parametrize("write_kind", ["insert", "update"])
async def test_terminal_tool_content_type_cannot_be_null(database_factory, write_kind):
    import asyncpg

    async with database_factory() as sandbox:
        db = sandbox.db
        run_id, tool_id = uuid.uuid4(), uuid.uuid4()
        await db.execute(
            "INSERT INTO agent_runs(id,user_id,state) VALUES ($1,$2,'running')", run_id, await _user(db),
        )
        query = """INSERT INTO agent_run_tool_calls
                   (id,run_id,run_version,tool_name,tool_class,payload_hash,status,started_at,finished_at,
                    terminal_response_status,terminal_response_content_type,terminal_response_body_text)
                   VALUES ($1,$2,0,'read_file','read_only',$3,'succeeded',now(),now(),200,$4,'{}')"""
        if write_kind == "insert":
            with pytest.raises(asyncpg.CheckViolationError):
                await db.execute(query, tool_id, run_id, "c" * 64, None)
            assert await db.fetchval("SELECT count(*) FROM agent_run_tool_calls") == 0
        else:
            await db.execute(query, tool_id, run_id, "c" * 64, "application/json")
            with pytest.raises(asyncpg.CheckViolationError):
                await db.execute(
                    "UPDATE agent_run_tool_calls SET terminal_response_content_type=NULL WHERE id=$1", tool_id,
                )
            assert await db.fetchval(
                "SELECT terminal_response_content_type FROM agent_run_tool_calls WHERE id=$1", tool_id,
            ) == "application/json"


@pytest.mark.parametrize("role", ["anon", "authenticated"])
async def test_private_tables_deny_browser_roles_even_if_select_granted(database_factory, role):
    import asyncpg

    async with database_factory() as sandbox:
        db = sandbox.db
        user_id = await _user(db)
        await db.execute("INSERT INTO agent_runs(id,user_id,state) VALUES ($1,$2,'running')", uuid.uuid4(), user_id)
        assert await db.fetchval("SELECT count(*) FROM agent_runs") == 1
        assert await db.fetchval("SELECT count(*) FROM schema_migrations") > 0
        for table in PRIVATE_TABLES:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with db.transaction() as tx:
                    await tx.execute(f"SET LOCAL ROLE {role}")
                    await tx.fetch(f'SELECT * FROM public."{table}"')
        async with db.transaction() as tx:
            await tx.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
            for table in PRIVATE_TABLES:
                await tx.execute(f'GRANT SELECT ON public."{table}" TO {role}')
            await tx.execute(f"SET LOCAL ROLE {role}")
            # Even this user's rows stay backend-only; this is not an Auth login test.
            await tx.execute("SELECT set_config('request.jwt.claim.sub', $1, true)", str(user_id))
            for table in PRIVATE_TABLES:
                assert await tx.fetch(f'SELECT * FROM public."{table}"') == []


async def test_wallet_concurrent_debits_preserve_nonnegative_balance(database_factory):
    async with database_factory(max_size=20) as sandbox:
        wallet = WalletService(sandbox.db)
        account = "user:" + str(await _user(sandbox.db))
        await wallet.fund_from_card(account, Decimal("10"), "initial")
        outcomes = await asyncio.wait_for(asyncio.gather(*(
            wallet.redeem(account, Decimal("3"), f"concurrent:{index}") for index in range(8)
        ), return_exceptions=True), timeout=15)
        assert sum(result is None for result in outcomes) == 3
        assert sum(isinstance(result, InsufficientBalance) for result in outcomes) == 5
        assert await wallet.get_balance(account) == Decimal("1")
        assert await sandbox.db.fetchval("SELECT count(*) FROM ledger_entries WHERE entry_type='redeem'") == 3
        assert await sandbox.db.fetchval("SELECT sum(balance) FROM wallet_accounts") == Decimal("0")


async def test_wallet_replay_survives_new_connections(database_factory):
    async with database_factory() as sandbox:
        account = "user:" + str(await _user(sandbox.db))
        await WalletService(sandbox.db).fund_from_card(account, Decimal("10"), "durable-credit")
        db = await sandbox.reconnect()
        wallet = WalletService(db)
        await wallet.fund_from_card(account, Decimal("10"), "durable-credit")
        assert await wallet.get_balance(account) == Decimal("10")
        assert await db.fetchval("SELECT count(*) FROM ledger_entries WHERE reference_id='durable-credit'") == 1


async def test_wallet_replay_does_not_abort_callers_transaction(database_factory):
    async with database_factory() as sandbox:
        db = sandbox.db
        wallet = WalletService(db)
        account = "user:" + str(await _user(db))
        await wallet.fund_from_card(account, Decimal("10"), "replay-in-outer-tx")
        async with db.transaction() as tx:
            await wallet.fund_from_card(account, Decimal("10"), "replay-in-outer-tx", tx=tx)
            assert await tx.fetchval("SELECT 1") == 1
        assert await wallet.get_balance(account) == Decimal("10")


async def test_wallet_debit_uses_supplied_transaction_with_single_connection(database_factory):
    async with database_factory(max_size=1) as sandbox:
        wallet = WalletService(sandbox.db)
        account = "user:" + str(await _user(sandbox.db))
        await wallet.fund_from_card(account, Decimal("10"), "single-connection-fund")
        async with sandbox.db.transaction() as tx:
            await asyncio.wait_for(wallet.redeem(account, Decimal("1"), "single-connection-debit", tx=tx), timeout=2)
        assert await wallet.get_balance(account) == Decimal("9")


async def test_signed_checkout_credit_and_replay_survive_service_restart(database_factory):
    async with database_factory() as sandbox:
        topup = await _topup(sandbox.db)
        user_id, topup_id, *_ = topup
        event = _checkout_event(topup)
        assert (await _billing(sandbox.db).handle_webhook(**_signed(event)))["status"] == "paid"
        db = await sandbox.reconnect()
        assert (await _billing(db).handle_webhook(**_signed(event)))["status"] == "duplicate"
        second_event = _checkout_event(topup)
        assert (await _billing(db).handle_webhook(**_signed(second_event)))["idempotent"] is True
        assert await _credit_count(db, topup_id) == 1
        assert await WalletService(db).get_balance("user:" + str(user_id)) == Decimal("1000")


async def test_invalid_stripe_signature_never_credits_wallet(database_factory):
    async with database_factory() as sandbox:
        topup = await _topup(sandbox.db)
        request = _signed(_checkout_event(topup))
        request["signature"] = "t=" + str(int(time.time())) + ",v1=" + "0" * 64
        with pytest.raises(Exception, match="signature|Signature"):
            await _billing(sandbox.db).handle_webhook(**request)
        assert await _credit_count(sandbox.db, topup[1]) == 0
        assert await sandbox.db.fetchval("SELECT count(*) FROM stripe_webhook_events") == 0


async def test_settlement_failure_rolls_back_event_and_credit_then_retries(database_factory):
    class InterruptedWallet(WalletService):
        async def fund_from_card(self, *args, **kwargs):
            await super().fund_from_card(*args, **kwargs)
            raise RuntimeError("injected interruption before commit")

    async with database_factory() as sandbox:
        topup = await _topup(sandbox.db)
        event = _checkout_event(topup)
        with pytest.raises(RuntimeError, match="injected interruption"):
            await _billing(sandbox.db, InterruptedWallet(sandbox.db)).handle_webhook(**_signed(event))
        assert await _credit_count(sandbox.db, topup[1]) == 0
        assert await sandbox.db.fetchval("SELECT count(*) FROM stripe_webhook_events") == 0
        assert await sandbox.db.fetchval("SELECT status::text FROM fiat_topups WHERE id=$1", topup[1]) == "checkout_created"
        db = await sandbox.reconnect()
        await _billing(db).handle_webhook(**_signed(event))
        assert await _credit_count(db, topup[1]) == 1


async def test_late_settlement_manual_review_is_durable(database_factory):
    async with database_factory() as sandbox:
        topup = await _topup(
            sandbox.db, status="expired", expires_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        event = _checkout_event(topup)
        result = await _billing(sandbox.db).handle_webhook(**_signed(event))
        assert result["status"] == "manual_reconciliation_required"
        db = await sandbox.reconnect()
        assert (await _billing(db).handle_webhook(**_signed(event)))["status"] == "duplicate"
        row = await sandbox.db.fetchrow(
            "SELECT status::text,manual_reconciliation_required FROM fiat_topups WHERE id=$1", topup[1],
        )
        assert row["status"] == "manual_reconciliation_required"
        assert row["manual_reconciliation_required"] is True
        assert await _credit_count(sandbox.db, topup[1]) == 0


@pytest.mark.parametrize("status", ["created", "failed"])
async def test_saved_card_success_webhook_recovers_interrupted_request(database_factory, status):
    async with database_factory() as sandbox:
        topup = await _topup(sandbox.db, status=status, persist_session=False)
        user_id, topup_id, _, customer_id = topup
        event = _intent_event(topup)
        db = await sandbox.reconnect()
        await _billing(db).handle_webhook(**_signed(event))
        assert await db.fetchval("SELECT status::text FROM fiat_topups WHERE id=$1", topup_id) == "paid"
        assert await _credit_count(db, topup_id) == 1
        assert await WalletService(db).get_balance("user:" + str(user_id)) == Decimal("1000")


async def test_checkout_event_before_session_persistence_is_not_lost(database_factory):
    async with database_factory() as sandbox:
        topup = await _topup(sandbox.db, status="created", persist_session=False)
        event = _checkout_event(topup)
        await _billing(sandbox.db).handle_webhook(**_signed(event))
        await sandbox.db.execute(
            "UPDATE fiat_topups SET stripe_checkout_session_id=$2 WHERE id=$1", topup[1], topup[2],
        )
        db = await sandbox.reconnect()
        await _billing(db).handle_webhook(**_signed(event))
        assert await _credit_count(db, topup[1]) == 1


async def test_wallet_rejected_debit_and_changed_replay_leave_transaction_usable(database_factory):
    async with database_factory(max_size=1) as sandbox:
        db = sandbox.db
        wallet = WalletService(db)
        account = "user:" + str(await _user(db))
        await wallet.fund_from_card(account, Decimal("10"), "original")
        async with db.transaction() as tx:
            with pytest.raises(InsufficientBalance):
                await wallet.redeem(account, Decimal("11"), "rejected", tx=tx)
            assert await tx.fetchval("SELECT 1") == 1
            with pytest.raises(ValueError, match="idempotency amount mismatch"):
                await wallet.fund_from_card(account, Decimal("20"), "original", tx=tx)
            assert await tx.fetchval("SELECT 1") == 1
            await wallet.redeem(account, Decimal("2"), "accepted", tx=tx)
        assert await wallet.get_balance(account) == Decimal("8")
        assert await db.fetchval("SELECT count(*) FROM ledger_entries") == 2
        assert await db.fetchval("SELECT sum(balance) FROM wallet_accounts") == 0


async def test_wallet_opposite_direction_transfers_do_not_deadlock(database_factory):
    async with database_factory() as sandbox:
        db = sandbox.db
        wallet = WalletService(db)
        a, b = "user:" + str(await _user(db)), "user:" + str(await _user(db))
        await wallet.fund_from_card(a, Decimal("100"), "fund-a")
        await wallet.fund_from_card(b, Decimal("100"), "fund-b")
        await asyncio.wait_for(asyncio.gather(*(
            wallet._execute(LedgerEntry(
                debit_account=a if i % 2 else b, credit_account=b if i % 2 else a,
                amount=Decimal("1"), entry_type="test_transfer", reference_id=str(i),
            )) for i in range(20)
        )), timeout=15)
        assert await wallet.get_balance(a) == await wallet.get_balance(b) == Decimal("100")
        assert await db.fetchval("SELECT count(*) FROM ledger_entries WHERE entry_type='test_transfer'") == 20


async def test_wallet_unrelated_unique_violation_is_not_treated_as_replay(database_factory):
    import asyncpg

    async with database_factory() as sandbox:
        wallet = WalletService(sandbox.db)
        account = "user:" + str(await _user(sandbox.db))
        entry_id = str(uuid.uuid4())
        await wallet._execute(LedgerEntry(
            id=entry_id, debit_account="mint_reserve", credit_account=account,
            amount=Decimal("10"), entry_type="fiat_topup", reference_id="first",
        ))
        async with sandbox.db.transaction() as tx:
            with pytest.raises(asyncpg.UniqueViolationError):
                await wallet._execute(LedgerEntry(
                    id=entry_id, debit_account="mint_reserve", credit_account=account,
                    amount=Decimal("10"), entry_type="fiat_topup", reference_id="different",
                ), tx=tx)
            assert await tx.fetchval("SELECT 1") == 1
        assert await wallet.get_balance(account) == Decimal("10")


async def test_wallet_concurrent_identical_replays_credit_once(database_factory):
    async with database_factory() as sandbox:
        wallet = WalletService(sandbox.db)
        account = "user:" + str(await _user(sandbox.db))
        await asyncio.wait_for(asyncio.gather(*(
            wallet.fund_from_card(account, Decimal("10"), "same-concurrent-reference")
            for _ in range(12)
        )), timeout=15)
        assert await wallet.get_balance(account) == Decimal("10")
        assert await sandbox.db.fetchval("SELECT count(*) FROM ledger_entries") == 1
        assert await sandbox.db.fetchval("SELECT sum(balance) FROM wallet_accounts") == 0


@pytest.mark.parametrize("same_event", [True, False])
async def test_concurrent_signed_events_credit_exactly_once(database_factory, same_event):
    async with database_factory() as sandbox:
        topup = await _topup(sandbox.db, status="created", persist_session=False)
        event = _intent_event(topup)
        results = await asyncio.wait_for(asyncio.gather(*(
            _billing(sandbox.db).handle_webhook(**_signed(event if same_event else _intent_event(topup)))
            for _ in range(8)
        )), timeout=15)
        assert len(results) == 8
        db = await sandbox.reconnect()
        assert await _credit_count(db, topup[1]) == 1
        assert await WalletService(db).get_balance("user:" + str(topup[0])) == Decimal("1000")
        assert await db.fetchval("SELECT count(*) FROM stripe_webhook_events") == (1 if same_event else 8)


async def test_signed_event_id_reuse_with_changed_payload_is_rejected(database_factory):
    async with database_factory() as sandbox:
        topup = await _topup(sandbox.db)
        event = _checkout_event(topup)
        await _billing(sandbox.db).handle_webhook(**_signed(event))
        event["data"]["object"]["amount_total"] = 2000
        with pytest.raises(BillingError, match="payload hash mismatch"):
            await _billing(sandbox.db).handle_webhook(**_signed(event))
        assert await _credit_count(sandbox.db, topup[1]) == 1
        assert await WalletService(sandbox.db).get_balance("user:" + str(topup[0])) == Decimal("1000")


@pytest.mark.parametrize("event_kind,field,value", [
    ("intent", "amount", 999), ("intent", "amount_received", 999),
    ("intent", "currency", "eur"), ("intent", "customer", "cus_wrong"),
    ("checkout", "amount_total", 999), ("checkout", "currency", "eur"),
    ("checkout", "customer", "cus_wrong"), ("checkout", "payment_intent", None),
])
async def test_recovery_rejects_mismatched_payment_evidence(database_factory, event_kind, field, value):
    async with database_factory() as sandbox:
        topup = await _topup(sandbox.db, status="created", persist_session=False)
        event = _intent_event(topup) if event_kind == "intent" else _checkout_event(topup)
        event["data"]["object"][field] = value
        with pytest.raises(BillingError, match="TOPUP_PROVIDER_"):
            await _billing(sandbox.db).handle_webhook(**_signed(event))
        assert await _credit_count(sandbox.db, topup[1]) == 0
        assert await sandbox.db.fetchval("SELECT count(*) FROM stripe_webhook_events") == 0
        assert await sandbox.db.fetchval("SELECT status::text FROM fiat_topups WHERE id=$1", topup[1]) == "created"


@pytest.mark.parametrize("event_kind", ["intent", "checkout"])
async def test_missing_topup_keeps_event_retryable(database_factory, event_kind):
    async with database_factory() as sandbox:
        topup = await _topup(sandbox.db, status="created", persist_session=False)
        event = _intent_event(topup) if event_kind == "intent" else _checkout_event(topup)
        row = await sandbox.db.fetchrow("SELECT * FROM fiat_topups WHERE id=$1", topup[1])
        await sandbox.db.execute("DELETE FROM fiat_topups WHERE id=$1", topup[1])
        with pytest.raises(BillingError, match="MAPPING_NOT_READY"):
            await _billing(sandbox.db).handle_webhook(**_signed(event))
        assert await sandbox.db.fetchval("SELECT count(*) FROM stripe_webhook_events") == 0
        await sandbox.db.execute(
            """INSERT INTO fiat_topups(id,user_id,amount_usd,v_credit,status,idempotency_key,
               idempotency_payload_hash,mode,stripe_customer_id)
               VALUES ($1,$2,10,1000,'created',$3,$4,'stripe',$5)""",
            topup[1], topup[0], row["idempotency_key"], row["idempotency_payload_hash"], topup[3],
        )
        db = await sandbox.reconnect()
        assert (await _billing(db).handle_webhook(**_signed(event)))["status"] == "paid"
        assert await _credit_count(db, topup[1]) == 1


@pytest.mark.parametrize("invalid_state", ["reversed", "simulated", "other_reference"])
async def test_recovery_cannot_overwrite_terminal_or_provider_identity(database_factory, invalid_state):
    async with database_factory() as sandbox:
        topup = await _topup(sandbox.db, status="created", persist_session=False)
        if invalid_state == "reversed":
            await sandbox.db.execute("UPDATE fiat_topups SET status='reversed' WHERE id=$1", topup[1])
        elif invalid_state == "simulated":
            await sandbox.db.execute("UPDATE fiat_topups SET mode='simulated' WHERE id=$1", topup[1])
        else:
            await sandbox.db.execute(
                "UPDATE fiat_topups SET stripe_payment_intent_id='pi_other' WHERE id=$1", topup[1],
            )
        with pytest.raises(BillingError):
            await _billing(sandbox.db).handle_webhook(**_signed(_intent_event(topup)))
        assert await _credit_count(sandbox.db, topup[1]) == 0
        assert await sandbox.db.fetchval("SELECT count(*) FROM stripe_webhook_events") == 0
