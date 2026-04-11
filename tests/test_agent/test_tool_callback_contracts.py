from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openvegas.agent.orchestration_service import AgentOrchestrationService
from openvegas.agent.orchestration_contracts import valid_actions_signature
from openvegas.agent.runtime_contracts import require_raw_sha256_hex
from openvegas.agent.runtime_contracts import result_submission_hash as compute_result_submission_hash
from openvegas.agent.tool_cas import (
    cancel_started_tx,
    claim_started_tx,
    heartbeat_tx,
    is_started_tool_timed_out,
    terminalize_tx,
)
from openvegas.contracts.errors import APIErrorCode, ContractError


class _FakeTx:
    def __init__(self, *, execute_statuses: list[str] | None = None, fetchrows: list[dict | None] | None = None):
        self.execute_statuses = list(execute_statuses or [])
        self.fetchrows = list(fetchrows or [])
        self.queries: list[str] = []

    async def execute(self, query: str, *_args):
        self.queries.append(query)
        if self.execute_statuses:
            return self.execute_statuses.pop(0)
        return "UPDATE 0"

    async def fetchrow(self, query: str, *_args):
        self.queries.append(query)
        if self.fetchrows:
            return self.fetchrows.pop(0)
        return None


class _UniqueViolationTx:
    async def execute(self, query: str, *_args):
        _ = query
        raise RuntimeError('duplicate key value violates unique constraint "ux_one_started_tool_per_run"')

    async def fetchrow(self, query: str, *_args):
        _ = query
        return None


@pytest.mark.asyncio
async def test_duplicate_start_same_tuple_is_idempotent_success():
    tx = _FakeTx(
        execute_statuses=["UPDATE 0"],
        fetchrows=[{"status": "started", "execution_token": "tok"}],
    )
    outcome = await claim_started_tx(tx, run_id="r1", tool_call_id="t1", execution_token="tok")
    assert outcome == "idempotent"


@pytest.mark.asyncio
async def test_duplicate_start_different_tuple_is_rejected():
    tx = _FakeTx(
        execute_statuses=["UPDATE 0"],
        fetchrows=[{"status": "started", "execution_token": "other"}],
    )
    with pytest.raises(ContractError) as e:
        await claim_started_tx(tx, run_id="r1", tool_call_id="t1", execution_token="tok")
    assert e.value.code == APIErrorCode.INVALID_TRANSITION


@pytest.mark.asyncio
async def test_start_unique_constraint_maps_to_active_mutation_in_progress():
    with pytest.raises(ContractError) as e:
        await claim_started_tx(_UniqueViolationTx(), run_id="r1", tool_call_id="t1", execution_token="tok")
    assert e.value.code == APIErrorCode.ACTIVE_MUTATION_IN_PROGRESS


@pytest.mark.asyncio
async def test_duplicate_cancel_same_tuple_is_idempotent_success():
    tx = _FakeTx(
        execute_statuses=["UPDATE 0"],
        fetchrows=[{"status": "cancelled", "execution_token": "tok"}],
    )
    outcome = await cancel_started_tx(tx, run_id="r1", tool_call_id="t1", execution_token="tok")
    assert outcome == "idempotent"


@pytest.mark.asyncio
async def test_duplicate_cancel_different_tuple_is_rejected():
    tx = _FakeTx(
        execute_statuses=["UPDATE 0"],
        fetchrows=[{"status": "cancelled", "execution_token": "other"}],
    )
    with pytest.raises(ContractError) as e:
        await cancel_started_tx(tx, run_id="r1", tool_call_id="t1", execution_token="tok")
    assert e.value.code == APIErrorCode.INVALID_TRANSITION


@pytest.mark.asyncio
async def test_tool_result_rejects_cancelled():
    svc = AgentOrchestrationService(db=None)
    with pytest.raises(ContractError) as e:
        await svc.result_tool_call(
            user_id="85add5d1-aaad-4caa-8422-8cd41ff400f7",
            actor_role="authenticated",
            run_id="r1",
            runtime_session_id="s1",
            tool_call_id="t1",
            execution_token="tok",
            result_status="cancelled",
            result_payload={},
            stdout="",
            stderr="",
        )
    assert e.value.code == APIErrorCode.INVALID_TRANSITION


