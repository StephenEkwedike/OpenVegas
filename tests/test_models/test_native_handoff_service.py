"""Server review/confirmation tests; no real provider, wallet or customer data."""
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from openvegas.agent.native_handoff_document import PortableTaskDocument
from openvegas.contracts.errors import ContractError
from openvegas.contracts.native_scope import NativeInferenceScope
from server.services import native_handoff_service as service
from tests.test_models.test_openrouter_attachments import MODEL, OWNER, config, review


@pytest.fixture
def reviewed(monkeypatch):
    inspected = review()
    inspected.update(capabilities={"tools": True, "reasoning_efforts": ["low", "high"]},
                     supported_parameters=["tools", "reasoning"])
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({"openrouter:" + MODEL: inspected}))
    monkeypatch.setenv("OPENVEGAS_FEATURES_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "1")
    monkeypatch.setattr(service, "resolve_provider_api_key", AsyncMock(return_value="test-only-not-a-credential"))
    document = PortableTaskDocument.from_tasks([{"user_text": "Original task.", "attachment_refs": [],
        "generations": [{"assistant_text": "Completed public answer.", "observations": []}]}])
    return SimpleNamespace(config=config(), review=inspected, document=document,
                           selection=service.HandoffSelection(MODEL, max_tokens=100),
                           uploads=SimpleNamespace(resolve_uploaded_for_inference=AsyncMock()))


async def inspect(case):
    tx = SimpleNamespace(fetchrow=AsyncMock(return_value=case.config))
    return await service._review_target(tx, user_id=OWNER, document=case.document,
                                       selection=case.selection, upload_service=case.uploads)


@pytest.mark.asyncio
async def test_server_builds_exact_target_fresh_tools_bound_and_private_receipt(reviewed):
    first, second = await inspect(reviewed), await inspect(reviewed)
    assert first == second and first.model == MODEL and first.enable_tools is True
    assert first.tool_definitions_sha256 != "0" * 64
    assert first.attachment_review_sha256 != "0" * 64
    assert "Original task" not in repr(first)
    reviewed.uploads.resolve_uploaded_for_inference.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tools", "access", "pricing", "context", "output", "reasoning", "disabled"])
async def test_destination_review_rejects_unsupported_without_fallback(reviewed, monkeypatch, change):
    inspected = deepcopy(reviewed.review)
    if change == "tools": inspected["capabilities"]["tools"] = False
    elif change == "access": inspected["account_access"] = False
    elif change == "pricing": reviewed.config["cost_input_per_1m"] = "9"
    elif change == "context": inspected["context_window_tokens"] = 1
    elif change == "output": reviewed.selection = service.HandoffSelection(MODEL, max_tokens=1001)
    elif change == "reasoning": reviewed.selection = service.HandoffSelection(MODEL, reasoning_effort="xhigh")
    else: monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "0")
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({"openrouter:" + MODEL: inspected}))
    with pytest.raises(ContractError): await inspect(reviewed)


@pytest.mark.asyncio
async def test_full_review_change_changes_confirmation_fingerprint(reviewed, monkeypatch):
    first = await inspect(reviewed)
    reviewed.review["operator_note"] = "review revision two"
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({"openrouter:" + MODEL: reviewed.review}))
    second = await inspect(reviewed)
    assert first.review_fingerprint != second.review_fingerprint


@pytest.mark.asyncio
async def test_web_preview_uses_internal_identity_without_sending_a_request(reviewed, monkeypatch):
    from tests.test_models import test_openrouter_web_gateway as web
    reviewed.config, reviewed.review = web.catalog_row(), web.review()
    reviewed.selection = service.HandoffSelection(MODEL, enable_web_search=True, max_tokens=100)
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({"openrouter:" + MODEL: reviewed.review}))
    monkeypatch.setenv("OPENVEGAS_ENABLE_WEB_SEARCH", "1")
    target = await inspect(reviewed)
    assert target.enable_web_search is True


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["prepare", "confirm", "resolve"])
async def test_service_default_off_precedes_any_database_access(monkeypatch, method):
    monkeypatch.delenv("OPENVEGAS_NATIVE_TASK_HANDOFF", raising=False)
    instance = service.NativeHandoffService(object())
    with pytest.raises(ContractError, match="disabled"):
        await getattr(instance, method)()


@pytest.mark.parametrize("options", [{"model": "openrouter/auto"}, {"max_tokens": True},
                                     {"enable_web_search": 1}, {"reasoning_effort": "private-canary"}])
def test_selection_is_strict_without_private_request_diagnostics(options):
    with pytest.raises(ContractError) as error:
        service.HandoffSelection(**{"model": MODEL, **options})
    assert "private-canary" not in str(error.value)


@pytest.mark.asyncio
async def test_review_uploads_bound_total_before_fetching_more_files():
    from tests.test_models.test_openrouter_attachments import IDS, row
    rows = [row(b"x" * service.MAX_TOTAL_BYTES, index=0), row(b"y", index=1), row(b"z", index=2)]
    uploads = SimpleNamespace(resolve_uploaded_for_inference=AsyncMock(side_effect=[[r] for r in rows]))
    tx = object()
    resolver = service._ReviewUploads(tx, uploads)
    document = PortableTaskDocument.from_tasks([{"user_text": "Files", "attachment_refs": [
        {"file_id": ident, "sha256": "a" * 64} for ident in IDS[:3]],
        "generations": [{"assistant_text": "Read", "observations": []}]}])
    with pytest.raises(ContractError): await resolver.load(user_id=OWNER, document=document)
    assert uploads.resolve_uploaded_for_inference.await_count == 2
    assert all(call.kwargs["tx"] is tx for call in uploads.resolve_uploaded_for_inference.await_args_list)


