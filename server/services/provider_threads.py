"""Provider-scoped thread persistence for inference context."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from openvegas.contracts.enums import ConversationMode
from openvegas.contracts.errors import APIErrorCode, ContractError


@dataclass(frozen=True)
class ThreadContext:
    thread_id: str | None
    thread_status: str
    conversation_mode: ConversationMode


class ProviderThreadService:
    def __init__(self, db: Any):
        self.db = db

    @staticmethod
    def context_enabled() -> bool:
        return os.getenv("OPENVEGAS_CONTEXT_ENABLED", "0") == "1"

    @staticmethod
    def _thread_ttl_hours() -> int:
        raw = int(os.getenv("OPENVEGAS_CONTEXT_TTL_HOURS", "72"))
        return max(1, raw)

    async def prepare_thread(
        self,
        *,
        user_id: str,
        provider: str,
        model_id: str,
        thread_id: str | None,
        conversation_mode: str | None,
    ) -> ThreadContext:
        mode_raw = (conversation_mode or ConversationMode.PERSISTENT.value).strip().lower()
        if mode_raw not in {ConversationMode.PERSISTENT.value, ConversationMode.EPHEMERAL.value}:
            mode_raw = ConversationMode.PERSISTENT.value
        mode = ConversationMode(mode_raw)

        if not self.context_enabled():
            return ThreadContext(
                thread_id=None,
                thread_status="disabled",
                conversation_mode=mode,
            )
        if mode == ConversationMode.EPHEMERAL:
            # Privacy invariant: ephemeral mode never persists thread messages.
            return ThreadContext(
                thread_id=None,
                thread_status="ephemeral",
                conversation_mode=mode,
            )

        async with self.db.transaction() as tx:
            if thread_id:
                try:
                    _ = uuid.UUID(str(thread_id))
                except Exception as exc:
                    raise ContractError(
                        APIErrorCode.PROVIDER_THREAD_MISMATCH,
                        "Thread belongs to a different provider.",
                    ) from exc
                row = await tx.fetchrow(
                    """
                    SELECT id, provider, expires_at
                    FROM provider_threads
                    WHERE id = $1::uuid
                      AND user_id = $2::uuid
                    FOR UPDATE
                    """,
                    thread_id,
                    user_id,
                )
                if not row:
                    raise ContractError(
                        APIErrorCode.PROVIDER_THREAD_MISMATCH,
                        "Thread not found for user/provider scope.",
                    )
                if str(row["provider"]) != provider:
                    raise ContractError(
                        APIErrorCode.PROVIDER_THREAD_MISMATCH,
                        "Thread belongs to a different provider.",
                    )
                expires_at = row.get("expires_at")
                if expires_at is not None and self._expired(expires_at):
                    new_id = str(uuid.uuid4())
                    await tx.execute(
                        """
                        INSERT INTO provider_threads
                          (id, user_id, provider, model_id, conversation_mode, thread_forked_from, expires_at, last_used_at, updated_at)
                        VALUES ($1::uuid, $2::uuid, $3, $4, 'persistent', $5::uuid, now() + ($6 || ' hours')::interval, now(), now())
                        """,
                        new_id,
                        user_id,
                        provider,
                        model_id,
                        str(row["id"]),
                        self._thread_ttl_hours(),
                    )
                    return ThreadContext(
                        thread_id=new_id,
                        thread_status=APIErrorCode.THREAD_EXPIRED_RESTARTED.value,
                        conversation_mode=mode,
                    )

                await tx.execute(
                    """
                    UPDATE provider_threads
                    SET model_id = $2, last_used_at = now(), updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    thread_id,
                    model_id,
                )
                return ThreadContext(
                    thread_id=thread_id,
                    thread_status="existing",
                    conversation_mode=mode,
                )

            new_id = str(uuid.uuid4())
            await tx.execute(
                """
                INSERT INTO provider_threads
                  (id, user_id, provider, model_id, conversation_mode, expires_at, last_used_at, updated_at)
                VALUES ($1::uuid, $2::uuid, $3, $4, 'persistent', now() + ($5 || ' hours')::interval, now(), now())
                """,
                new_id,
                user_id,
                provider,
                model_id,
                self._thread_ttl_hours(),
            )
            return ThreadContext(
                thread_id=new_id,
                thread_status="created",
                conversation_mode=mode,
            )

    async def append_exchange(
        self,
        *,
        thread_ctx: ThreadContext,
        prompt: str,
        response_text: str,
        input_tokens: int,
        output_tokens: int,
        persist_context: bool,
    ) -> None:
        import json

        if not self.context_enabled():
            return
        if thread_ctx.conversation_mode == ConversationMode.EPHEMERAL:
            return
        if not persist_context:
            return
        if not thread_ctx.thread_id:
            return

        async with self.db.transaction() as tx:
            await tx.execute(
                """
                INSERT INTO provider_thread_messages (thread_id, role, content, token_count)
                VALUES
                  ($1::uuid, 'user', $2::jsonb, NULL),
                  ($1::uuid, 'assistant', $3::jsonb, $4)
                """,
                thread_ctx.thread_id,
                json.dumps({"text": prompt}, ensure_ascii=False, separators=(",", ":")),
                json.dumps({"text": response_text}, ensure_ascii=False, separators=(",", ":")),
                max(input_tokens + output_tokens, 0),
            )
            await tx.execute(
                """
                UPDATE provider_threads
                SET last_used_at = now(), updated_at = now()
                WHERE id = $1::uuid
                """,
                thread_ctx.thread_id,
            )
            await self._truncate_messages(tx, thread_ctx.thread_id)

    async def _truncate_messages(self, tx: Any, thread_id: str) -> None:
        max_messages = max(20, int(os.getenv("OPENVEGAS_CONTEXT_MAX_MESSAGES", "200")))
        await tx.execute(
            """
            DELETE FROM provider_thread_messages
            WHERE id IN (
              SELECT id
              FROM provider_thread_messages
              WHERE thread_id = $1::uuid
              ORDER BY created_at DESC
              OFFSET $2
            )
            """,
            thread_id,
            max_messages,
        )

    @staticmethod
    def _expired(expires_at: datetime) -> bool:
        ts = expires_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts <= datetime.now(timezone.utc)