def test_null_heartbeat_timeout_fallback_uses_started_at_threshold():
    now = datetime.now(timezone.utc)
    assert is_started_tool_timed_out(
        started_at=now - timedelta(seconds=91),
        last_heartbeat_at=None,
        now=now,
        timeout_seconds=90,
    )
    assert not is_started_tool_timed_out(
        started_at=now - timedelta(seconds=30),
        last_heartbeat_at=None,
        now=now,
        timeout_seconds=90,
    )


def test_raw_lowercase_hash_validation():
    valid = "a" * 64
    assert require_raw_sha256_hex(valid, "stdout_sha256") == valid
    with pytest.raises(ValueError):
        require_raw_sha256_hex("sha256:" + valid, "stdout_sha256")
    with pytest.raises(ValueError):
        require_raw_sha256_hex(valid.upper(), "stdout_sha256")


class _FakeTxCM:
    def __init__(self, tx):
        self.tx = tx

    async def __aenter__(self):
        return self.tx

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeServiceDB:
    def __init__(self, tx):
        self.tx = tx

    def transaction(self):
        return _FakeTxCM(self.tx)


class _FakeServiceTx:
    def __init__(self, run_row: dict):
        self._run_row = dict(run_row)

    async def fetchrow(self, query: str, *_args):
        if "FROM agent_runs" in query:
            return dict(self._run_row)
        return None


class _RecordOnlyRow:
    def __init__(self, data: dict):
        self._data = dict(data)

    def __getitem__(self, key: str):
        return self._data[key]