@pytest.fixture
def resolution_case(monkeypatch, reviewed):
    for flag in ("OPENVEGAS_NATIVE_TASK_HANDOFF", "OPENVEGAS_NATIVE_GENERATION_SCOPE",
                 "OPENVEGAS_NATIVE_GENERATION_HISTORY"):
        monkeypatch.setenv(flag, "1")
    source = NativeInferenceScope(run_id=str(uuid4()), runtime_session_id=str(uuid4()),
        expected_run_version=1, expected_valid_actions_signature="sha256:" + "a" * 64)
    destination = source.model_copy(update={"run_id": str(uuid4())})
    now = datetime.now(UTC)
    record = SimpleNamespace(handoff_id=str(uuid4()), handoff_sha256="b" * 64,
        source_scope=source, destination_scope=None, target=SimpleNamespace(
            model=MODEL, enable_web_search=False, reasoning_effort=None, max_tokens=100),
        expires_at=now, document=reviewed.document, workspace_json="workspace")
    steps = []
    tx = SimpleNamespace(fetchval=AsyncMock(return_value=now), execute=AsyncMock())

    @asynccontextmanager
    async def transaction():
        yield tx
        steps.append("commit")

    async def load(*args, **kwargs):
        steps.append("load")
        return record

    async def runs(*args):
        steps.append("runs")
        return {source.run_id: {}, destination.run_id: {}}

    async def lock(*args):
        steps.append("record")
        return record

    monkeypatch.setattr(service.store, "load_handoff_tx", AsyncMock(side_effect=load))
    monkeypatch.setattr(service.store, "_runs", AsyncMock(side_effect=runs))
    monkeypatch.setattr(service.store, "_lock_record", AsyncMock(side_effect=lock))
    monkeypatch.setattr(service.store, "_registration", Mock())
    monkeypatch.setattr(service.store, "_workspace", Mock(return_value="workspace"))
    monkeypatch.setattr(service.store, "bind_destination_tx", AsyncMock(return_value=record))
    monkeypatch.setattr(service, "_review_target", AsyncMock(side_effect=AssertionError("resolution reviewed target")))
    kwargs = dict(user_id=OWNER, handoff_id=record.handoff_id, handoff_sha256=record.handoff_sha256,
                  destination_scope=destination, idempotency_key="confirm-original")
    return SimpleNamespace(instance=service.NativeHandoffService(SimpleNamespace(transaction=transaction)),
                           record=record, tx=tx, steps=steps, kwargs=kwargs, now=now)


@pytest.mark.asyncio
async def test_resolution_expired_unbound_is_repeatable_locked_and_read_only(resolution_case):
    c = resolution_case
    for _ in range(2):
        assert await c.instance.resolve(**c.kwargs) == service.HandoffResolution("expired_uncommitted")
    assert c.steps == ["load", "runs", "load", "record", "commit"] * 2
    assert c.tx.fetchval.await_args.args == ("SELECT clock_timestamp()",)
    c.tx.execute.assert_not_awaited()
    service.store.bind_destination_tx.assert_not_awaited()
    service._review_target.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolution_still_valid_unbound_is_not_recoverable(resolution_case):
    c = resolution_case
    c.record.expires_at += timedelta(microseconds=1)
    with pytest.raises(ContractError):
        await c.instance.resolve(**c.kwargs)
    c.tx.execute.assert_not_awaited()
    service.store.bind_destination_tx.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolution_commit_after_wait_replays_without_review_or_expiry_check(resolution_case):
    c = resolution_case
    original = service.store._runs.side_effect

    async def committed(*args):
        runs = await original(*args)
        c.record.destination_scope = c.kwargs["destination_scope"]
        c.record.expires_at -= timedelta(days=1)
        return runs

    service.store._runs.side_effect = committed
    result = await c.instance.resolve(**c.kwargs)
    assert result.outcome == "committed" and result.confirmed.destination_scope == c.record.destination_scope
    assert result.confirmed.expires_at == c.record.expires_at
    service.store.bind_destination_tx.assert_awaited_once_with(
        c.tx, **c.kwargs, target=c.record.target)
    service.store._lock_record.assert_not_awaited()
    c.tx.fetchval.assert_not_awaited()
    service._review_target.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["digest", "source_destination", "workspace", "owner", "commit_conflict",
                                   "reload_digest", "locked_commit"])
async def test_resolution_mismatch_never_becomes_expired_proof(resolution_case, change):
    c = resolution_case
    if change == "digest": c.kwargs["handoff_sha256"] = "c" * 64
    elif change == "source_destination": c.kwargs["destination_scope"] = c.record.source_scope
    elif change == "workspace": service.store._workspace.return_value = "different"
    elif change == "owner": service.store._runs.side_effect = RuntimeError("private-owner-diagnostic")
    elif change == "commit_conflict":
        c.record.destination_scope = c.kwargs["destination_scope"]
        service.store.bind_destination_tx.side_effect = RuntimeError("private-commit-conflict")
    elif change == "reload_digest":
        original = service.store._runs.side_effect
        async def altered(*args):
            runs = await original(*args)
            c.record.handoff_sha256 = "c" * 64
            return runs
        service.store._runs.side_effect = altered
    else:
        async def changed(*args):
            c.record.destination_scope = c.kwargs["destination_scope"]
            return c.record
        service.store._lock_record.side_effect = changed
    with pytest.raises(ContractError) as error:
        await c.instance.resolve(**c.kwargs)
    assert "private-" not in str(error.value)
    c.tx.execute.assert_not_awaited()
