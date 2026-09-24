"""Real upload ownership/expiry/digest rechecks; synthetic reviews, no providers."""
import base64
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from openvegas.contracts.errors import ContractError
from server.services import native_handoff_attachments as handoff
from server.services.attachment_history import references
from server.services.file_uploads import FileUploadService
from server.services.openrouter_attachments import AttachmentError
from tests.test_models.test_native_handoff_attachments import document
from tests.test_models.test_openrouter_attachments import MODEL, config, review

pytestmark = pytest.mark.asyncio
CONTENT = b"Complete synthetic owned attachment."


@pytest_asyncio.fixture
async def case(database_factory, monkeypatch):
    reviewed = review()
    monkeypatch.setattr(handoff, "get_model_review", lambda *args: reviewed)
    async with database_factory(through=48, max_size=4) as sandbox:
        db = sandbox.db
        owner, other = str(uuid4()), str(uuid4())
        for user in (owner, other):
            await db.execute("INSERT INTO auth.users(id) VALUES ($1::uuid)", user)
        uploads = FileUploadService(db)
        pending = await uploads.upload_init(
            user_id=owner, filename="fixture.txt", mime_type="text/plain", size_bytes=len(CONTENT),
            sha256_hex=hashlib.sha256(CONTENT).hexdigest(),
        )
        complete = await uploads.upload_complete(
            user_id=owner, upload_id=pending["upload_id"],
            content_base64=base64.b64encode(CONTENT).decode(),
        )
        rows = await uploads.resolve_uploaded_for_inference(user_id=owner, file_ids=[complete["file_id"]])
        kwargs = {"document": document(rows), "user_id": owner, "model_id": MODEL,
                  "model_config": config(), "upload_service": uploads}
        yield SimpleNamespace(db=db, owner=owner, other=other, kwargs=kwargs,
                              file_id=complete["file_id"], refs=references(rows))


async def test_real_preview_and_dispatch_keep_owned_bytes(case):
    receipt = await handoff.preview_handoff_attachments(**case.kwargs)
    result = await handoff.dispatch_handoff_attachments(preview=receipt, **case.kwargs)
    assert result.by_task[0].blocks[0]["text"].endswith(CONTENT.decode())
    assert case.kwargs["document"].values()["tasks"][0]["attachment_refs"] == case.refs
    assert await case.db.fetchval("SELECT count(*) FROM inference_requests") == 0


@pytest.mark.parametrize("change", ["expiry", "owner", "bytes", "deleted", "status", "filename", "mime"])
async def test_upload_invalidated_after_preview_blocks_dispatch(case, change):
    receipt = await handoff.preview_handoff_attachments(**case.kwargs)
    statements = {
        "expiry": "UPDATE chat_file_uploads SET expires_at=now()-interval '1 second' WHERE id=$1::uuid",
        "owner": "UPDATE chat_file_uploads SET user_id=$2::uuid WHERE id=$1::uuid",
        "bytes": "UPDATE chat_file_uploads SET content_bytes=$2,size_bytes=length($2::bytea) WHERE id=$1::uuid",
        "deleted": "DELETE FROM chat_file_uploads WHERE id=$1::uuid",
        "status": "UPDATE chat_file_uploads SET status='expired' WHERE id=$1::uuid",
        "filename": "UPDATE chat_file_uploads SET filename='changed.txt' WHERE id=$1::uuid",
        "mime": "UPDATE chat_file_uploads SET mime_type='text/markdown' WHERE id=$1::uuid",
    }
    extra = [case.other] if change == "owner" else [b"substituted"] if change == "bytes" else []
    await case.db.execute(statements[change], case.file_id, *extra)
    with pytest.raises((ContractError, AttachmentError)):
        await handoff.dispatch_handoff_attachments(preview=receipt, **case.kwargs)
    assert await case.db.fetchval("SELECT count(*) FROM inference_requests") == 0


async def test_preview_rejects_other_owners_file(case):
    case.kwargs["user_id"] = case.other
    with pytest.raises(AttachmentError, match="attachment_unavailable"):
        await handoff.preview_handoff_attachments(**case.kwargs)
