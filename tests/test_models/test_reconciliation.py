"""Offline stored-fact/transaction simulations; not native PostgreSQL certification."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from openvegas.gateway.conversation import CanonicalConversation
from openvegas.gateway.reconciliation import (
    AUDIT_SCHEMA_PROPOSAL,
    ReconciliationError,
    inspect_turn,
    request_payload_hash,
    restore_turn,
)
from scripts import reconcile_inference_turn as command

USER, OTHER, THREAD, REQUEST, KEY, HOLD, MESSAGE, OPERATOR = [str(uuid4()) for _ in range(8)]
PROMPT = "Remember the blue balloon."
ANSWER = "The balloon is blue."
SCOPE = {"user_id": USER, "thread_id": THREAD, "request_id": REQUEST}


def fixture_data(cost="3", reserved="5"):
    cost, reserved = Decimal(cost), Decimal(reserved)
    history = CanonicalConversation.from_messages(
        [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Welcome"},
        ]
    )
    pending = json.loads(history.to_json()) | {"pending": KEY}
    body = {
        "text": ANSWER,
        "completion_status": "complete",
        "input_tokens": 15,
        "output_tokens": 7,
        "v_cost": str(cost),
        "actual_cost_usd": "0.03",
        "provider_request_id": "synthetic-provider-receipt",
        "tool_calls": [],
        "web_search_used": False,
        "web_search_sources": [],
        "web_search_retry_without_tool": False,
    }
    reference, account = f"infer-preauth:{HOLD}", f"user:{USER}"
    escrow = f"escrow:{reference}"
    ledger = []
    for ref, kind, debit, credit, amount in (
        (reference, "reserve", account, escrow, reserved),
        (reference, "reserve_settle", escrow, "store", min(cost, reserved)),
        (reference, "reserve_refund", escrow, account, max(reserved - cost, Decimal(0))),
        (reference + ":extra", "redeem", account, "store", max(cost - reserved, Decimal(0))),
    ):
        if amount:
            ledger.append(
                {
                    "id": str(uuid4()),
                    "reference_id": ref,
                    "entry_type": kind,
                    "debit_account": debit,
                    "credit_account": credit,
                    "amount": amount,
                }
            )
    return {
        "thread": {
            "id": THREAD,
            "user_id": USER,
            "provider": "openai",
            "model_id": "fixture",
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        },
        "records": [{"id": MESSAGE, "role": "system", "content": json.dumps(pending)}],
        "request": {
            "id": REQUEST,
            "user_id": USER,
            "idempotency_key": KEY,
            "status": "succeeded",
            "inference_source": "wrapper",
            "response_status": 200,
            "payload_hash": request_payload_hash(
                history, provider="openai", model="fixture", prompt=PROMPT, max_tokens=1024
            ),
            "response_body_text": json.dumps(body),
            "final_charge_v": cost,
            "final_provider_cost_usd": Decimal("0.03"),
            "provider_request_id": body["provider_request_id"],
        },
        "holds": [
            {
                "id": HOLD,
                "request_id": REQUEST,
                "user_id": USER,
                "account_id": account,
                "provider": "openai",
                "model_id": "fixture",
                "reserved_v": reserved,
                "settled_v": cost,
                "status": "settled" if cost else "refunded",
            }
        ],
        "usage": [
            {
                "id": str(uuid4()),
                "request_id": REQUEST,
                "user_id": USER,
                "account_id": account,
                "provider": "openai",
                "model_id": "fixture",
                "input_tokens": 15,
                "output_tokens": 7,
                "v_cost": cost,
                "actual_cost_usd": Decimal("0.03"),
                "inference_source": "wrapper",
            }
        ],
        "ledger": ledger,
        "escrow": {"balance": Decimal(0)} if reserved else None,
        "charges": [
            {
                "event_id": str(uuid4()),
                "user_id": USER,
                "display_amount_v": -cost,
                "display_status": "completed",
                "metadata_json": json.dumps(
                    {
                        "provider": "openai",
                        "model_id": "fixture",
                        "input_tokens": 15,
                        "output_tokens": 7,
                    }
                ),
            }
        ],
        "receipts": [],
    }


class MemoryDB:
    """Row-lock/rollback boundary simulation, with strict SQL dispatch and no network."""

    def __init__(self, data=None):
        self.data = data or fixture_data()
        self.lock = asyncio.Lock()
        self.calls = []
        self.writes = []
        self.fail_receipt = False
        self.lose_ack = False
        self.fail_compare = False

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            before = copy.deepcopy(self.data)
            try:
                yield self
            except BaseException:
                self.data = before
                raise
            if self.lose_ack:
                self.lose_ack = False
                raise ConnectionError("synthetic lost commit acknowledgement")

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        if "FROM provider_threads " in query:
            row = self.data["thread"]
            return copy.deepcopy(row) if args == (row["id"], row["user_id"]) else None
        if "FROM inference_requests " in query:
            row = self.data["request"]
            return copy.deepcopy(row) if args == (row["id"], row["user_id"]) else None
        if "FROM wallet_accounts " in query:
            return copy.deepcopy(self.data["escrow"])
        raise AssertionError(query)

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        if "FROM provider_thread_messages " in query:
            return copy.deepcopy(self.data["records"])
        if "FROM inference_preauthorizations " in query:
            return copy.deepcopy(self.data["holds"])
        if "FROM inference_usage " in query:
            return copy.deepcopy(self.data["usage"])
        if "FROM ledger_entries " in query:
            return copy.deepcopy([r for r in self.data["ledger"] if r["reference_id"] in args[0]])
        if "FROM wallet_accounts " in query:
            return []
        if "FROM wallet_history_projection " in query:
            return copy.deepcopy(self.data["charges"])
        if "FROM inference_turn_reconciliations " in query:
            return copy.deepcopy(self.data["receipts"])
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.writes.append((query, args))
        if query.startswith("UPDATE provider_thread_messages "):
            record = self.data["records"][0]
            if self.fail_compare or json.loads(record["content"]) != json.loads(args[3]):
                return "UPDATE 0"
            assert args[0] == record["id"] and args[2] == THREAD
            record["content"] = args[1]
            return "UPDATE 1"
        if query.startswith("UPDATE provider_threads "):
            assert args == (THREAD, USER)
            return "UPDATE 1"
        if query.startswith("INSERT INTO inference_turn_reconciliations "):
            if self.fail_receipt:
                raise RuntimeError("synthetic audit persistence error")
            assert args[:4] == (USER, THREAD, REQUEST, OPERATOR)
            self.data["receipts"].append(
                {
                    "user_id": USER,
                    "thread_id": THREAD,
                    "request_id": REQUEST,
                    "operator_id": OPERATOR,
                    "plan_token": args[4],
                    "details": args[5],
                }
            )
            return "INSERT 0 1"
        raise AssertionError("No financial write is allowed: " + query)


async def plan(db, **kwargs):
    return await inspect_turn(db, **(SCOPE | {"prompt": PROMPT, "max_tokens": 1024} | kwargs))


async def apply(db, token, **kwargs):
    return await restore_turn(
        db,
        **(
            SCOPE
            | {
                "operator_id": OPERATOR,
                "expected_plan": token,
                "prompt": PROMPT,
                "max_tokens": 1024,
            }
            | kwargs
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cost", "reserved"), [("3", "5"), ("9", "5"), ("0", "5"), ("0", "0"), ("5", "0")]
)
async def test_read_only_plan_and_single_history_recovery_without_money_movement(cost, reserved):
    db = MemoryDB(fixture_data(cost, reserved))
    initial = copy.deepcopy(db.data)
    report = await plan(db)
    assert report["can_restore"] and report["status"] == "recoverable_completed"
    assert report["final_charge_v"] == str(Decimal(cost).quantize(Decimal("0.000001")))
    assert db.data == initial and not db.writes
    assert not any("FOR UPDATE" in q for q, _ in db.calls)
    result = await apply(db, report["plan_token"])
    assert result["status"] == "restored" and not result["replayed"]
    history = CanonicalConversation.from_storage(db.data["records"][0]["content"])
    assert history.turns[-2:] == (("user", PROMPT), ("assistant", ANSWER))
    assert len(history.turns) == 4
    for key in ("request", "holds", "usage", "ledger", "escrow", "charges"):
        assert db.data[key] == initial[key]
    assert len(db.data["receipts"]) == 1
    safe_output = json.dumps(report) + json.dumps(result) + db.data["receipts"][0]["details"]
    assert PROMPT not in safe_output and ANSWER not in safe_output
    assert "synthetic-provider-receipt" not in safe_output


@pytest.mark.asyncio
async def test_concurrent_restore_serializes_and_replays_without_double_append():
    db = MemoryDB()
    report = await plan(db)
    results = await asyncio.gather(*(apply(db, report["plan_token"]) for _ in range(12)))
    assert sum(not r["replayed"] for r in results) == 1
    assert len(db.data["receipts"]) == 1
    assert len(CanonicalConversation.from_storage(db.data["records"][0]["content"]).turns) == 4
    locks = [q for q, _ in db.calls if "FOR UPDATE" in q]
    for index, table in enumerate(
        ("provider_threads", "inference_requests", "inference_preauthorizations", "wallet_accounts")
    ):
        assert table in locks[index]
    assert "ORDER BY account_id FOR UPDATE" in locks[3]


@pytest.mark.asyncio
async def test_lost_commit_ack_is_recoverable_by_inspection_not_a_paid_retry():
    db = MemoryDB()
    report = await plan(db)
    db.lose_ack = True
    with pytest.raises(ConnectionError):
        await apply(db, report["plan_token"])
    replay = await plan(db)
    assert replay["status"] == "already_reconciled"
    assert (await apply(db, report["plan_token"]))["replayed"]
    assert len(db.data["receipts"]) == 1


@pytest.mark.asyncio
async def test_history_and_receipt_roll_back_together():
    db = MemoryDB()
    original = copy.deepcopy(db.data)
    report = await plan(db)
    db.fail_receipt = True
    with pytest.raises(RuntimeError):
        await apply(db, report["plan_token"])
    assert db.data == original
    db.fail_receipt = False
    assert (await apply(db, report["plan_token"]))["status"] == "restored"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["processing", "failed", "future"])
async def test_uncertain_outcomes_never_assume_provider_was_unbilled(status):
    db = MemoryDB()
    db.data["request"]["status"] = status
    db.data["holds"][0]["status"] = "voided"
    db.data["holds"][0]["settled_v"] = Decimal(0)
    db.data["usage"] = []
    db.data["charges"] = []
    db.data["ledger"] = []
    report = await plan(db)
    assert report["status"] == "blocked"
    assert report["reason"] == "UNCONFIRMED_PROVIDER_OUTCOME"
    assert not report["provider_invoice_verified"]
    with pytest.raises(ReconciliationError):
        await apply(db, "a" * 64)
    assert not db.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["user_id", "thread_id", "request_id"])
async def test_user_thread_request_scopes_cannot_be_crossed(field):
    db = MemoryDB()
    with pytest.raises(ReconciliationError, match="SCOPED_TURN_NOT_FOUND"):
        await plan(db, **{field: OTHER})
    assert not db.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["holds", "usage"])
@pytest.mark.parametrize("field", ["user_id", "account_id", "provider", "model_id", "request_id"])
async def test_financial_rows_must_belong_to_exact_user_account_and_model(table, field):
    db = MemoryDB()
    db.data[table][0][field] = OTHER
    with pytest.raises(ReconciliationError, match="SCOPE_MISMATCH"):
        await plan(db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "wrong_amount", "wrong_credit", "legacy", "extra", "open_escrow"],
)
async def test_exact_ledger_evidence_is_required(mutation):
    db = MemoryDB()
    ledger = db.data["ledger"]
    if mutation == "missing":
        ledger.pop()
    elif mutation == "duplicate":
        ledger.append(copy.deepcopy(ledger[0]))
    elif mutation == "wrong_amount":
        ledger[0]["amount"] += 1
    elif mutation == "wrong_credit":
        ledger[1]["credit_account"] = f"user:{OTHER}"
    elif mutation == "legacy":
        ledger[0]["reference_id"] = REQUEST
    elif mutation == "extra":
        ledger.append(ledger[0] | {"entry_type": "operator_correction"})
    else:
        db.data["escrow"]["balance"] = Decimal(1)
    with pytest.raises(ReconciliationError, match="LEDGER_MISMATCH|ESCROW_NOT_CLOSED"):
        await plan(db)
    assert not db.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["usage", "holds", "charges"])
@pytest.mark.parametrize("count", [0, 2])
async def test_missing_or_duplicate_authoritative_rows_fail_closed(table, count):
    db = MemoryDB()
    db.data[table] *= count
    with pytest.raises(ReconciliationError, match="INCOMPLETE_SETTLEMENT_EVIDENCE"):
        await plan(db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["v_cost", "actual_cost_usd", "input_tokens", "provider_request_id"]
)
async def test_response_must_match_persisted_usage_and_charge(field):
    db = MemoryDB()
    body = json.loads(db.data["request"]["response_body_text"])
    body[field] = 42
    db.data["request"]["response_body_text"] = json.dumps(body)
    with pytest.raises(ReconciliationError, match="RESPONSE_"):
        await plan(db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("completion_status", "incomplete"),
        ("tool_calls", [{}]),
        ("web_search_used", True),
        ("web_search_sources", ["link"]),
        ("web_search_retry_without_tool", True),
    ],
)
async def test_truncated_tool_or_web_output_is_not_restored(field, value):
    db = MemoryDB()
    body = json.loads(db.data["request"]["response_body_text"])
    body[field] = value
    db.data["request"]["response_body_text"] = json.dumps(body)
    report = await plan(db)
    assert report["status"] == "blocked" and report["reason"] == "RESPONSE_NOT_COMPLETE_PLAIN_TEXT"


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "0.0000001", "1e100", True])
async def test_nonfinite_negative_imprecise_amounts_never_become_new_prices(amount):
    db = MemoryDB()
    db.data["request"]["final_charge_v"] = amount
    with pytest.raises(ReconciliationError, match="INVALID_STORED_AMOUNT"):
        await plan(db)


@pytest.mark.asyncio
async def test_prompt_required_never_guessed_and_original_hash_exact():
    db = MemoryDB()
    assert (await plan(db, prompt=None))["reason"] == "ORIGINAL_PROMPT_REQUIRED"
    for kwargs in ({"prompt": PROMPT + " "}, {"max_tokens": 512}):
        with pytest.raises(ReconciliationError, match="ORIGINAL_REQUEST_HASH_MISMATCH"):
            await plan(db, **kwargs)
    assert not db.writes


@pytest.mark.asyncio
async def test_expired_thread_stays_blocked():
    db = MemoryDB()
    db.data["thread"]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    assert (await plan(db))["reason"] == "THREAD_EXPIRED_OR_UNBOUNDED"


@pytest.mark.asyncio
async def test_pending_binding_plan_token_and_optimistic_compare_are_required():
    db = MemoryDB()
    report = await plan(db)
    with pytest.raises(ReconciliationError, match="REVIEWED_PLAN_CHANGED"):
        await apply(db, "f" * 64)
    db.data["charges"][0]["event_id"] = str(uuid4())
    with pytest.raises(ReconciliationError, match="REVIEWED_PLAN_CHANGED"):
        await apply(db, report["plan_token"])
    report = await plan(db)
    db.fail_compare = True
    with pytest.raises(ReconciliationError, match="HISTORY_CHANGED"):
        await apply(db, report["plan_token"])
    pending = json.loads(db.data["records"][0]["content"])
    pending["pending"] = OTHER
    db.data["records"][0]["content"] = json.dumps(pending)
    with pytest.raises(ReconciliationError, match="PENDING_REQUEST_MISMATCH"):
        await plan(db)
    assert not db.data["receipts"]


def test_payload_hash_matches_actual_gateway_contract():
    from openvegas.gateway.inference import AIGateway, InferenceRequest

    history = CanonicalConversation.from_messages([])
    for provider in ("openai", "anthropic", "gemini", "mistral", "openrouter"):
        request = InferenceRequest(
            account_id=f"user:{USER}",
            provider=provider,
            model="fixture",
            messages=[{"role": "user", "content": PROMPT}],
            max_tokens=1024,
            strict_continuity=True,
        )
        assert request_payload_hash(
            history, provider=provider, model="fixture", prompt=PROMPT, max_tokens=1024
        ) == AIGateway._payload_hash(request)


@pytest.mark.asyncio
async def test_openrouter_namespaced_model_recovery_uses_stored_scope_not_live_registry():
    db = MemoryDB()
    provider, model = "openrouter", "openai/fixture-model"
    for row in (db.data["thread"], db.data["holds"][0], db.data["usage"][0]):
        row.update(provider=provider, model_id=model)
    charge = db.data["charges"][0]
    metadata = json.loads(charge["metadata_json"])
    charge["metadata_json"] = json.dumps(metadata | {"provider": provider, "model_id": model})
    content = json.loads(db.data["records"][0]["content"])
    content.pop("pending")
    db.data["request"]["payload_hash"] = request_payload_hash(
        CanonicalConversation.from_storage(content),
        provider=provider,
        model=model,
        prompt=PROMPT,
        max_tokens=1024,
    )
    report = await plan(db)
    assert (await apply(db, report["plan_token"]))["status"] == "restored"


def arguments(**changes):
    return SimpleNamespace(
        **(
            {
                "user": USER,
                "thread": THREAD,
                "request": REQUEST,
                "prompt_file": None,
                "apply": False,
                "operator": None,
                "confirm_request": None,
                "confirm_plan": None,
            }
            | changes
        )
    )


@pytest.mark.parametrize(
    "url",
    [
        "",
        "postgresql://remote.example/ov_test_one",
        "postgresql://localhost/ov_test_one",
        "postgresql://127.0.0.1/postgres",
        "postgresql://127.0.0.1/ov_test_one?host=remote",
        "postgresql://127.0.0.1/ov_test_one#fragment",
        "postgresql://127.0.0.1:bad/ov_test_one",
        "postgresql://127.0.0.1/%6fv_test_one",
    ],
)
def test_operator_rejects_remote_ambiguous_or_nondisposable_targets(url):
    with pytest.raises(ReconciliationError):
        command.validate_target(arguments(), url)


def test_read_only_default_and_explicit_apply_confirmation():
    url = "postgresql://local:secret@127.0.0.1:5544/ov_test_reconcile"
    assert command.validate_target(arguments(), url) == url
    args = arguments(
        apply=True,
        operator=OPERATOR,
        confirm_request=REQUEST,
        confirm_plan="a" * 64,
        prompt_file="private.json",
    )
    assert command.validate_target(args, url) == url
    for field in ("operator", "confirm_request", "confirm_plan", "prompt_file"):
        invalid = copy.copy(args)
        setattr(invalid, field, None)
        with pytest.raises(ReconciliationError):
            command.validate_target(invalid, url)


def test_private_prompt_file_bounded_and_regular(tmp_path):
    path = tmp_path / "prompt.json"
    payload = {"prompt": PROMPT, "max_tokens": 1024}
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    assert command.read_prompt_file(str(path)) == payload
    path.chmod(0o644)
    with pytest.raises(ReconciliationError):
        command.read_prompt_file(str(path))
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        command.read_prompt_file(str(link))
    path.write_text("x" * (command._MAX_INPUT + 1))
    with pytest.raises(ReconciliationError):
        command.read_prompt_file(str(path))


def test_command_never_prints_raw_error_or_loads_generic_database_url(monkeypatch, capsys):
    monkeypatch.delenv(command.DB_ENV, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret:password@remote.example/production")
    monkeypatch.setattr(
        command, "read_prompt_file", lambda *_: pytest.fail("Target must fail first")
    )
    code = command.main(["--user", USER, "--thread", THREAD, "--request", REQUEST])
    output = capsys.readouterr().out
    assert code == 1 and "not_confirmed" in output
    assert all(value not in output for value in ("secret", "password", "remote.example", PROMPT))


@pytest.mark.asyncio
async def test_audit_receipt_is_scoped_versioned_and_not_an_unconditional_unlock():
    db = MemoryDB()
    report = await plan(db)
    await apply(db, report["plan_token"])
    good = copy.deepcopy(db.data["receipts"])
    for key, value in (
        ("action", "refund"),
        ("thread_id", OTHER),
        ("request_id", OTHER),
        ("version", 2),
        ("operator_id", None),
        ("plan_token", "invalid"),
    ):
        db.data["receipts"] = copy.deepcopy(good)
        receipt = db.data["receipts"][0]
        metadata = json.loads(receipt["details"])
        metadata[key] = value
        receipt["details"] = json.dumps(metadata)
        with pytest.raises(ReconciliationError):
            await apply(db, report["plan_token"])
    db.data["receipts"] = good * 2
    with pytest.raises(ReconciliationError, match="AUDIT_CONFLICT"):
        await plan(db)


@pytest.mark.asyncio
async def test_normal_completion_winning_race_is_not_overwritten():
    db = MemoryDB()
    report = await plan(db)
    history = json.loads(db.data["records"][0]["content"])
    history.pop("pending")
    final = CanonicalConversation.from_storage(history).append(PROMPT, ANSWER)
    db.data["records"][0]["content"] = final.to_json()
    with pytest.raises(ReconciliationError, match="REVIEWED_PLAN_CHANGED"):
        await apply(db, report["plan_token"])
    assert not db.writes and not db.data["receipts"]


@pytest.mark.asyncio
async def test_cancellation_during_audit_rolls_back_history():
    class CancelDB(MemoryDB):
        async def execute(self, query, *args):
            if query.startswith("INSERT INTO inference_turn_reconciliations"):
                raise asyncio.CancelledError
            return await super().execute(query, *args)

    db = CancelDB()
    original = copy.deepcopy(db.data)
    report = await plan(db)
    with pytest.raises(asyncio.CancelledError):
        await apply(db, report["plan_token"])
    assert db.data == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("display_amount_v", Decimal(-4)), ("display_status", "failed"), ("user_id", OTHER)],
)
async def test_charge_projection_is_not_trusted_without_cross_check(field, value):
    db = MemoryDB()
    db.data["charges"][0][field] = value
    with pytest.raises(ReconciliationError, match="CHARGE_PROJECTION_MISMATCH"):
        await plan(db)


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": PROMPT, "max_tokens": -1},
        {"prompt": PROMPT, "max_tokens": True},
        {"prompt": PROMPT, "max_tokens": 1024, "charge": 5},
        {"prompt": "", "max_tokens": 1024},
    ],
)
def test_prompt_file_rejects_invalid_request_before_connection(tmp_path, payload):
    path = tmp_path / "prompt.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        command.read_prompt_file(str(path))


class LocalConnection(MemoryDB):
    def __init__(self):
        super().__init__()
        self.transactions = []
        self.closed = False

    @asynccontextmanager
    async def transaction(self, **kwargs):
        self.transactions.append(kwargs)
        async with super().transaction():
            yield

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_command_uses_readonly_consistent_snapshot_explicit_apply_and_no_retries(
    monkeypatch, tmp_path
):
    connection = LocalConnection()
    calls = []

    async def connect(url, **kwargs):
        calls.append((url, kwargs))
        return connection

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=connect))
    url = "postgresql://fixture@127.0.0.1/ov_test_reconciliation"
    monkeypatch.setenv(command.DB_ENV, url)
    path = tmp_path / "prompt.json"
    path.write_text(json.dumps({"prompt": PROMPT, "max_tokens": 1024}))
    path.chmod(0o600)
    args = arguments(prompt_file=str(path))
    report = await command.run(args)
    assert report["action"] == "inspection_only" and report["can_restore"]
    assert connection.transactions == [{"isolation": "repeatable_read", "readonly": True}]
    assert connection.closed and len(calls) == 1 and not connection.writes
    assert calls[0][0] == url and calls[0][1]["command_timeout"] == 10
    args = arguments(
        prompt_file=str(path),
        apply=True,
        operator=OPERATOR,
        confirm_request=REQUEST,
        confirm_plan=report["plan_token"],
    )
    applied = await command.run(args)
    assert applied["action"] == "restore_history_only" and applied["status"] == "restored"
    assert connection.transactions[-1] == {} and len(calls) == 2


def test_driver_errors_do_not_expose_credentials_or_transcripts(monkeypatch, capsys):
    async def failing_run(_):
        raise RuntimeError("database-password response-content secret-prompt")

    monkeypatch.setattr(command, "run", failing_run)
    assert command.main(["--user", USER, "--thread", THREAD, "--request", REQUEST]) == 1
    output = capsys.readouterr().out
    assert "not_confirmed" in output
    assert "database-password" not in output and "response-content" not in output


def test_blocked_dry_run_has_nonzero_exit_code(monkeypatch, capsys):
    async def blocked_run(_):
        return {"status": "blocked", "reason": "UNCONFIRMED_PROVIDER_OUTCOME"}

    monkeypatch.setattr(command, "run", blocked_run)
    assert command.main(["--user", USER, "--thread", THREAD, "--request", REQUEST]) == 2
    assert "UNCONFIRMED_PROVIDER_OUTCOME" in capsys.readouterr().out


def test_proposed_audit_schema_is_private_unique_and_not_auto_installed():
    assert "UNIQUE (user_id, thread_id, request_id)" in AUDIT_SCHEMA_PROPOSAL
    assert "ENABLE ROW LEVEL SECURITY" in AUDIT_SCHEMA_PROPOSAL
    assert "FROM anon, authenticated" in AUDIT_SCHEMA_PROPOSAL
    assert "wallet_history_projection" not in AUDIT_SCHEMA_PROPOSAL


@pytest.mark.asyncio
async def test_missing_private_audit_schema_fails_before_any_write():
    class MissingTableError(RuntimeError):
        sqlstate = "42P01"

    class MissingAuditDB(MemoryDB):
        async def fetch(self, query, *args):
            if "FROM inference_turn_reconciliations" in query:
                raise MissingTableError("must not print driver details")
            return await super().fetch(query, *args)

    db = MissingAuditDB()
    with pytest.raises(ReconciliationError, match="PRIVATE_AUDIT_MIGRATION_REQUIRED"):
        await plan(db)
    with pytest.raises(ReconciliationError, match="PRIVATE_AUDIT_MIGRATION_REQUIRED"):
        await apply(db, "a" * 64)
    assert not db.writes
