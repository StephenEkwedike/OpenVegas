"""Real lock-order regression, not a provider or native terminal test."""
import asyncio
import hashlib
from uuid import uuid4

import pytest

from server.services.file_uploads import FileUploadError, FileUploadService

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("operation", ["resolve", "complete"])
async def test_cleanup_releases_unrelated_lock_before_requested_upload(database_factory, operation):
    async with database_factory(through=49, max_size=2) as sandbox:
        db, user = sandbox.db, str(uuid4())
        await db.execute("INSERT INTO auth.users(id) VALUES($1::uuid)", user)
        uploads = FileUploadService(db)
        args = {"user_id": user, "filename": "fixture.txt", "size_bytes": 1,
                "mime_type": "text/plain", "sha256_hex": hashlib.sha256(b"a").hexdigest()}
        a = await uploads.upload_init(**args)
        await uploads.upload_complete(user_id=user, upload_id=a["upload_id"], content_base64="YQ==")
        b = await uploads.upload_init(**args)
        await db.execute("UPDATE chat_file_uploads SET expires_at=now()-interval '1 second' WHERE id=$1::uuid", b["upload_id"])
        held_a, cleanup_held_b, requesting_b = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class PausedCleanup(FileUploadService):
            async def _cleanup_expired(self, tx):
                await super()._cleanup_expired(tx)
                cleanup_held_b.set()
                await requesting_b.wait()

        async def handoff_reader():
            async with db.transaction() as tx:
                await uploads.resolve_uploaded_for_inference(user_id=user, file_ids=[a["upload_id"]], tx=tx)
                held_a.set()
                await cleanup_held_b.wait()
                requesting_b.set()
                with pytest.raises(FileUploadError, match="not uploaded"):
                    await uploads.resolve_uploaded_for_inference(user_id=user, file_ids=[b["upload_id"]], tx=tx)

        async def standalone():
            await held_a.wait()
            service = PausedCleanup(db)
            if operation == "resolve":
                rows = await service.resolve_uploaded_for_inference(user_id=user, file_ids=[a["upload_id"]])
                assert rows[0]["content_bytes"] == b"a"
            else:
                result = await service.upload_complete(user_id=user, upload_id=a["upload_id"], content_base64="YQ==")
                assert result["status"] == "uploaded"

        tasks = [asyncio.create_task(handoff_reader()), asyncio.create_task(standalone())]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
