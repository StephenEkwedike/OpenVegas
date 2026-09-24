"""Preview is not authorization: no transports, credentials, or provider calls."""
import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openvegas.agent.native_handoff_document import PortableTaskDocument
from openvegas.contracts.errors import ContractError
from server.services import native_handoff_attachments as handoff
from server.services.attachment_history import references
from server.services.file_uploads import FileUploadError
from server.services.openrouter_attachments import AttachmentError
from tests.test_models.test_openrouter_attachments import (
    IDS,
    MODEL,
    OTHER,
    OWNER,
    config,
    picture,
    review,
    row,
)

REJECT = (ContractError, AttachmentError)


def document(rows, *, tasks=1):
    return PortableTaskDocument.from_tasks([
        {"user_text": "Inspect my files", "attachment_refs": references(rows) if rows else [],
         "generations": [{"assistant_text": "Original complete answer", "observations": []}]}
        for _ in range(tasks)
    ])


@pytest.fixture
def setup(monkeypatch):
    current = review()
    monkeypatch.setattr(handoff, "get_model_review", lambda *args: current)
    service = SimpleNamespace(resolve_uploaded_for_inference=AsyncMock(return_value=[row()]))
    kwargs = {"document": document([row()]), "user_id": OWNER, "model_id": MODEL,
              "model_config": config(), "upload_service": service}
    return SimpleNamespace(review=current, service=service, kwargs=kwargs)


@pytest.mark.asyncio
async def test_preview_has_no_bytes_and_dispatch_resolves_again(setup):
    receipt = await handoff.preview_handoff_attachments(**setup.kwargs)
    assert receipt.file_occurrences == receipt.unique_files == 1
    assert "complete text" not in repr(receipt) and "complete text" not in str(vars(receipt))
    result = await handoff.dispatch_handoff_attachments(preview=receipt, **setup.kwargs)
    assert setup.service.resolve_uploaded_for_inference.await_count == 2
    assert result.by_task[0].blocks[0]["text"].endswith("complete text")
    detached = result.by_task[0].blocks
    detached.clear()
    assert result.by_task[0].blocks and "complete text" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["bytes", "filename", "mime", "missing", "order", "owner"])
async def test_mutated_upload_is_never_substituted(setup, change):
    receipt = await handoff.preview_handoff_attachments(**setup.kwargs)
    changed = row()
    if change == "bytes":
        changed = row(b"different private contents")
    elif change == "filename":
        changed["filename"] = "different.txt"
    elif change == "mime":
        changed["mime_type"] = "text/markdown"
    elif change == "order":
        changed["file_id"] = IDS[1]
    elif change == "owner":
        changed["user_id"] = OTHER
    setup.service.resolve_uploaded_for_inference.return_value = [] if change == "missing" else [changed]
    with pytest.raises(REJECT) as error:
        await handoff.dispatch_handoff_attachments(preview=receipt, **setup.kwargs)
    assert "different private contents" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [("file_expired", 410), ("file_not_found", 404),
                                         ("file_not_uploaded", 409)])
async def test_expired_unowned_incomplete_rechecked(setup, code, status):
    receipt = await handoff.preview_handoff_attachments(**setup.kwargs)
    setup.service.resolve_uploaded_for_inference.side_effect = FileUploadError(status, code, "private detail")
    with pytest.raises(AttachmentError, match="attachment_unavailable") as error:
        await handoff.dispatch_handoff_attachments(preview=receipt, **setup.kwargs)
    assert "private detail" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "endpoint", "context", "capabilities", "price", "disabled"])
async def test_review_or_catalog_change_requires_new_preview(setup, change):
    receipt = await handoff.preview_handoff_attachments(**setup.kwargs)
    if change == "expired":
        setup.review["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif change == "endpoint":
        setup.review["attachments"]["provider"] = "different-provider"
    elif change == "context":
        setup.review["context_window_tokens"] -= 1
    elif change == "capabilities":
        setup.review["capabilities"] = {"tools": False}
    elif change == "price":
        setup.kwargs["model_config"]["v_price_input_per_1m"] = "11"
    else:
        setup.kwargs["model_config"]["enabled"] = False
    with pytest.raises(REJECT):
        await handoff.dispatch_handoff_attachments(preview=receipt, **setup.kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "model", "document"])
