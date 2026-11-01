"""Opt-in metadata inventory. No prompts, files, recordings or sale permissions."""

from __future__ import annotations

import os
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

NOTICE_VERSION = "metadata-pilot-2026-10-20"
NOTICE = (
    "Optional OpenVegas research metadata pilot. Submit only task categories, host/model "
    "families, language, duration bucket and your reported outcome/corrections. We keep "
    "these account-linked records for up to 30 days in the active database. This is not "
    "anonymous data. Do not submit employer/client work, prompts, code, files, personal "
    "information or terminal recordings. This permission does not permit selling your "
    "data or training models on your content. Participation earns no credits and is not "
    "required to use OpenVegas. Withdraw to stop and delete pilot records. Backup expiry "
    "follows the deployment's disclosed backup schedule. Adults only."
)


class ContributionError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(detail)


def enabled():
    return os.getenv("OPENVEGAS_DATA_CONTRIBUTIONS_ENABLED", "0") == "1"


class Contribution(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    submission_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    host: Literal["openvegas", "claude", "codex"]
    model_family: Literal["openai", "anthropic", "google", "mistral", "other", "unknown"]
    task_type: Literal["bugfix", "feature", "refactor", "tests", "documentation", "other"]
    language: Literal["python", "javascript", "typescript", "rust", "go", "java", "other", "unknown"]
    outcome: Literal["accepted", "rejected", "abandoned", "unknown"]
    test_result: Literal["passed", "failed", "not_run", "unknown"]
    correction_count: int = Field(ge=0, le=100)
    duration_bucket: Literal["under_1m", "1_to_5m", "5_to_30m", "over_30m", "unknown"]


FIELDS = ("host", "model_family", "task_type", "language", "outcome", "test_result",
          "correction_count", "duration_bucket")


class ContributionService:
    def __init__(self, db):
        self.db = db

    async def _lock(self, tx, user):
        await tx.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                         "ov-data-contributions:" + str(user))

    async def _cohort(self, tx, user):
        # Conservative: any organizational affiliation blocks the consumer pilot.
        # Membership never grants a licence to employer/client content.
        member = await tx.fetchval("SELECT EXISTS(SELECT 1 FROM org_members WHERE user_id=$1)", user)
        if not isinstance(member, bool):
            raise ContributionError(503, "Account classification unavailable.")
        return "enterprise" if member else "consumer"

    async def _account(self, tx, user):
        valid = await tx.fetchval("""SELECT EXISTS(SELECT 1 FROM auth.users WHERE id=$1
            AND email_confirmed_at IS NOT NULL AND is_anonymous IS NOT TRUE
            AND deleted_at IS NULL AND (banned_until IS NULL OR banned_until<=now()))""", user)
        if valid is not True:
            raise ContributionError(403, "A verified active account is required.")

    async def preference(self, user, opt_in: bool):
        user = UUID(str(user))
        if opt_in and not enabled():
            raise ContributionError(404, "Contribution pilot is disabled.")
        async with self.db.transaction() as tx:
            await self._lock(tx, user)
            if opt_in:
                await self._account(tx, user)
                if await self._cohort(tx, user) != "consumer":
                    raise ContributionError(403, "Enterprise contributions require a separate agreement; pilot unavailable.")
            row = await tx.fetchrow("""INSERT INTO data_contribution_preferences
                (user_id,opted_in,notice_version) VALUES($1,$2,$3)
                ON CONFLICT(user_id) DO UPDATE SET opted_in=excluded.opted_in,
                notice_version=excluded.notice_version, updated_at=clock_timestamp()
                RETURNING opted_in""", user, opt_in, NOTICE_VERSION)
            if row is None:
                raise ContributionError(503, "Contribution storage unavailable.")
            if not opt_in:
                await tx.execute("DELETE FROM data_contribution_inventory WHERE user_id=$1", user)
        return {"opted_in": opt_in, "records_deleted": not opt_in, "sale_authorized": False}

    async def status(self, user):
        user = UUID(str(user))
        async with self.db.transaction() as tx:
            await self._lock(tx, user)
            cohort = await self._cohort(tx, user)
            pref = await tx.fetchrow("SELECT opted_in,notice_version FROM data_contribution_preferences WHERE user_id=$1", user)
            rows = await tx.fetch("""SELECT outcome,count(*) AS count FROM data_contribution_inventory
                WHERE user_id=$1 AND expires_at>now() GROUP BY outcome""", user)
        return {"cohort": cohort, "opted_in": bool(pref and pref["opted_in"] and
                    pref["notice_version"] == NOTICE_VERSION),
                "collection_available": enabled() and cohort == "consumer",
                "reported_outcomes": {r["outcome"]: r["count"] for r in rows},
                "evidence_kind": "participant_reported_unverified", "sale_authorized": False,
                "content_collection": False, "retention_days": 30}

    async def submit(self, user, value: Contribution):
        if not enabled():
            raise ContributionError(404, "Contribution pilot is disabled.")
        user, sid = UUID(str(user)), UUID(value.submission_id)
        async with self.db.transaction() as tx:
            await self._lock(tx, user)
            await self._account(tx, user)
            if await self._cohort(tx, user) != "consumer":
                raise ContributionError(403, "Enterprise pilot unavailable.")
            pref = await tx.fetchrow("SELECT opted_in,notice_version FROM data_contribution_preferences WHERE user_id=$1", user)
            if not pref or not pref["opted_in"] or pref["notice_version"] != NOTICE_VERSION:
                raise ContributionError(403, "Current explicit metadata consent is required.")
            await tx.execute("DELETE FROM data_contribution_inventory WHERE user_id=$1 AND expires_at<=now()", user)
            old = await tx.fetchrow("SELECT * FROM data_contribution_inventory WHERE user_id=$1 AND submission_id=$2", user, sid)
            if old:
                if any(old[k] != getattr(value, k) for k in FIELDS):
                    raise ContributionError(409, "Submission ID already has different metadata.")
                return {"stored": True, "replayed": True, "sale_authorized": False}
            count = await tx.fetchval("SELECT count(*) FROM data_contribution_inventory WHERE user_id=$1", user)
            if not isinstance(count, int):
                raise ContributionError(503, "Contribution storage unavailable.")
            if count >= 100:
                raise ContributionError(429, "Pilot limit is 100 retained records per account.")
            await tx.execute("""INSERT INTO data_contribution_inventory
                (user_id,submission_id,host,model_family,task_type,language,outcome,test_result,
                 correction_count,duration_bucket,notice_version)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
                user, sid, *(getattr(value, k) for k in FIELDS), NOTICE_VERSION)
        return {"stored": True, "replayed": False, "sale_authorized": False}

    async def purge_expired(self):
        # Explicit operator job; bounded batches avoid long deletion transactions.
        return await self.db.fetchval("""WITH expired AS (
            SELECT user_id,submission_id FROM data_contribution_inventory WHERE expires_at<=now()
            ORDER BY expires_at LIMIT 5000 FOR UPDATE SKIP LOCKED
        ), deleted AS (DELETE FROM data_contribution_inventory d USING expired e
            WHERE d.user_id=e.user_id AND d.submission_id=e.submission_id RETURNING 1)
        SELECT count(*) FROM deleted""")

