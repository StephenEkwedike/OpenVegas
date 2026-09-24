"""Server review/confirmation tests; no real provider, wallet or customer data."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openvegas.agent.native_handoff_document import PortableTaskDocument
from openvegas.contracts.errors import ContractError
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
@pytest.mark.parametrize("method", ["prepare", "confirm"])
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
