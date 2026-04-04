from __future__ import annotations

import base64
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from server.services.file_uploads import FileUploadError, FileUploadService


@dataclass
class _Tx:
    db: "_FakeDB"

    async def execute(self, query: str, *args):
        if "UPDATE chat_file_uploads" in query and "WHERE status = 'pending' AND expires_at <= now()" in query:
            now = datetime.now(timezone.utc)
            for row in self.db.rows.values():
                if row["status"] == "pending" and row["expires_at"] <= now:
                    row["status"] = "expired"
                    row["updated_at"] = now
            return "UPDATE"
        if "DELETE FROM chat_file_uploads" in query and "status IN ('expired', 'uploaded')" in query:
            retention_sec = int(args[0])
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=retention_sec)
            delete_ids: list[str] = []
            for upload_id, row in self.db.rows.items():
                if row["status"] in {"expired", "uploaded"} and row["expires_at"] <= cutoff:
                    delete_ids.append(upload_id)
            for upload_id in delete_ids:
                self.db.rows.pop(upload_id, None)
            return "DELETE"
        if "INSERT INTO chat_file_uploads" in query:
            upload_id, user_id, filename, mime_type, size_bytes, sha256, ttl_sec = args
            now = datetime.now(timezone.utc)
            self.db.rows[str(upload_id)] = {
                "id": str(upload_id),
                "user_id": str(user_id),
                "filename": str(filename),
                "mime_type": str(mime_type),
                "size_bytes": int(size_bytes),
                "sha256": str(sha256),
                "status": "pending",
                "content_bytes": None,
                "error_code": None,
                "expires_at": now + timedelta(seconds=int(ttl_sec)),
                "completed_at": None,
                "created_at": now,
                "updated_at": now,
            }
            return "INSERT"
        if "SET status = 'expired'" in query and "WHERE id = $1::uuid" in query:
            upload_id = str(args[0])
            row = self.db.rows[upload_id]
            row["status"] = "expired"
            row["updated_at"] = datetime.now(timezone.utc)
            return "UPDATE"
        if "SET status = 'failed'" in query and "WHERE id = $1::uuid" in query:
            upload_id = str(args[0])
            error_code = ""
            if "error_code = 'size_mismatch'" in query:
                error_code = "size_mismatch"
            elif "error_code = 'sha256_mismatch'" in query:
                error_code = "sha256_mismatch"
            elif "error_code = 'mime_mismatch'" in query:
                error_code = "mime_mismatch"
            row = self.db.rows[upload_id]
            row["status"] = "failed"
            row["error_code"] = error_code
            row["updated_at"] = datetime.now(timezone.utc)
            return "UPDATE"
        if "SET status = 'uploaded'" in query and "content_bytes = $2" in query:
            upload_id, content_bytes, uploaded_ttl_sec = args
            now = datetime.now(timezone.utc)
            row = self.db.rows[str(upload_id)]
            row["status"] = "uploaded"
            row["content_bytes"] = bytes(content_bytes)
            row["error_code"] = None
            row["completed_at"] = now
            row["expires_at"] = now + timedelta(seconds=int(uploaded_ttl_sec))
            row["updated_at"] = now
            return "UPDATE"
        return "OK"

    async def fetchrow(self, query: str, *args):
        if "FROM chat_file_uploads" in query and "WHERE id = $1::uuid AND user_id = $2::uuid" in query:
            upload_id = str(args[0])
            user_id = str(args[1])
            row = self.db.rows.get(upload_id)
            if not row or row["user_id"] != user_id:
                return None
            return dict(row)
        return None


class _FakeDB:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    @asynccontextmanager
    async def transaction(self):
        yield _Tx(self)

    async def fetch(self, query: str, *args):
        if "FROM chat_file_uploads" in query and "status = 'uploaded'" in query:
            user_id = str(args[0])
            rows: list[dict] = []
            for row in self.rows.values():
                if row["user_id"] != user_id:
                    continue
                if row["status"] != "uploaded":
                    continue
                if row["expires_at"] <= datetime.now(timezone.utc):
                    continue
                rows.append(dict(row))
            rows.sort(key=lambda r: r["updated_at"], reverse=True)
            return rows[:200]
        return []


