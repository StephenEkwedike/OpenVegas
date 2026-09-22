from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from openvegas.agent.orchestration_service import AgentOrchestrationService
from openvegas.agent.runtime_contracts import result_submission_hash
from openvegas.contracts.errors import APIErrorCode, ContractError

USER_ID = "85add5d1-aaad-4caa-8422-8cd41ff400f7"
RUN_ID = "22222222-2222-4222-8222-222222222222"
SESSION_ID = "11111111-1111-4111-8111-111111111111"
TOOL_ID = "33333333-3333-4333-8333-333333333333"
TOKEN = "accepted-execution-token"
TERMINAL_STATUSES = ("succeeded", "failed", "blocked", "timed_out")


class _FakeServiceTx:
    """Exercise real callback/CAS/event SQL without a provider or database."""

    def __init__(self):
        self.run = {
            "id": RUN_ID,
            "user_id": USER_ID,
            "runtime_session_id": SESSION_ID,
            "version": 7,
            "run_event_seq": 0,
        }
        self.tool = {
            "id": TOOL_ID,
            "run_id": RUN_ID,
            "status": "started",
            "execution_token": TOKEN,
        }
        self.events = []
        self.active = False
        self.fail_event_insert = False

    def snapshot(self):
        return deepcopy((self.run, self.tool, self.events))

    async def fetchrow(self, query: str, *args):
        assert self.active
        sql = " ".join(query.split())
        if sql.startswith("UPDATE agent_runs SET run_event_seq"):
            assert args == (RUN_ID,)
            self.run["run_event_seq"] += 1
            return {"run_event_seq": self.run["run_event_seq"]}
        if "FROM agent_runs" in sql:
            assert args == (RUN_ID, USER_ID)
            return deepcopy(self.run)
        if "FROM agent_run_tool_calls" in sql:
            assert args == (TOOL_ID, RUN_ID)
            return deepcopy(self.tool)
        raise AssertionError(f"Unexpected callback query: {sql}")

    async def execute(self, query: str, *args):
        assert self.active
        sql = " ".join(query.split())
        if sql.startswith("UPDATE agent_run_tool_calls"):
            assert "AND status='started'" in sql
            assert "AND execution_token=$3" in sql
            assert len(args) == 16
            if args[:3] != (TOOL_ID, RUN_ID, self.tool["execution_token"]):
                return "UPDATE 0"
            if self.tool["status"] != "started":
                return "UPDATE 0"
            fields = (
                "status", "result_payload", "stdout", "stderr", "stdout_truncated",
                "stderr_truncated", "stdout_sha256", "stderr_sha256",
                "result_submission_hash", "terminal_response_status",
                "terminal_response_body_text", "terminal_response_truncated",
                "terminal_response_hash",
            )
            self.tool.update(zip(fields, args[3:]))
            self.tool["result_payload"] = json.loads(self.tool["result_payload"])
            return "UPDATE 1"
        if sql.startswith("INSERT INTO agent_run_events"):
            assert self.tool["status"] in TERMINAL_STATUSES
            if self.fail_event_insert:
                raise RuntimeError("receipt event insert failed")
            run_id, version, sequence, event_type, actor_id, payload = args
            self.events.append({
                "run_id": run_id,
                "run_version": version,
                "event_seq": sequence,
                "event_type": event_type,
                "actor_id": actor_id,
                "payload": json.loads(payload),
            })
            return "INSERT 0 1"
        raise AssertionError(f"Unexpected callback write: {sql}")


class _FakeTxCM:
    def __init__(self, tx):
        self.tx = tx

    async def __aenter__(self):
        assert not self.tx.active
        self.before = self.tx.snapshot()
        self.tx.active = True
        return self.tx

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.tx.run, self.tx.tool, self.tx.events = self.before
        self.tx.active = False
        return False


class _FakeServiceDB:
    def __init__(self, tx):
        self.tx = tx

    def transaction(self):
        return _FakeTxCM(self.tx)


