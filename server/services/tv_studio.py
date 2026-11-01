"""Review-only creator intake. Reuses the server database; never publishes media."""

from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

DOCUMENT_MAX_BYTES = 1_000_000
_validation_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tv-validation")
_validation_slots = BoundedSemaphore(2)
SUMMARY_COLUMNS = "id,title,channel_id,status,created_at,review_note"


async def validated_call(function, *args):
    # A cancelled await cannot release a running thread's slot or grow a work queue.
    slots = _validation_slots
    if not slots.acquire(blocking=False):
        raise IntakeError(429, "TV validation is busy; retry later.")
    try:
        future = _validation_executor.submit(function, *args)
    except BaseException:
        slots.release()
        raise
    future.add_done_callback(lambda _: slots.release())
    return await asyncio.wrap_future(future)


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: UUID
    title: str = Field(min_length=3, max_length=100)
    channel_id: str = Field(min_length=1, max_length=48, pattern=r"^[a-z0-9-]+$")
    pitch: str = Field(min_length=30, max_length=4000)
    rights_confirmed: StrictBool
    episode: dict | None = None

    @field_validator("rights_confirmed")
    @classmethod
    def rights(cls, value):
        if value is not True:
            raise ValueError("Rights declaration required")
        return value

    @field_validator("title", "pitch")
    @classmethod
    def plain_text(cls, value):
        if not value.strip() or any(
            unicodedata.category(char).startswith("C") and char not in "\n\t" for char in value
        ):
            raise ValueError("Use plain text without control characters")
        return value


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["shortlisted", "needs_changes", "rejected"]
    note: str = Field(min_length=1, max_length=1000)

    @field_validator("note")
    @classmethod
    def plain_text(cls, value):
        return Submission.plain_text(value)


class IntakeError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def normalized(payload: Submission) -> tuple[dict, str, str]:
    from openvegas.tv.catalog import catalog
    from openvegas.tv.schema import validate_episode

    value = payload.model_dump(mode="json", exclude={"idempotency_key"})
    if value["channel_id"] not in {row["id"] for row in catalog()["channels"]}:
        raise IntakeError(422, "Choose an existing channel.")
    if value["episode"] is not None:
        try:
            value["episode"] = validate_episode(value["episode"])
        except (ValueError, TypeError):
            raise IntakeError(422, "Episode does not meet the studio format.") from None
        if value["episode"]["channel_id"] != value["channel_id"]:
            raise IntakeError(422, "The episode and pitch must use the same channel.")
    raw = json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    if len(raw.encode("utf-8")) > DOCUMENT_MAX_BYTES:
        raise IntakeError(413, "Application exceeds 1 MB.")
    return value, raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def public_row(row, *, include_document=False):
    result = {
        "id": str(row["id"]),
        "title": row["title"],
        "channel_id": row["channel_id"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat(),
        "review_note": row["review_note"],
        "published": False,
    }
    if include_document:
        raw = row["document_json"]
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > DOCUMENT_MAX_BYTES:
            raise IntakeError(503, "Application document is unavailable.")
        result["application"] = json.loads(raw)
    return result


class StudioIntake:
    def __init__(self, db):
        self.db = db

    async def submit(self, user_id: str, payload: Submission):
        value, raw, fingerprint = await validated_call(normalized, payload)
        user = UUID(user_id)
        key = payload.idempotency_key
        async with self.db.transaction() as tx:
            # Serialize only this applicant, covering quota and idempotency races.
            await tx.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", str(user))
            row = await tx.fetchrow(
                f"SELECT {SUMMARY_COLUMNS},payload_sha256 FROM public.tv_applications WHERE user_id=$1 AND idempotency_key=$2",
                user,
                key,
            )
            if row:
                if row["payload_sha256"] != fingerprint:
                    raise IntakeError(409, "This request key belongs to a different application.")
                return {**public_row(row), "replayed": True}
            count = await tx.fetchval(
                "SELECT count(*) FROM public.tv_applications WHERE user_id=$1 "
                "AND created_at > now() - interval '24 hours'",
                user,
            )
            if count is None:
                raise IntakeError(503, "Creator database is unavailable.")
            if count >= 5:
                raise IntakeError(429, "Five applications per day; try again later.")
            row = await tx.fetchrow(
                "INSERT INTO public.tv_applications "
                "(user_id,idempotency_key,payload_sha256,title,channel_id,document_json) "
                f"VALUES ($1,$2,$3,$4,$5,$6) RETURNING {SUMMARY_COLUMNS}",
                user,
                key,
                fingerprint,
                value["title"],
                value["channel_id"],
                raw,
            )
            if not row:
                raise IntakeError(503, "Application was not saved.")
            return {**public_row(row), "replayed": False}

    async def mine(self, user_id: str):
        rows = await self.db.fetch(
            f"SELECT {SUMMARY_COLUMNS} FROM public.tv_applications WHERE user_id=$1 "
            "ORDER BY created_at DESC LIMIT 50",
            UUID(user_id),
        )
        return [public_row(row) for row in rows]

    async def pending(self):
        rows = await self.db.fetch(
            f"SELECT {SUMMARY_COLUMNS} FROM public.tv_applications WHERE status='submitted' "
            "ORDER BY created_at LIMIT 50"
        )
        return [public_row(row) for row in rows]

    async def detail(self, submission_id: UUID, *, user_id: str | None):
        query = f"SELECT {SUMMARY_COLUMNS},document_json FROM public.tv_applications WHERE id=$1"
        args = [submission_id]
        if user_id is not None:
            query += " AND user_id=$2"
            args.append(UUID(user_id))
        row = await self.db.fetchrow(query, *args)
        if not row:
            raise IntakeError(404, "Application is not available.")
        return public_row(row, include_document=True)

    async def review(self, submission_id: UUID, admin_id: str, payload: Review):
        row = await self.db.fetchrow(
            "UPDATE public.tv_applications SET status=$2,review_note=$3,reviewer_id=$4,"
            f"reviewed_at=now() WHERE id=$1 AND status='submitted' RETURNING {SUMMARY_COLUMNS}",
            submission_id,
            payload.decision,
            payload.note,
            UUID(admin_id),
        )
        if not row:
            raise IntakeError(409, "Application is absent or has already been reviewed.")
        return public_row(row)
