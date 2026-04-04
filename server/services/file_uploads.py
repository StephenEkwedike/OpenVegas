"""Backend service for chat attachment upload lifecycle."""

from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class FileUploadError(Exception):
    status_code: int
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


class FileUploadService:
    def __init__(self, db: Any):
        self.db = db

    @staticmethod
    def _upload_ttl_sec() -> int:
        raw = str(os.getenv("OPENVEGAS_FILE_UPLOAD_INIT_TTL_SEC", "900")).strip()
        try:
            return max(60, min(7200, int(raw)))
        except Exception:
            return 900

    @staticmethod
    def _uploaded_ttl_sec() -> int:
        raw = str(os.getenv("OPENVEGAS_FILE_UPLOAD_COMPLETED_TTL_SEC", "259200")).strip()
        try:
            return max(300, min(30 * 24 * 3600, int(raw)))
        except Exception:
            return 259200

    @staticmethod
    def _cleanup_retention_sec() -> int:
        raw = str(os.getenv("OPENVEGAS_FILE_UPLOAD_CLEANUP_RETENTION_SEC", "86400")).strip()
        try:
            return max(300, min(30 * 24 * 3600, int(raw)))
        except Exception:
            return 86400

    @staticmethod
    def _max_upload_bytes() -> int:
        raw = str(os.getenv("OPENVEGAS_CHAT_MAX_ATTACHMENT_BYTES", str(20 * 1024 * 1024))).strip()
        try:
            return max(1024, min(100 * 1024 * 1024, int(raw)))
        except Exception:
            return 20 * 1024 * 1024

    @staticmethod
    def _max_attachments_per_turn() -> int:
        raw = str(os.getenv("OPENVEGAS_CHAT_MAX_ATTACHMENTS", "3")).strip()
        try:
            return max(1, min(20, int(raw)))
        except Exception:
            return 3

    @staticmethod
    def _allowed_mime_patterns() -> tuple[str, ...]:
        raw = str(
            os.getenv(
                "OPENVEGAS_FILE_UPLOAD_ALLOWED_MIME",
                "text/*,image/*,application/pdf,application/json,application/xml,application/octet-stream",
            )
        ).strip()
        tokens = [part.strip().lower() for part in raw.split(",") if part.strip()]
        if not tokens:
            return ("text/*", "image/*", "application/pdf", "application/octet-stream")
        return tuple(tokens)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _row_get(row: Any, key: str) -> Any:
        if row is None:
            return None
        if isinstance(row, dict):
            return row.get(key)
        try:
            return row[key]
        except Exception:
            return None

    @staticmethod
    def _normalize_filename(filename: str) -> str:
        token = str(filename or "").strip()
        token = token.replace("\\", "/")
        token = token.split("/")[-1].strip()
        if not token:
            raise FileUploadError(400, "invalid_filename", "filename is required")
        if len(token) > 255:
            raise FileUploadError(400, "invalid_filename", "filename is too long")
        return token

    @staticmethod
    def _normalize_mime(mime_type: str) -> str:
        token = str(mime_type or "").strip().lower()
        if not token or "/" not in token:
            raise FileUploadError(400, "invalid_mime_type", "mime_type is required")
        return token

    @staticmethod
    def _normalize_size(size_bytes: int) -> int:
        try:
            value = int(size_bytes)
        except Exception as exc:
            raise FileUploadError(400, "invalid_size", "size_bytes must be an integer") from exc
        if value <= 0:
            raise FileUploadError(400, "invalid_size", "size_bytes must be positive")
        if value > FileUploadService._max_upload_bytes():
            raise FileUploadError(
                413,
                "file_too_large",
                f"size_bytes exceeds limit ({FileUploadService._max_upload_bytes()} bytes)",
            )
        return value

    @staticmethod
    def _normalize_sha256(sha256_hex: str) -> str:
        token = str(sha256_hex or "").strip().lower()
        if not _SHA256_HEX_RE.match(token):
            raise FileUploadError(400, "invalid_sha256", "sha256 must be 64 lowercase hex characters")
        return token

    @classmethod
    def _mime_allowed(cls, mime_type: str) -> bool:
        token = str(mime_type or "").strip().lower()
        for pattern in cls._allowed_mime_patterns():
            if pattern.endswith("/*"):
                prefix = pattern[:-1]
                if token.startswith(prefix):
                    return True
            elif token == pattern:
                return True
        return False

    @staticmethod
    def _detect_content_mime(data: bytes, filename: str) -> str:
        if data.startswith(b"%PDF-"):
            return "application/pdf"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data[:6] in {b"GIF87a", b"GIF89a"}:
            return "image/gif"
        if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
            return "image/webp"

        guessed, _ = mimetypes.guess_type(filename)
        guessed_norm = str(guessed or "").strip().lower()

        try:
            decoded = data.decode("utf-8")
            if decoded:
                stripped = decoded.lstrip()
                if stripped.startswith("{") or stripped.startswith("["):
                    return "application/json"
                if stripped.startswith("<"):
                    return "application/xml"
                return "text/plain"
        except Exception:
            pass

        if guessed_norm:
            return guessed_norm
        return "application/octet-stream"

    @staticmethod
    def _mime_matches(claimed_mime: str, detected_mime: str, filename: str) -> bool:
        claimed = str(claimed_mime or "").strip().lower()
        detected = str(detected_mime or "").strip().lower()
        guessed, _ = mimetypes.guess_type(filename)
        guessed_norm = str(guessed or "").strip().lower()

        if detected == "application/octet-stream":
            return True
        if claimed == detected:
            return True

        claimed_major = claimed.split("/", 1)[0]
        detected_major = detected.split("/", 1)[0]
        if claimed_major != detected_major:
            return False

        if guessed_norm and guessed_norm.split("/", 1)[0] != claimed_major:
            return False
        return True

    @staticmethod
    def _extract_pdf_text(content: bytes) -> str:
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception:
            PdfReader = None  # type: ignore
        if PdfReader is not None:
            try:
                reader = PdfReader(io.BytesIO(content))
                pages: list[str] = []
                for page in list(getattr(reader, "pages", []) or []):
                    try:
                        txt = str(page.extract_text() or "").strip()
                    except Exception:
                        txt = ""
                    if txt:
                        pages.append(txt)
                joined = "\n\n".join(pages).strip()
                if joined:
                    return joined
            except Exception:
                pass

        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(prefix="ov_pdf_", suffix=".pdf", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            proc = subprocess.run(
                ["pdftotext", "-enc", "UTF-8", "-q", tmp_path, "-"],
                check=False,
                capture_output=True,
                text=True,
                timeout=8.0,
            )
            if proc.returncode == 0:
                return str(proc.stdout or "").strip()
        except Exception:
            return ""
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        return ""

    @staticmethod
    def _decode_text_content(content: bytes, *, mime_type: str) -> str:
        mime = str(mime_type or "").strip().lower()
        if mime == "application/pdf":
            return FileUploadService._extract_pdf_text(content)
        likely_text = mime.startswith("text/") or mime in {
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-yaml",
            "text/markdown",
        }
        if not likely_text:
            return ""
        try:
            return content.decode("utf-8")
        except Exception:
            try:
                return content.decode("latin-1")
            except Exception:
                return ""

    async def _cleanup_expired(self, tx: Any) -> None:
        await tx.execute(
            """
            UPDATE chat_file_uploads
            SET status = 'expired', updated_at = now()
            WHERE status = 'pending' AND expires_at <= now()
            """
        )
        await tx.execute(
            """
            DELETE FROM chat_file_uploads
            WHERE status IN ('expired', 'uploaded')
              AND expires_at <= now() - make_interval(secs => $1::int)
            """,
            self._cleanup_retention_sec(),
        )

    async def upload_init(
        self,
        *,
        user_id: str,
        filename: str,
        size_bytes: int,
        mime_type: str,
        sha256_hex: str,
    ) -> dict[str, Any]:
        normalized_name = self._normalize_filename(filename)
        normalized_size = self._normalize_size(size_bytes)
        normalized_mime = self._normalize_mime(mime_type)
        normalized_sha = self._normalize_sha256(sha256_hex)
        if not self._mime_allowed(normalized_mime):
            raise FileUploadError(415, "unsupported_mime_type", f"Unsupported mime_type: {normalized_mime}")

        upload_id = str(uuid.uuid4())
        ttl_sec = self._upload_ttl_sec()
        async with self.db.transaction() as tx:
            await self._cleanup_expired(tx)
            await tx.execute(
                """
                INSERT INTO chat_file_uploads
                  (id, user_id, filename, mime_type, size_bytes, sha256, status, expires_at, updated_at)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, 'pending',
                        now() + make_interval(secs => $7::int), now())
                """,
                upload_id,
                user_id,
                normalized_name,
                normalized_mime,
                normalized_size,
                normalized_sha,
                ttl_sec,
            )
        return {
            "upload_id": upload_id,
            "status": "pending",
            "expires_in_sec": ttl_sec,
        }

    async def upload_complete(
        self,
        *,
        user_id: str,
        upload_id: str,
        content_base64: str,
    ) -> dict[str, Any]:
        raw_upload_id = str(upload_id or "").strip()
        if not raw_upload_id:
            raise FileUploadError(400, "invalid_upload_id", "upload_id is required")

        payload = str(content_base64 or "").strip()
        if not payload:
            raise FileUploadError(400, "empty_upload_payload", "content_base64 is required")
        try:
            content_bytes = base64.b64decode(payload, validate=True)
        except Exception as exc:
            raise FileUploadError(400, "invalid_base64", "content_base64 is not valid base64") from exc

        async with self.db.transaction() as tx:
            await self._cleanup_expired(tx)
            row = await tx.fetchrow(
                """
                SELECT id, user_id, filename, mime_type, size_bytes, sha256, status, expires_at, completed_at
                FROM chat_file_uploads
                WHERE id = $1::uuid AND user_id = $2::uuid
                FOR UPDATE
                """,
                raw_upload_id,
                user_id,
            )
            if not row:
                raise FileUploadError(404, "upload_not_found", "upload_id not found for user")

            status = str(self._row_get(row, "status") or "")
            if status == "uploaded":
                return {
                    "file_id": str(self._row_get(row, "id")),
                    "upload_id": str(self._row_get(row, "id")),
                    "status": "uploaded",
                }

            expires_at = self._row_get(row, "expires_at")
            if isinstance(expires_at, datetime):
                ts = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
                if ts <= self._now():
                    await tx.execute(
                        """
                        UPDATE chat_file_uploads
                        SET status = 'expired', updated_at = now()
                        WHERE id = $1::uuid
                        """,
                        raw_upload_id,
                    )
                    raise FileUploadError(410, "upload_expired", "upload_id expired before completion")

            expected_size = int(self._row_get(row, "size_bytes") or 0)
            if len(content_bytes) != expected_size:
                await tx.execute(
                    """
                    UPDATE chat_file_uploads
                    SET status = 'failed', error_code = 'size_mismatch', updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    raw_upload_id,
                )
                raise FileUploadError(400, "size_mismatch", "Uploaded content size does not match initialized size")

            expected_sha = str(self._row_get(row, "sha256") or "").strip().lower()
            actual_sha = hashlib.sha256(content_bytes).hexdigest()
            if actual_sha != expected_sha:
                await tx.execute(
                    """
                    UPDATE chat_file_uploads
                    SET status = 'failed', error_code = 'sha256_mismatch', updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    raw_upload_id,
                )
                raise FileUploadError(400, "sha256_mismatch", "Uploaded content hash mismatch")

            claimed_mime = str(self._row_get(row, "mime_type") or "").strip().lower()
            filename = str(self._row_get(row, "filename") or "").strip()
            detected_mime = self._detect_content_mime(content_bytes, filename)
            if not self._mime_matches(claimed_mime, detected_mime, filename):
                await tx.execute(
                    """
                    UPDATE chat_file_uploads
                    SET status = 'failed', error_code = 'mime_mismatch', updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    raw_upload_id,
                )
                raise FileUploadError(
                    415,
                    "mime_mismatch",
                    f"MIME mismatch: claimed={claimed_mime} detected={detected_mime}",
                )

            uploaded_ttl_sec = self._uploaded_ttl_sec()
            await tx.execute(
                """
                UPDATE chat_file_uploads
                SET status = 'uploaded',
                    content_bytes = $2,
                    error_code = NULL,
                    completed_at = now(),
                    expires_at = now() + make_interval(secs => $3::int),
                    updated_at = now()
                WHERE id = $1::uuid
                """,
                raw_upload_id,
                content_bytes,
                uploaded_ttl_sec,
            )

        return {
            "file_id": raw_upload_id,
            "upload_id": raw_upload_id,
            "status": "uploaded",
        }

    async def resolve_uploaded_for_inference(
        self,
        *,
        user_id: str,
        file_ids: list[str],
    ) -> list[dict[str, Any]]:
        requested_ids: list[str] = []
        seen: set[str] = set()
        for candidate in list(file_ids or []):
            token = str(candidate or "").strip()
            if not token or token in seen:
                continue
            seen.add(token)
            requested_ids.append(token)

        if not requested_ids:
            return []
        if len(requested_ids) > self._max_attachments_per_turn():
            raise FileUploadError(
                400,
                "too_many_attachments",
                f"Attachment count exceeds max {self._max_attachments_per_turn()}",
            )

        out: list[dict[str, Any]] = []
        async with self.db.transaction() as tx:
            await self._cleanup_expired(tx)
            for file_id in requested_ids:
                row = await tx.fetchrow(
                    """
                    SELECT id, user_id, filename, mime_type, size_bytes, status, content_bytes, expires_at
                    FROM chat_file_uploads
                    WHERE id = $1::uuid AND user_id = $2::uuid
                    FOR UPDATE
                    """,
                    file_id,
                    user_id,
                )
                if not row:
                    raise FileUploadError(404, "file_not_found", f"file_id not found: {file_id}")

                status = str(self._row_get(row, "status") or "")
                if status != "uploaded":
                    raise FileUploadError(409, "file_not_uploaded", f"file_id not uploaded: {file_id}")

                expires_at = self._row_get(row, "expires_at")
                if isinstance(expires_at, datetime):
                    ts = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
                    if ts <= self._now():
                        await tx.execute(
                            """
                            UPDATE chat_file_uploads
                            SET status = 'expired', updated_at = now()
                            WHERE id = $1::uuid
                            """,
                            file_id,
                        )
                        raise FileUploadError(410, "file_expired", f"file_id expired: {file_id}")

                payload = self._row_get(row, "content_bytes")
                if payload is None:
                    raise FileUploadError(409, "file_content_missing", f"file_id has no content: {file_id}")
                if isinstance(payload, memoryview):
                    content_bytes = payload.tobytes()
                elif isinstance(payload, bytearray):
                    content_bytes = bytes(payload)
                elif isinstance(payload, bytes):
                    content_bytes = payload
                else:
                    raise FileUploadError(409, "file_content_invalid", f"file_id content invalid: {file_id}")

                out.append(
                    {
                        "file_id": str(self._row_get(row, "id") or file_id),
                        "filename": str(self._row_get(row, "filename") or ""),
                        "mime_type": str(self._row_get(row, "mime_type") or ""),
                        "size_bytes": int(self._row_get(row, "size_bytes") or 0),
                        "content_bytes": content_bytes,
                    }
                )
        return out

    async def search_uploaded_text(
        self,
        *,
        user_id: str,
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        token = str(query or "").strip().lower()
        if not token:
            return []
        capped_limit = max(1, min(20, int(limit)))

        rows = await self.db.fetch(
            """
            SELECT id, filename, mime_type, size_bytes, content_bytes
            FROM chat_file_uploads
            WHERE user_id = $1::uuid
              AND status = 'uploaded'
              AND expires_at > now()
            ORDER BY updated_at DESC
            LIMIT 200
            """,
            user_id,
        )
        hits: list[dict[str, Any]] = []
        for row in rows:
            mime_type = str(self._row_get(row, "mime_type") or "").strip().lower()
            payload = self._row_get(row, "content_bytes")
            if payload is None:
                continue
            if isinstance(payload, memoryview):
                content = payload.tobytes()
            elif isinstance(payload, bytearray):
                content = bytes(payload)
            elif isinstance(payload, bytes):
                content = payload
            else:
                continue

            text = self._decode_text_content(content, mime_type=mime_type)
            if not text:
                continue
            text_lc = text.lower()
            idx = text_lc.find(token)
            if idx < 0:
                continue
            start = max(0, idx - 120)
            end = min(len(text), idx + len(token) + 120)
            snippet = text[start:end].strip()
            hits.append(
                {
                    "file_id": str(self._row_get(row, "id") or ""),
                    "filename": str(self._row_get(row, "filename") or ""),
                    "mime_type": mime_type,
                    "size_bytes": int(self._row_get(row, "size_bytes") or 0),
                    "snippet": snippet,
                }
            )
            if len(hits) >= capped_limit:
                break
        return hits