@pytest.mark.asyncio
async def test_upload_init_and_complete_success_round_trip():
    svc = FileUploadService(_FakeDB())
    content = b"Hello from OpenVegas upload path."
    sha = hashlib.sha256(content).hexdigest()

    init = await svc.upload_init(
        user_id="11111111-1111-1111-1111-111111111111",
        filename="notes.txt",
        size_bytes=len(content),
        mime_type="text/plain",
        sha256_hex=sha,
    )
    assert init["status"] == "pending"
    upload_id = str(init["upload_id"])

    done = await svc.upload_complete(
        user_id="11111111-1111-1111-1111-111111111111",
        upload_id=upload_id,
        content_base64=base64.b64encode(content).decode("ascii"),
    )
    assert done["status"] == "uploaded"
    assert done["file_id"] == upload_id


@pytest.mark.asyncio
async def test_upload_complete_enforces_ownership():
    svc = FileUploadService(_FakeDB())
    content = b"owner-bound payload"
    sha = hashlib.sha256(content).hexdigest()
    init = await svc.upload_init(
        user_id="11111111-1111-1111-1111-111111111111",
        filename="owner.txt",
        size_bytes=len(content),
        mime_type="text/plain",
        sha256_hex=sha,
    )

    with pytest.raises(FileUploadError) as exc:
        await svc.upload_complete(
            user_id="22222222-2222-2222-2222-222222222222",
            upload_id=str(init["upload_id"]),
            content_base64=base64.b64encode(content).decode("ascii"),
        )

    assert exc.value.status_code == 404
    assert exc.value.code == "upload_not_found"


@pytest.mark.asyncio
async def test_upload_complete_marks_expired_uploads():
    db = _FakeDB()
    svc = FileUploadService(db)
    content = b"expired payload"
    sha = hashlib.sha256(content).hexdigest()
    init = await svc.upload_init(
        user_id="33333333-3333-3333-3333-333333333333",
        filename="stale.txt",
        size_bytes=len(content),
        mime_type="text/plain",
        sha256_hex=sha,
    )
    upload_id = str(init["upload_id"])
    db.rows[upload_id]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=5)

    with pytest.raises(FileUploadError) as exc:
        await svc.upload_complete(
            user_id="33333333-3333-3333-3333-333333333333",
            upload_id=upload_id,
            content_base64=base64.b64encode(content).decode("ascii"),
        )

    assert exc.value.status_code == 410
    assert exc.value.code == "upload_expired"
    assert db.rows[upload_id]["status"] == "expired"


@pytest.mark.asyncio
async def test_upload_init_runs_expired_cleanup():
    db = _FakeDB()
    old_id = "00000000-0000-0000-0000-000000000000"
    db.rows[old_id] = {
        "id": old_id,
        "user_id": "11111111-1111-1111-1111-111111111111",
        "filename": "old.txt",
        "mime_type": "text/plain",
        "size_bytes": 3,
        "sha256": hashlib.sha256(b"old").hexdigest(),
        "status": "expired",
        "content_bytes": b"old",
        "error_code": None,
        "expires_at": datetime.now(timezone.utc) - timedelta(days=10),
        "completed_at": datetime.now(timezone.utc) - timedelta(days=10),
        "created_at": datetime.now(timezone.utc) - timedelta(days=10),
        "updated_at": datetime.now(timezone.utc) - timedelta(days=10),
    }
    svc = FileUploadService(db)

    content = b"new"
    await svc.upload_init(
        user_id="11111111-1111-1111-1111-111111111111",
        filename="new.txt",
        size_bytes=len(content),
        mime_type="text/plain",
        sha256_hex=hashlib.sha256(content).hexdigest(),
    )

    assert old_id not in db.rows