class _RegisterWorkspaceTx:
    def __init__(self, initial_row: dict, updated_row: dict):
        self._fetchrows = [
            _RecordOnlyRow(initial_row),
            None,
            dict(updated_row),
        ]
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, query: str, *_args):
        _ = query
        if self._fetchrows:
            return self._fetchrows.pop(0)
        return None

    async def execute(self, query: str, *args):
        self.execute_calls.append((query, args))
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_duplicate_start_same_tuple_no_second_event_or_version_bump(monkeypatch):
    run_row = {
        "id": "r1",
        "user_id": "85add5d1-aaad-4caa-8422-8cd41ff400f7",
        "runtime_session_id": "11111111-1111-1111-1111-111111111111",
        "version": 7,
    }
    tx = _FakeServiceTx(run_row)
    svc = AgentOrchestrationService(db=_FakeServiceDB(tx))
    event_counter = {"count": 0}
    outcomes = iter(["claimed", "idempotent"])

    async def _claim(*_args, **_kwargs):
        return next(outcomes)

    async def _insert_event(*_args, **_kwargs):
        event_counter["count"] += 1

    async def _derive_actions(*_args, **_kwargs):
        return [{"action": "cancel"}]

    async def _success(*_args, **_kwargs):
        return {
            "error": None,
            "detail": "",
            "retryable": False,
            "current_state": "running",
            "run_version": 7,
            "projection_version": 0,
            "valid_actions": [{"action": "cancel"}],
            "valid_actions_signature": valid_actions_signature(7, [{"action": "cancel"}]),
        }

    async def _assert_session(*_args, **_kwargs):
        return None

    monkeypatch.setattr("openvegas.agent.orchestration_service.claim_started_tx", _claim)
    monkeypatch.setattr(svc, "_insert_durable_event_tx", _insert_event)
    monkeypatch.setattr(svc, "_derive_valid_actions_tx", _derive_actions)
    monkeypatch.setattr(svc, "_success_envelope_tx", _success)
    monkeypatch.setattr(svc, "_assert_runtime_session_tx", _assert_session)

    expected_sig = valid_actions_signature(7, [{"action": "cancel"}])
    first = await svc.start_tool_call(
        user_id="85add5d1-aaad-4caa-8422-8cd41ff400f7",
        actor_role="authenticated",
        run_id="r1",
        runtime_session_id="11111111-1111-1111-1111-111111111111",
        tool_call_id="t1",
        execution_token="tok",
        expected_run_version=7,
        expected_valid_actions_signature=expected_sig,
        idempotency_key="k1",
    )
    second = await svc.start_tool_call(
        user_id="85add5d1-aaad-4caa-8422-8cd41ff400f7",
        actor_role="authenticated",
        run_id="r1",
        runtime_session_id="11111111-1111-1111-1111-111111111111",
        tool_call_id="t1",
        execution_token="tok",
        expected_run_version=7,
        expected_valid_actions_signature=expected_sig,
        idempotency_key="k2",
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert int(first.payload["run_version"]) == 7
    assert int(second.payload["run_version"]) == 7
    assert event_counter["count"] == 1


@pytest.mark.asyncio
async def test_register_workspace_accepts_record_like_rows_without_get(monkeypatch):
    tx = _RegisterWorkspaceTx(
        initial_row={
            "id": "r1",
            "user_id": "85add5d1-aaad-4caa-8422-8cd41ff400f7",
            "runtime_session_id": None,
            "workspace_root": None,
            "workspace_fingerprint": None,
            "git_root": None,
        },
        updated_row={
            "id": "r1",
            "user_id": "85add5d1-aaad-4caa-8422-8cd41ff400f7",
            "runtime_session_id": "11111111-1111-1111-1111-111111111111",
            "workspace_root": "/tmp/work",
            "workspace_fingerprint": "sha256:" + ("a" * 64),
            "git_root": "/tmp/work",
            "state": "running",
            "version": 0,
        },
    )
    svc = AgentOrchestrationService(db=_FakeServiceDB(tx))

    async def _success(*_args, **_kwargs):
        return {
            "error": None,
            "detail": "",
            "retryable": False,
            "current_state": "running",
            "run_version": 0,
            "projection_version": 0,
            "valid_actions": [{"action": "cancel"}],
            "valid_actions_signature": valid_actions_signature(0, [{"action": "cancel"}]),
        }

    monkeypatch.setattr(svc, "_success_envelope_tx", _success)

    payload = await svc.register_workspace(
        user_id="85add5d1-aaad-4caa-8422-8cd41ff400f7",
        run_id="r1",
        runtime_session_id="11111111-1111-1111-1111-111111111111",
        workspace_root="/tmp/work",
        workspace_fingerprint="sha256:" + ("a" * 64),
        git_root="/tmp/work",
    )

    assert payload["error"] is None
    assert payload["run_id"] == "r1"
    assert payload["runtime_session_id"] == "11111111-1111-1111-1111-111111111111"
    assert tx.execute_calls


@pytest.mark.asyncio
async def test_duplicate_cancel_same_tuple_no_second_event_or_version_bump(monkeypatch):
    run_row = {
        "id": "r1",
        "user_id": "85add5d1-aaad-4caa-8422-8cd41ff400f7",
        "runtime_session_id": "11111111-1111-1111-1111-111111111111",
        "version": 9,
    }
    tx = _FakeServiceTx(run_row)
    svc = AgentOrchestrationService(db=_FakeServiceDB(tx))
    event_counter = {"count": 0}
    outcomes = iter(["cancelled", "idempotent"])

    async def _cancel(*_args, **_kwargs):
        return next(outcomes)

    async def _insert_event(*_args, **_kwargs):
        event_counter["count"] += 1

    async def _success(*_args, **_kwargs):
        return {
            "error": None,
            "detail": "",
            "retryable": False,
            "current_state": "running",
            "run_version": 9,
            "projection_version": 0,
            "valid_actions": [{"action": "cancel"}],
            "valid_actions_signature": valid_actions_signature(9, [{"action": "cancel"}]),
        }

    async def _assert_session(*_args, **_kwargs):
        return None

    monkeypatch.setattr("openvegas.agent.orchestration_service.cancel_started_tx", _cancel)
    monkeypatch.setattr(svc, "_insert_durable_event_tx", _insert_event)
    monkeypatch.setattr(svc, "_success_envelope_tx", _success)
    monkeypatch.setattr(svc, "_assert_runtime_session_tx", _assert_session)

    first = await svc.cancel_tool_call(
        user_id="85add5d1-aaad-4caa-8422-8cd41ff400f7",
        actor_role="authenticated",
        run_id="r1",
        runtime_session_id="11111111-1111-1111-1111-111111111111",
        tool_call_id="t1",
        execution_token="tok",
    )
    second = await svc.cancel_tool_call(
        user_id="85add5d1-aaad-4caa-8422-8cd41ff400f7",
        actor_role="authenticated",
        run_id="r1",
        runtime_session_id="11111111-1111-1111-1111-111111111111",
        tool_call_id="t1",
        execution_token="tok",
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert int(first.payload["run_version"]) == 9
    assert int(second.payload["run_version"]) == 9
    assert event_counter["count"] == 1


@pytest.mark.asyncio
async def test_callback_paths_do_not_touch_replay_table_sql():
    tx_start = _FakeTx(execute_statuses=["UPDATE 1"])
    await claim_started_tx(tx_start, run_id="r1", tool_call_id="t1", execution_token="tok")

    tx_hb = _FakeTx(execute_statuses=["UPDATE 1"])
    hb = await heartbeat_tx(tx_hb, run_id="r1", tool_call_id="t1", execution_token="tok")
    assert hb.as_dict()["active"] is True

    tx_cancel = _FakeTx(execute_statuses=["UPDATE 1"])
    await cancel_started_tx(tx_cancel, run_id="r1", tool_call_id="t1", execution_token="tok")

    tx_result = _FakeTx(execute_statuses=["UPDATE 1"])
    await terminalize_tx(
        tx_result,
        run_id="r1",
        tool_call_id="t1",
        execution_token="tok",
        result_status="succeeded",
        result_payload={"ok": True},
        stdout_text="",
        stderr_text="",
        stdout_truncated=False,
        stderr_truncated=False,
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
        terminal_response_status=200,
        terminal_response_body_text='{"error":null}',
        terminal_response_truncated=False,
        terminal_response_hash=None,
    )

    all_queries = tx_start.queries + tx_hb.queries + tx_cancel.queries + tx_result.queries
    assert all("agent_mutation_replays" not in q for q in all_queries)


@pytest.mark.asyncio
async def test_terminalize_duplicate_same_hash_replays_stored_response():
    expected_hash = compute_result_submission_hash(
        result_status="succeeded",
        result_payload={"ok": True},
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
    )
    tx = _FakeTx(
        execute_statuses=["UPDATE 0"],
        fetchrows=[
            {
                "status": "succeeded",
                "execution_token": "tok",
                "result_submission_hash": expected_hash,
                "terminal_response_status": 200,
                "terminal_response_body_text": '{"error":null}',
            }
        ],
    )
    outcome = await terminalize_tx(
        tx,
        run_id="r1",
        tool_call_id="t1",
        execution_token="tok",
        result_status="succeeded",
        result_payload={"ok": True},
        stdout_text="",
        stderr_text="",
        stdout_truncated=False,
        stderr_truncated=False,
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
        terminal_response_status=200,
        terminal_response_body_text='{"error":null}',
        terminal_response_truncated=False,
        terminal_response_hash=None,
    )
    assert outcome.state == "replayed"
    assert outcome.response_status == 200


@pytest.mark.asyncio
async def test_terminalize_duplicate_different_hash_conflicts():
    tx = _FakeTx(
        execute_statuses=["UPDATE 0"],
        fetchrows=[
            {
                "status": "succeeded",
                "execution_token": "tok",
                "result_submission_hash": "f" * 64,
                "terminal_response_status": 200,
                "terminal_response_body_text": '{"error":null}',
            }
        ],
    )
    with pytest.raises(ContractError) as e:
        await terminalize_tx(
            tx,
            run_id="r1",
            tool_call_id="t1",
            execution_token="tok",
            result_status="succeeded",
            result_payload={"ok": True},
            stdout_text="",
            stderr_text="",
            stdout_truncated=False,
            stderr_truncated=False,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
            terminal_response_status=200,
            terminal_response_body_text='{"error":null}',
            terminal_response_truncated=False,
            terminal_response_hash=None,
        )
    assert e.value.code == APIErrorCode.IDEMPOTENCY_CONFLICT