@pytest.fixture
def callback_service(monkeypatch):
    tx = _FakeServiceTx()
    service = AgentOrchestrationService(_FakeServiceDB(tx))
    published = []

    async def success(**_kwargs):
        return {"error": None, "run_version": 7, "receipt_marker": "stored response"}

    async def error(**kwargs):
        return {
            "error": kwargs["error"],
            "detail": kwargs["detail"],
            "run_version": 7,
            "receipt_marker": "stored response",
        }

    monkeypatch.setattr(service, "_success_envelope_tx", success)
    monkeypatch.setattr(service, "_error_envelope_tx", error)
    monkeypatch.setattr(
        "openvegas.agent.orchestration_service.publish_tool_event",
        lambda **event: published.append(deepcopy(event)),
    )
    monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", "")
    for name in ("STDOUT", "STDERR", "RESULT_PAYLOAD", "RESPONSE"):
        monkeypatch.setenv(f"OPENVEGAS_TOOL_{name}_MAX_BYTES", "131072")
    return service, tx, published


def _submission(status="succeeded"):
    return {
        "user_id": USER_ID,
        "actor_role": "authenticated",
        "run_id": RUN_ID,
        "runtime_session_id": SESSION_ID,
        "tool_call_id": TOOL_ID,
        "execution_token": TOKEN,
        "result_status": status,
        "result_payload": {"ok": status == "succeeded", "detail": "runtime observation"},
        "stdout": "recorded stdout\n",
        "stderr": "recorded stderr\n",
    }