async def test_identity_mismatch_rejected_before_file_lookup(setup, change):
    receipt = await handoff.preview_handoff_attachments(**setup.kwargs)
    if change == "owner":
        setup.kwargs["user_id"] = OTHER
    elif change == "model":
        setup.kwargs["model_id"] = "fixture/another-model"
    else:
        setup.kwargs["document"] = document([row()], tasks=2)
    with pytest.raises(ContractError):
        await handoff.dispatch_handoff_attachments(preview=receipt, **setup.kwargs)
    assert setup.service.resolve_uploaded_for_inference.await_count == 1


@pytest.mark.asyncio
async def test_image_is_transferred_natively_or_rejected_not_text_fallback(setup):
    image = row(picture(), "image/png", "image.png")
    setup.service.resolve_uploaded_for_inference.return_value = [image]
    setup.kwargs["document"] = document([image])
    receipt = await handoff.preview_handoff_attachments(**setup.kwargs)
    result = await handoff.dispatch_handoff_attachments(preview=receipt, **setup.kwargs)
    assert result.by_task[0].blocks[0]["type"] == "image_url"
    setup.review["attachments"].update(input_modalities=["text"], image_tokens=0,
                                       pdf_page_tokens=0, pdf_file_overhead_tokens=0)
    with pytest.raises(AttachmentError, match="attachment_capability_unreviewed"):
        await handoff.dispatch_handoff_attachments(preview=receipt, **setup.kwargs)


@pytest.mark.asyncio
async def test_preserves_each_tasks_file_position(setup):
    tasks = setup.kwargs["document"].values()["tasks"]
    empty = deepcopy(tasks[0])
    empty["attachment_refs"] = []
    setup.kwargs["document"] = PortableTaskDocument.from_tasks([empty, *tasks, *tasks])
    receipt = await handoff.preview_handoff_attachments(**setup.kwargs)
    result = await handoff.dispatch_handoff_attachments(preview=receipt, **setup.kwargs)
    assert (receipt.file_occurrences, receipt.unique_files) == (2, 1)
    assert result.by_task[0] is None
    assert result.by_task[1].file_ids == result.by_task[2].file_ids == (IDS[0],)


@pytest.mark.asyncio
async def test_occurrence_bound_before_lookup(setup):
    setup.kwargs["document"] = document([row()], tasks=13)
    with pytest.raises(ContractError):
        await handoff.preview_handoff_attachments(**setup.kwargs)
    setup.service.resolve_uploaded_for_inference.assert_not_awaited()


@pytest.mark.asyncio
async def test_total_bytes_bounded_across_tasks(setup, monkeypatch):
    monkeypatch.setattr(handoff, "MAX_TOTAL_BYTES", len(row()["content_bytes"]))
    setup.kwargs["document"] = document([row()], tasks=2)
    with pytest.raises(ContractError):
        await handoff.preview_handoff_attachments(**setup.kwargs)


@pytest.mark.asyncio
async def test_empty_files_no_upload_lookup(setup):
    setup.kwargs["document"] = document([])
    receipt = await handoff.preview_handoff_attachments(**setup.kwargs)
    result = await handoff.dispatch_handoff_attachments(preview=receipt, **setup.kwargs)
    assert result.by_task == (None,) and receipt.unique_files == 0
    setup.service.resolve_uploaded_for_inference.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_filename_is_part_of_preview_binding(setup):
    image = row(picture(), "image/png", "image.png")
    setup.service.resolve_uploaded_for_inference.return_value = [image]
    setup.kwargs["document"] = document([image])
    receipt = await handoff.preview_handoff_attachments(**setup.kwargs)
    image["filename"] = "renamed.png"
    with pytest.raises(ContractError):
        await handoff.dispatch_handoff_attachments(preview=receipt, **setup.kwargs)


@pytest.mark.asyncio
async def test_review_revoked_during_upload_resolution(setup):
    receipt = await handoff.preview_handoff_attachments(**setup.kwargs)

    async def resolve(**kwargs):
        setup.review["account_access"] = False
        return [row()]

    setup.service.resolve_uploaded_for_inference.side_effect = resolve
    with pytest.raises(ContractError):
        await handoff.dispatch_handoff_attachments(preview=receipt, **setup.kwargs)


@pytest.mark.asyncio
async def test_review_time_expires_during_upload_resolution(setup):
    setup.review["expires_at"] = (datetime.now(UTC) + timedelta(seconds=0.3)).isoformat()
    receipt = await handoff.preview_handoff_attachments(**setup.kwargs)

    async def resolve(**kwargs):
        await asyncio.sleep(0.35)
        return [row()]

    setup.service.resolve_uploaded_for_inference.side_effect = resolve
    with pytest.raises(AttachmentError, match="attachment_review_required"):
        await handoff.dispatch_handoff_attachments(preview=receipt, **setup.kwargs)
