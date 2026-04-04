"""Provider-scoped thread persistence for inference context."""

from __future__ import annotations

import json
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


def _is_plain_assistant_content(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.startswith("{") or stripped.startswith("["):
        return False

    lowered = stripped.lower()
    blocked_markers = (
        '"tool_name"',
        "'tool_name'",
        '"arguments"',
        "'arguments'",
        '"observation"',
        "'observation'",
        '"tool_calls"',
        "'tool_calls'",
        '"function_call"',
        "'function_call'",
        '"result_status"',
        "'result_status'",
        '"shell_mode"',
        "'shell_mode'",
        '"timeout_sec"',
        "'timeout_sec'",
    )
    if any(marker in lowered for marker in blocked_markers):
        return False
    if stripped.startswith("```json") or stripped.startswith("```tool") or stripped.startswith("```"):
        return False

    xml_trace_markers = (
        "<tool",
        "</tool",
        "<observation",
        "</observation",
        "<trace",
        "</trace",
        "<function_call",
        "</function_call",
    )
    if any(marker in lowered for marker in xml_trace_markers):
        return False
    return True


def _extract_text_content(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("text") or "")
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = json.loads(raw)
            except Exception:
                return raw
            if isinstance(parsed, dict):
                return str(parsed.get("text") or "")
            if isinstance(parsed, str):
                return parsed
            return ""
        return raw
    return ""


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

    @staticmethod
    def _max_context_messages() -> int:
        try:
            raw = int(os.getenv("OPENVEGAS_CONTEXT_MAX_MESSAGES", "200"))
        except Exception:
            raw = 200
        return max(20, raw)

    @staticmethod
    def _compaction_enabled() -> bool:
        return str(os.getenv("OPENVEGAS_CONTEXT_COMPACTION_ENABLED", "1")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _compaction_trigger_messages() -> int:
        try:
            raw = int(os.getenv("OPENVEGAS_CONTEXT_COMPACTION_TRIGGER_MESSAGES", "120"))
        except Exception:
            raw = 120
        return max(5, min(raw, 2000))

    @staticmethod
    def _compaction_keep_recent_messages() -> int:
        try:
            raw = int(os.getenv("OPENVEGAS_CONTEXT_COMPACTION_KEEP_RECENT_MESSAGES", "60"))
        except Exception:
            raw = 60
        return max(1, min(raw, 1000))

    @staticmethod
    def _compaction_max_summary_chars() -> int:
        try:
            raw = int(os.getenv("OPENVEGAS_CONTEXT_COMPACTION_MAX_SUMMARY_CHARS", "6000"))
        except Exception:
            raw = 6000
        return max(512, min(raw, 50000))

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
                        VALUES ($1::uuid, $2::uuid, $3, $4, 'persistent', $5::uuid, now() + make_interval(hours => $6::int), now(), now())
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
                VALUES ($1::uuid, $2::uuid, $3, $4, 'persistent', now() + make_interval(hours => $5::int), now(), now())
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
            await self._maybe_compact_thread(tx, thread_ctx.thread_id)
            await self._truncate_messages(tx, thread_ctx.thread_id)

    async def _truncate_messages(self, tx: Any, thread_id: str) -> None:
        max_messages = self._max_context_messages()
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
    def _is_summary_message(text: str) -> bool:
        return str(text or "").strip().startswith("conversation_summary_v1")

    def _build_compaction_summary(self, rows: list[dict[str, Any]]) -> str:
        lines: list[str] = ["conversation_summary_v1", "Earlier context summary:"]
        max_chars = self._compaction_max_summary_chars()
        used = len("\n".join(lines))

        for row in rows:
            role = str(row.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            text = _extract_text_content(row.get("content"))
            if not text.strip():
                continue
            if role == "assistant":
                if not _is_plain_assistant_content(text):
                    continue
                if self._is_summary_message(text):
                    continue
            compact = " ".join(str(text).split())
            if not compact:
                continue
            if len(compact) > 220:
                compact = compact[:220].rstrip() + "..."
            prefix = "User" if role == "user" else "Assistant"
            line = f"- {prefix}: {compact}"
            next_used = used + len(line) + 1
            if next_used > max_chars:
                break
            lines.append(line)
            used = next_used

        if len(lines) <= 2:
            return "conversation_summary_v1\nEarlier context summary unavailable."
        return "\n".join(lines)

    async def _maybe_compact_thread(self, tx: Any, thread_id: str) -> None:
        if not self._compaction_enabled():
            return
        trigger = self._compaction_trigger_messages()
        keep_recent = self._compaction_keep_recent_messages()
        if keep_recent >= trigger:
            keep_recent = max(1, trigger // 2)

        total_messages = await tx.fetchval(
            """
            SELECT COUNT(*)::int
            FROM provider_thread_messages
            WHERE thread_id = $1::uuid
            """,
            thread_id,
        )
        total = int(total_messages or 0)
        if total <= trigger:
            return
        compact_count = max(0, total - keep_recent)
        if compact_count <= 0:
            return

        rows = await tx.fetch(
            """
            SELECT id, role, content
            FROM provider_thread_messages
            WHERE thread_id = $1::uuid
            ORDER BY created_at ASC, id ASC
            LIMIT $2
            """,
            thread_id,
            compact_count,
        )
        if not rows:
            return

        ids: list[str] = [str(r.get("id")) for r in rows if r.get("id")]
        if not ids:
            return
        summary_text = self._build_compaction_summary(rows)
        summary_payload = json.dumps({"text": summary_text}, ensure_ascii=False, separators=(",", ":"))
        token_count = max(1, min(10000, len(summary_text) // 4))

        await tx.execute(
            """
            DELETE FROM provider_thread_messages
            WHERE id = ANY($1::uuid[])
            """,
            ids,
        )
        await tx.execute(
            """
            INSERT INTO provider_thread_messages (thread_id, role, content, token_count)
            VALUES ($1::uuid, 'assistant', $2::jsonb, $3::int)
            """,
            thread_id,
            summary_payload,
            token_count,
        )

    async def get_recent_messages_with_stats(
        self,
        *,
        thread_id: str,
        limit: int = 200,
    ) -> tuple[list[dict[str, str]], int, int]:
        if not self.context_enabled():
            return [], 0, 0
        if not thread_id:
            return [], 0, 0

        try:
            requested_limit = int(limit)
        except Exception:
            requested_limit = 200
        cap = max(1, min(requested_limit, self._max_context_messages()))

        rows = await self.db.fetch(
            """
            SELECT role, content
            FROM provider_thread_messages
            WHERE thread_id = $1::uuid
            ORDER BY created_at DESC, id DESC
            LIMIT $2
            """,
            thread_id,
            cap,
        )
        loaded = len(rows)
        out: list[dict[str, str]] = []

        for row in reversed(rows):
            role = str(row.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            text = _extract_text_content(row.get("content"))
            if not text.strip():
                continue
            if role == "assistant" and not _is_plain_assistant_content(text):
                continue
            out.append({"role": role, "content": text})

        skipped = max(0, loaded - len(out))
        return out, loaded, skipped

    async def get_recent_messages(
        self,
        *,
        thread_id: str,
        limit: int = 200,
    ) -> list[dict[str, str]]:
        messages, _loaded, _skipped = await self.get_recent_messages_with_stats(
            thread_id=thread_id,
            limit=limit,
        )
        return messages

    @staticmethod
    def _expired(expires_at: datetime) -> bool:
        ts = expires_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts <= datetime.now(timezone.utc)