def _submission_hash(submission):
    return result_submission_hash(
        result_status=submission["result_status"],
        result_payload=submission["result_payload"],
        stdout_sha256=hashlib.sha256(submission["stdout"].encode()).hexdigest(),
        stderr_sha256=hashlib.sha256(submission["stderr"].encode()).hexdigest(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", ["result_payload", "result_status", "stdout", "stderr"])
async def test_known_accepted_hash_with_changed_body_is_rejected(callback_service, changed_field):
    service, tx, published = callback_service
    submission = _submission()
    await service.result_tool_call(**submission)
    before = tx.snapshot()
    forged = deepcopy(submission)
    forged[changed_field] = {
        "result_payload": {"ok": True, "detail": "forged observation"},
        "result_status": "failed",
        "stdout": "forged stdout",
        "stderr": "forged stderr",
    }[changed_field]
    forged["result_submission_hash_value"] = tx.tool["result_submission_hash"]

    with pytest.raises(ContractError) as exc:
        await service.result_tool_call(**forged)

    assert exc.value.code == APIErrorCode.INVALID_TRANSITION
    assert tx.snapshot() == before
    assert len(published) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", TERMINAL_STATUSES)
@pytest.mark.parametrize("supply_hash", [False, True])
async def test_terminal_replay_rejects_wrong_token(callback_service, status, supply_hash):
    service, tx, published = callback_service
    submission = _submission(status)
    await service.result_tool_call(**submission)
    before = tx.snapshot()
    submission["execution_token"] = "different-execution-token"
    if supply_hash:
        submission["result_submission_hash_value"] = tx.tool["result_submission_hash"]

    with pytest.raises(ContractError) as exc:
        await service.result_tool_call(**submission)

    assert exc.value.code == APIErrorCode.INVALID_TRANSITION
    assert tx.snapshot() == before
    assert len(published) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", TERMINAL_STATUSES)
@pytest.mark.parametrize("supply_hash", [False, True])
async def test_correct_tuple_replays_stored_receipt_without_another_event(
    callback_service, status, supply_hash,
):
    service, tx, published = callback_service
    submission = _submission(status)
    if supply_hash:
        submission["result_submission_hash_value"] = _submission_hash(submission)
    first = await service.result_tool_call(**submission)
    before = tx.snapshot()

    replay = await service.result_tool_call(**submission)

    assert replay.status_code == first.status_code == (200 if status == "succeeded" else 409)
    assert replay.payload == first.payload == json.loads(tx.tool["terminal_response_body_text"])
    assert tx.tool["result_submission_hash"] == _submission_hash(submission)
    assert tx.snapshot() == before
    assert len(tx.events) == len(published) == 1
    assert tx.events[0]["payload"] == {
        "tool_call_id": TOOL_ID,
        "status": status,
        "source": "runtime_callback",
        "redaction_checked": True,
        "redaction_required": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "blocked"])
async def test_accepted_409_stores_result_and_server_callback_provenance(callback_service, status):
    service, tx, published = callback_service
    submission = _submission(status)
    submission["result_payload"].update({
        "source": "reconciler",
        "reason_code": "runtime_policy_denied",
    })
    submission["result_submission_hash_value"] = _submission_hash(submission)

    result = await service.result_tool_call(**submission)

    assert result.status_code == 409
    assert result.payload["error"]
    assert tx.tool["status"] == status
    assert tx.tool["result_payload"] == submission["result_payload"]
    assert tx.tool["stdout"] == submission["stdout"]
    assert tx.tool["stderr"] == submission["stderr"]
    assert tx.tool["result_submission_hash"] == _submission_hash(submission)
    assert tx.tool["terminal_response_status"] == 409
    assert json.loads(tx.tool["terminal_response_body_text"]) == result.payload
    assert tx.events == [{
        "run_id": RUN_ID,
        "run_version": 7,
        "event_seq": 1,
        "event_type": f"tool_finished_{status}",
        "actor_id": USER_ID,
        "payload": {
            "tool_call_id": TOOL_ID,
            "status": status,
            "source": "runtime_callback",
            "redaction_checked": True,
            "redaction_required": False,
        },
    }]
    assert len(published) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", TERMINAL_STATUSES)
@pytest.mark.parametrize("field", ["stdout", "stderr"])
@pytest.mark.parametrize("matches", [False, True])
async def test_output_redaction_metadata_uses_original_submission(
    callback_service, monkeypatch, status, field, matches,
):
    service, tx, published = callback_service
    monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", r"tenant-private-\d+")
    submission = _submission(status)
    submission[field] = "tenant-private-123\n" if matches else "tenant-public-123\n"
    original = deepcopy(submission)

    await service.result_tool_call(**submission)

    assert tx.tool[field] == ("[REDACTED]\n" if matches else submission[field])
    assert tx.tool[field + "_sha256"] == hashlib.sha256(tx.tool[field].encode()).hexdigest()
    assert tx.tool[field + "_truncated"] is False
    assert tx.events[0]["payload"] == {
        "tool_call_id": TOOL_ID,
        "status": status,
        "source": "runtime_callback",
        "redaction_checked": True,
        "redaction_required": matches,
    }
    assert tx.events[0]["payload"]["redaction_checked"] is True
    assert tx.events[0]["payload"]["redaction_required"] is matches
    assert tx.tool["result_payload"] == submission["result_payload"]
    assert submission == original
    assert len(tx.events) == len(published) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", TERMINAL_STATUSES)
@pytest.mark.parametrize(("payload", "pattern", "required"), [
    pytest.param(
        {"nested": [{"text": "tenant-private-123"}]}, r"^tenant-private-\d+$", True,
        id="nested-value",
    ),
    pytest.param(
        {"nested": [{"tenant-private-123": "safe"}]}, r"^tenant-private-\d+$", True,
        id="nested-key",
    ),
    pytest.param(
        {"nested": [["tenant-private-123"]]}, r"^tenant-private-\d+$", True,
        id="nested-list",
    ),
    pytest.param(
        {"count": 123}, r'"count":123', True,
        id="serialized-payload-only",
    ),
    pytest.param(
        {"nested": [{"text": "tenant-public-123"}]}, r"^tenant-private-\d+$", False,
        id="nonmatching-configured-pattern",
    ),
])
async def test_payload_redaction_metadata_uses_recursive_and_serialized_content(
    callback_service, monkeypatch, status, payload, pattern, required,
):
    service, tx, published = callback_service
    monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", pattern)
    submission = _submission(status)
    submission["result_payload"].update(deepcopy(payload))
    submission["result_submission_hash_value"] = _submission_hash(submission)
    original = deepcopy(submission)

    await service.result_tool_call(**submission)

    assert tx.events[0]["payload"] == {
        "tool_call_id": TOOL_ID,
        "status": status,
        "source": "runtime_callback",
        "redaction_checked": True,
        "redaction_required": required,
    }
    assert tx.events[0]["payload"]["redaction_checked"] is True
    assert tx.events[0]["payload"]["redaction_required"] is required
    assert tx.tool["result_payload"] == submission["result_payload"]
    assert tx.tool["result_submission_hash"] == _submission_hash(submission)
    assert tx.tool["stdout"] == submission["stdout"]
    assert tx.tool["stderr"] == submission["stderr"]
    assert submission == original
    assert len(tx.events) == len(published) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", TERMINAL_STATUSES)
@pytest.mark.parametrize("required", [False, True])
async def test_runtime_payload_cannot_forge_server_redaction_metadata(
    callback_service, monkeypatch, status, required,
):
    service, tx, published = callback_service
    monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", r"tenant-private-\d+")
    submission = _submission(status)
    submission["result_payload"].update({
        "source": "reconciler",
        "redaction_checked": False,
        "redaction_required": not required,
        "nested": [{"content": "tenant-private-123" if required else "safe"}],
    })

    await service.result_tool_call(**submission)

    assert tx.events[0]["payload"]["source"] == "runtime_callback"
    assert tx.events[0]["payload"]["redaction_checked"] is True
    assert tx.events[0]["payload"]["redaction_required"] is required
    assert tx.tool["result_payload"] == submission["result_payload"]
    assert len(tx.events) == len(published) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", TERMINAL_STATUSES)
@pytest.mark.parametrize("field", ["stdout", "stderr", "result_payload"])
async def test_redaction_decision_survives_configuration_removal_and_replay(
    callback_service, monkeypatch, status, field,
):
    service, tx, published = callback_service
    monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", r"tenant-private-\d+")
    submission = _submission(status)
    if field == "result_payload":
        submission[field]["nested"] = [{"content": "tenant-private-123"}]
    else:
        submission[field] = "tenant-private-123"
    first = await service.result_tool_call(**submission)
    assert tx.events[0]["payload"]["redaction_checked"] is True
    assert tx.events[0]["payload"]["redaction_required"] is True
    before = tx.snapshot()

    monkeypatch.delenv("OPENVEGAS_TOOL_REDACT_PATTERNS")
    replay_submission = deepcopy(submission)
    # Replay the accepted stored bytes so changing policy does not change its hash.
    replay_submission["stdout"] = tx.tool["stdout"]
    replay_submission["stderr"] = tx.tool["stderr"]
    replay_submission["result_submission_hash_value"] = tx.tool["result_submission_hash"]
    replay = await service.result_tool_call(**replay_submission)

    assert replay.status_code == first.status_code
    assert replay.payload == first.payload
    assert tx.snapshot() == before
    assert tx.events[0]["payload"]["redaction_checked"] is True
    assert tx.events[0]["payload"]["redaction_required"] is True
    assert len(tx.events) == len(published) == 1


@pytest.mark.asyncio
async def test_supplied_hash_mismatch_rejects_first_acceptance(callback_service):
    service, tx, published = callback_service
    submission = _submission()
    submission["result_submission_hash_value"] = "0" * 64
    before = tx.snapshot()

    with pytest.raises(ContractError) as exc:
        await service.result_tool_call(**submission)

    assert exc.value.code == APIErrorCode.INVALID_TRANSITION
    assert tx.snapshot() == before
    assert not published


@pytest.mark.asyncio
async def test_callback_event_failure_rolls_back_terminal_receipt(callback_service):
    service, tx, published = callback_service
    tx.fail_event_insert = True
    before = tx.snapshot()

    with pytest.raises(RuntimeError, match="receipt event insert failed"):
        await service.result_tool_call(**_submission("failed"))

    assert tx.snapshot() == before
    assert not published