@pytest.mark.asyncio
async def test_upload_complete_rejects_mime_mismatch():
    svc = FileUploadService(_FakeDB())
    image_like = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 24)
    sha = hashlib.sha256(image_like).hexdigest()
    init = await svc.upload_init(
        user_id="44444444-4444-4444-4444-444444444444",
        filename="fake.txt",
        size_bytes=len(image_like),
        mime_type="text/plain",
        sha256_hex=sha,
    )

    with pytest.raises(FileUploadError) as exc:
        await svc.upload_complete(
            user_id="44444444-4444-4444-4444-444444444444",
            upload_id=str(init["upload_id"]),
            content_base64=base64.b64encode(image_like).decode("ascii"),
        )

    assert exc.value.status_code == 415
    assert exc.value.code == "mime_mismatch"


@pytest.mark.asyncio
async def test_resolve_uploaded_for_inference_enforces_uploaded_state_and_order():
    db = _FakeDB()
    svc = FileUploadService(db)
    owner = "55555555-5555-5555-5555-555555555555"

    content_a = b"A"
    init_a = await svc.upload_init(
        user_id=owner,
        filename="a.txt",
        size_bytes=1,
        mime_type="text/plain",
        sha256_hex=hashlib.sha256(content_a).hexdigest(),
    )
    await svc.upload_complete(
        user_id=owner,
        upload_id=str(init_a["upload_id"]),
        content_base64=base64.b64encode(content_a).decode("ascii"),
    )

    content_b = b"B"
    init_b = await svc.upload_init(
        user_id=owner,
        filename="b.txt",
        size_bytes=1,
        mime_type="text/plain",
        sha256_hex=hashlib.sha256(content_b).hexdigest(),
    )
    await svc.upload_complete(
        user_id=owner,
        upload_id=str(init_b["upload_id"]),
        content_base64=base64.b64encode(content_b).decode("ascii"),
    )

    rows = await svc.resolve_uploaded_for_inference(
        user_id=owner,
        file_ids=[str(init_b["upload_id"]), str(init_a["upload_id"])],
    )
    assert [r["filename"] for r in rows] == ["b.txt", "a.txt"]


@pytest.mark.asyncio
async def test_resolve_uploaded_for_inference_rejects_non_uploaded():
    db = _FakeDB()
    svc = FileUploadService(db)
    owner = "66666666-6666-6666-6666-666666666666"
    content = b"x"
    init = await svc.upload_init(
        user_id=owner,
        filename="x.txt",
        size_bytes=1,
        mime_type="text/plain",
        sha256_hex=hashlib.sha256(content).hexdigest(),
    )

    with pytest.raises(FileUploadError) as exc:
        await svc.resolve_uploaded_for_inference(user_id=owner, file_ids=[str(init["upload_id"])])
    assert exc.value.status_code == 409
    assert exc.value.code == "file_not_uploaded"


@pytest.mark.asyncio
async def test_search_uploaded_text_returns_matching_snippets():
    db = _FakeDB()
    svc = FileUploadService(db)
    owner = "77777777-7777-7777-7777-777777777777"

    content = b"OpenVegas supports multimodal uploads and web search."
    init = await svc.upload_init(
        user_id=owner,
        filename="features.txt",
        size_bytes=len(content),
        mime_type="text/plain",
        sha256_hex=hashlib.sha256(content).hexdigest(),
    )
    await svc.upload_complete(
        user_id=owner,
        upload_id=str(init["upload_id"]),
        content_base64=base64.b64encode(content).decode("ascii"),
    )

    hits = await svc.search_uploaded_text(user_id=owner, query="multimodal", limit=5)
    assert len(hits) == 1
    assert hits[0]["filename"] == "features.txt"
    assert "multimodal" in hits[0]["snippet"].lower()
