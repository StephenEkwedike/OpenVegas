"""Provider-scoped thread persistence for inference context."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from openvegas.contracts.enums import ConversationMode
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.catalog import ProviderCatalog
from openvegas.gateway.conversation import (
    ATTACHMENT_WARNING,
    TOOL_WARNING,
    CanonicalConversation,
    ContinuityError,
)
from openvegas.gateway.providers import get_model_review, model_switch_enabled
from openvegas.gateway.switching import ModelSwitchPlan, plan_context_transfer


@dataclass(frozen=True)
class ThreadContext:
    thread_id: str | None
    thread_status: str
    conversation_mode: ConversationMode


def _is_plain_assistant_content(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.startswith(("{", "[")):
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
    if stripped.startswith("```"):
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
    return not any(marker in lowered for marker in xml_trace_markers)


def _extract_text_content(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("text") or "")
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
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
    def _continuity_scope(user_id: str, thread_id: str | None = None) -> None:
        try:
            uuid.UUID(str(user_id))
            if thread_id is not None:
                uuid.UUID(str(thread_id))
        except (ValueError, TypeError, AttributeError):
            raise ContractError(
                APIErrorCode.PROVIDER_THREAD_MISMATCH, "Invalid thread/user scope."
            ) from None

    async def _canonical_locked(
        self, tx: Any, user_id: str, thread_id: str, *, expected_pending: str | None = None,
    ):
        row = await tx.fetchrow(
            "SELECT id, provider, model_id, expires_at FROM provider_threads "
            "WHERE id = $1::uuid AND user_id = $2::uuid FOR UPDATE",
            thread_id, user_id,
        )
        if not row:
            raise ContractError(
                APIErrorCode.PROVIDER_THREAD_MISMATCH, "Thread not found for user scope."
            )
        if row.get("expires_at") is not None and self._expired(row["expires_at"]):
            raise ContinuityError("Source thread expired; explicitly start a new conversation.")
        records = await tx.fetch(
            "SELECT id, role, content FROM provider_thread_messages "
            "WHERE thread_id = $1::uuid ORDER BY created_at ASC, id ASC LIMIT 2",
            thread_id,
        )
        if len(records) != 1 or records[0]["role"] != "system":
            raise ContinuityError("Legacy history may be incomplete; explicitly start fresh.")
        content = records[0]["content"]
        if expected_pending is not None:
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except ValueError:
                    raise ContinuityError("Invalid canonical turn state.") from None
            if not isinstance(content, dict) or content.get("pending") != expected_pending:
                raise ContinuityError("Canonical turn changed; do not retry blindly.")
            content = {k: v for k, v in content.items() if k != "pending"}
        return row, records[0], CanonicalConversation.from_storage(content)

    async def _mark_canonical_pending(
        self, *, user_id: str, thread_id: str, canonical: CanonicalConversation,
        request_key: str, restore: bool = False,
    ) -> None:
        async with self.db.transaction() as tx:
            _, record, current = await self._canonical_locked(
                tx, user_id, thread_id, expected_pending=request_key if restore else None,
            )
            if current.revision != canonical.revision:
                raise ContinuityError("Canonical turn changed; no inference was started.")
            content = json.loads(canonical.to_json())
            if not restore:
                content["pending"] = request_key
            await tx.execute(
                "UPDATE provider_thread_messages SET content = $2::jsonb "
                "WHERE id = $1::uuid AND thread_id = $3::uuid",
                str(record["id"]), json.dumps(content), thread_id,
            )

    async def create_canonical_thread(
        self, *, user_id: str, provider: str, model_id: str, catalog: ProviderCatalog,
        max_output_tokens: int = 1024, required_capabilities: list[str] | None = None,
    ) -> ModelSwitchPlan:
        """Explicit fresh start. Never imports client roles, provider IDs or credentials."""
        self._continuity_scope(user_id)
        if not model_switch_enabled() or not self.context_enabled():
            raise ContinuityError("Model switching and persistent server context must be enabled.")
        canonical = CanonicalConversation()
        async with self.db.transaction() as tx:
            target = await self._validate_canonical_target(
                tx, catalog, provider, model_id, required_capabilities, max_output_tokens,
            )
            canonical.validate_target(target, max_output_tokens)
            thread_id = str(uuid.uuid4())
            await self._insert_canonical(tx, user_id, thread_id, target, canonical, None)
        return ModelSwitchPlan(
            "ready", "Fresh canonical text-only conversation created. " + ATTACHMENT_WARNING,
            history_scope="canonical_text_v1", requires_confirmation=False,
            revision=canonical.revision, thread_id=thread_id, requires_new_thread=False,
        )

    async def _validate_canonical_target(
        self, tx: Any, catalog: ProviderCatalog, provider: str, model_id: str,
        required_capabilities: list[str] | None, max_output_tokens: int,
    ) -> dict:
        # Hold the catalog row against a concurrent operator disable until commit.
        await tx.fetchrow(
            "SELECT model_id FROM provider_catalog WHERE provider = $1 AND model_id = $2 FOR SHARE",
            provider, model_id,
        )
        # Bind validation to this transaction, including managed credential lookup.
        if not isinstance(catalog, ProviderCatalog):
            raise TypeError("A server ProviderCatalog is required.")
        target = await ProviderCatalog(tx).validate_selection(
            provider, model_id, required_capabilities=required_capabilities,
            max_tokens=max_output_tokens,
        )
        review = get_model_review(provider, model_id)
        if review.get("account_access") is not True or review.get("completion_chat") is not True:
            raise ContinuityError("Canonical continuity requires an exact-model managed account access review.")
        return target

    async def _insert_canonical(
        self, tx: Any, user_id: str, thread_id: str, target: dict,
        canonical: CanonicalConversation, source_thread_id: str | None,
    ) -> None:
        try:
            await tx.execute(
                "INSERT INTO provider_threads "
                "(id, user_id, provider, model_id, conversation_mode, thread_forked_from, expires_at) "
                "VALUES ($1::uuid, $2::uuid, $3, $4, 'persistent', $5::uuid, "
                "now() + make_interval(hours => $6::int))",
                thread_id, user_id, target["provider"], target["model_id"], source_thread_id,
                self._thread_ttl_hours(),
            )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23514":
                raise ContinuityError(
                    "Provider thread schema does not support this target; operator migration required."
                ) from None
            raise
        await tx.execute(
            "INSERT INTO provider_thread_messages (thread_id, role, content) "
            "VALUES ($1::uuid, 'system', $2::jsonb)", thread_id, canonical.to_json(),
        )

    async def canonical_switch(
        self, *, user_id: str, thread_id: str, provider: str, model_id: str,
        catalog: ProviderCatalog, expected_revision: str | None = None,
        commit: bool = False, max_output_tokens: int = 1024,
        required_capabilities: list[str] | None = None,
    ) -> ModelSwitchPlan:
        """Scoped compare-and-fork; no paid call, source mutation, or partial copying.

        Preflight exposes only a revision, never the transcript. Commit revalidates
        catalog access and the full source under the same lock as inference.
        Retrying a lost commit may create an unused fork, but never incurs billing.
        """
        if not model_switch_enabled() or not self.context_enabled():
            raise ContinuityError("Model switching and persistent server context must be enabled.")
        self._continuity_scope(user_id, thread_id)
        async with self.db.transaction() as tx:
            _, _, canonical = await self._canonical_locked(tx, user_id, thread_id)
            target = await self._validate_canonical_target(
                tx, catalog, provider, model_id, required_capabilities, max_output_tokens,
            )
            canonical.validate_target(target, max_output_tokens)
            if commit and expected_revision != canonical.revision:
                raise ContinuityError("History changed; review a new switch plan before committing.")
            destination = None
            if commit:
                destination = str(uuid.uuid4())
                await self._insert_canonical(
                    tx, user_id, destination, target, canonical, thread_id,
                )
        return ModelSwitchPlan(
            "ready", "Complete canonical text retained; no tools replayed. " + ATTACHMENT_WARNING,
            history_scope="canonical_text_v1", context_transferred=commit,
            requires_confirmation=not commit, revision=canonical.revision, thread_id=destination,
            requires_new_thread=not commit,
        )

    async def canonical_history(
        self, *, user_id: str, thread_id: str, provider: str, model_id: str,
    ) -> CanonicalConversation | None:
        """Return None for legacy threads; never disclose history across tenants."""
        self._continuity_scope(user_id, thread_id)
        async with self.db.transaction() as tx:
            row = await tx.fetchrow(
                "SELECT id, provider, model_id, expires_at FROM provider_threads "
                "WHERE id = $1::uuid AND user_id = $2::uuid FOR UPDATE", thread_id, user_id,
            )
            if not row or row["provider"] != provider or row["model_id"] != model_id:
                raise ContractError(APIErrorCode.PROVIDER_THREAD_MISMATCH, "Thread scope mismatch.")
            records = await tx.fetch(
                "SELECT id, role, content FROM provider_thread_messages "
                "WHERE thread_id = $1::uuid ORDER BY created_at ASC, id ASC LIMIT 2", thread_id,
            )
            if not records or records[0]["role"] != "system":
                return None
            # A system metadata row is never treated as trusted provider instructions.
            _, _, canonical = await self._canonical_locked(tx, user_id, thread_id)
            return canonical

    async def append_canonical_exchange(
        self, *, user_id: str, thread_id: str, expected_revision: str,
        prompt: str, response_text: str, attachments: bool = False, tool_calls: bool = False,
        pending_key: str | None = None,
    ) -> None:
        """Compare-and-append after one completed logical turn; pending_key binds a reserved turn."""
        self._continuity_scope(user_id, thread_id)
        if attachments:
            raise ContinuityError(ATTACHMENT_WARNING)
        if tool_calls:
            raise ContinuityError(TOOL_WARNING)
        async with self.db.transaction() as tx:
            _, record, canonical = await self._canonical_locked(
                tx, user_id, thread_id, expected_pending=pending_key,
            )
            if expected_revision != canonical.revision:
                raise ContinuityError("History changed; exchange was not appended twice.")
            updated = canonical.append(prompt, response_text)
            await tx.execute(
                "UPDATE provider_thread_messages SET content = $2::jsonb "
                "WHERE id = $1::uuid AND thread_id = $3::uuid",
                str(record["id"]), updated.to_json(), thread_id,
            )
            await tx.execute(
                "UPDATE provider_threads SET last_used_at = now(), updated_at = now() "
                "WHERE id = $1::uuid AND user_id = $2::uuid", thread_id, user_id,
            )

    async def infer_canonical(
        self, *, user_id: str, thread_id: str, provider: str, model_id: str,
        expected_revision: str, prompt: str, idempotency_key: str,
        catalog: ProviderCatalog, gateway: Any, max_output_tokens: int = 1024,
    ) -> dict:
        """Exactly one gateway call with full roles; no retries, tools or summaries.

        Caller applies the normal auth, rate-limit and account-mode policy first.
        The existing gateway exclusively owns reservation, settlement and refunds.
        """
        from openvegas.gateway.catalog import ModelDisabled
        from openvegas.gateway.inference import InferenceRequest
        from openvegas.wallet.ledger import InsufficientBalance

        self._continuity_scope(user_id, idempotency_key)

        if "strict_continuity" not in InferenceRequest.__dataclass_fields__:
            raise ContinuityError("Canonical inference requires the reviewed gateway continuity patch.")

        if not self.context_enabled():
            raise ContinuityError("Persistent context is disabled; canonical inference was not run.")
        self._continuity_scope(user_id, thread_id)
        canonical = await self.canonical_history(
            user_id=user_id, thread_id=thread_id, provider=provider, model_id=model_id,
        )
        if canonical is None:
            raise ContinuityError("Start an explicit canonical conversation before using this endpoint.")
        if expected_revision != canonical.revision:
            raise ContinuityError("History changed or this turn already completed; do not retry blindly.")
        async with self.db.transaction() as tx:
            target = await self._validate_canonical_target(
                tx, catalog, provider, model_id, None, max_output_tokens,
            )
        canonical.validate_next_turn(prompt, target, max_output_tokens)
        # Durable intent means a crash or post-billing write failure cannot
        # silently unlock the old revision for a second paid continuation.
        await self._mark_canonical_pending(
            user_id=user_id, thread_id=thread_id, canonical=canonical,
            request_key=idempotency_key,
        )
        try:
            result = await gateway.infer(InferenceRequest(
                account_id=f"user:{user_id}", provider=provider, model=model_id,
                messages=canonical.messages() + [{"role": "user", "content": prompt}],
                max_tokens=max_output_tokens, idempotency_key=idempotency_key,
                enable_tools=False, enable_web_search=False,
                strict_continuity=True,
            ))
        except (ModelDisabled, InsufficientBalance):
            # These pre-provider failures cannot have settled a paid result.
            # Cancellation is deliberately NOT restored: it can race a committed
            # settlement whose acknowledgement never reached the caller.
            await self._mark_canonical_pending(
                user_id=user_id, thread_id=thread_id, canonical=canonical,
                request_key=idempotency_key, restore=True,
            )
            raise
        except Exception:  # noqa: BLE001 - An uncertain gateway outcome must never trigger a fresh paid retry.
            raise ContinuityError(
                "Inference outcome is unconfirmed. Continuity is blocked; do not retry blindly. "
                "Review billing and explicitly start fresh."
            ) from None
        warnings = []
        revision = None
        try:
            if getattr(result, "completion_status", "unknown") != "complete":
                raise ContinuityError("Truncated or unconfirmed output cannot complete a canonical turn.")
            if getattr(result, "web_search_used", False):
                raise ContinuityError("Unexpected tool use cannot complete a canonical text turn.")
            await self.append_canonical_exchange(
                user_id=user_id, thread_id=thread_id, expected_revision=canonical.revision,
                prompt=prompt, response_text=result.text, tool_calls=bool(result.tool_calls),
                pending_key=idempotency_key,
            )
            revision = canonical.append(prompt, result.text).revision
        except Exception:  # noqa: BLE001 - Preserve billed response; durable pending state blocks unsafe retries.
            # A paid response must not be mistaken for an unpaid retryable failure.
            # The durable pending marker preserves the last safe snapshot and
            # makes all future use fail closed, even if storage is unavailable.
            warnings.append(
                "Inference returned once but the response is incomplete or could not be retained safely. "
                "Continuity is now blocked; explicitly start fresh. No retry was made."
            )
        return {
            "text": result.text, "v_cost": str(result.v_cost),
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "thread_id": thread_id, "revision": revision,
            "continuity_blocked": bool(warnings), "warnings": warnings,
            "tool_calls": [], "context_enabled": True,
        }

    async def plan_model_switch(
        self, *, user_id: str, thread_id: str, provider: str, model_id: str,
        catalog: ProviderCatalog, active_generation: bool = False,
        pending_tool_calls: bool = False, max_output_tokens: int = 1024,
        required_capabilities: list[str] | None = None,
    ) -> ModelSwitchPlan:
        """Read-only scoped preflight. Never alters thread/model or copies filtered history.

        Coordinator owns applying the plan under its turn lock. The existing
        inference endpoint still requires a provider-scoped thread; do not send
        the source ID to a different provider or persist this history twice.
        """
        if not model_switch_enabled():
            return ModelSwitchPlan("blocked", "Model switching is disabled by the server operator.")
        if active_generation or pending_tool_calls:
            return ModelSwitchPlan("blocked", "Wait for completion or explicitly cancel the active turn.")
        if not self.context_enabled():
            return ModelSwitchPlan("blocked", "Server context is disabled; use explicit local history or start fresh.")
        try:
            uuid.UUID(str(thread_id))
            uuid.UUID(str(user_id))
        except (ValueError, TypeError, AttributeError):
            raise ContractError(APIErrorCode.PROVIDER_THREAD_MISMATCH, "Invalid thread/user scope.") from None
        target = await catalog.validate_selection(
            provider, model_id, required_capabilities=required_capabilities, max_tokens=max_output_tokens,
        )
        async with self.db.transaction() as tx:
            row = await tx.fetchrow(
                "SELECT id, provider, expires_at FROM provider_threads "
                "WHERE id = $1::uuid AND user_id = $2::uuid FOR UPDATE", thread_id, user_id,
            )
            if not row:
                raise ContractError(APIErrorCode.PROVIDER_THREAD_MISMATCH, "Thread not found for user scope.")
            if row.get("expires_at") is not None and self._expired(row["expires_at"]):
                return ModelSwitchPlan("blocked", "Source thread expired; explicitly start a new conversation.")
            rows = await tx.fetch(
                "SELECT role, content FROM provider_thread_messages "
                "WHERE thread_id = $1::uuid ORDER BY created_at ASC, id ASC LIMIT $2",
                thread_id, 201,
            )
        messages = []
        for message in rows:
            raw = message.get("content")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = {"text": raw}
            if not isinstance(raw, dict) or set(raw) != {"text"} or not isinstance(raw["text"], str):
                return ModelSwitchPlan("blocked", "Non-text history needs explicit review; nothing was copied.")
            text = raw["text"]
            if str(message.get("role")) == "assistant" and (
                not _is_plain_assistant_content(text) or self._is_summary_message(text)
            ):
                return ModelSwitchPlan("blocked", "Tool traces or compacted history need an explicit reviewed summary.")
            messages.append({"role": message.get("role"), "content": text})
        plan = plan_context_transfer(messages, target, max_output_tokens=max_output_tokens)
        if plan.status == "ready":
            return replace(plan, history_scope="retained_server_text_only", reason=(
                "Only retained server text can transfer; earlier pruning, attachments and tool state "
                "are not represented. Explicit confirmation or a reviewed summary is required."
            ))
        return plan

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
        except (ValueError, TypeError):
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
        except (ValueError, TypeError):
            raw = 120
        return max(5, min(raw, 2000))

    @staticmethod
    def _compaction_keep_recent_messages() -> int:
        try:
            raw = int(os.getenv("OPENVEGAS_CONTEXT_COMPACTION_KEEP_RECENT_MESSAGES", "60"))
        except (ValueError, TypeError):
            raw = 60
        return max(1, min(raw, 1000))

    @staticmethod
    def _compaction_max_summary_chars() -> int:
        try:
            raw = int(os.getenv("OPENVEGAS_CONTEXT_COMPACTION_MAX_SUMMARY_CHARS", "6000"))
        except (ValueError, TypeError):
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
                metadata = await tx.fetchrow(
                    "SELECT content FROM provider_thread_messages "
                    "WHERE thread_id = $1::uuid AND role = 'system' LIMIT 1", thread_id,
                )
                if metadata:
                    raise ContractError(
                        APIErrorCode.INVALID_TRANSITION,
                        "Canonical threads require /models/conversations/ask; "
                        "legacy inference must not prune or flatten their history.",
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
        except (ValueError, TypeError):
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
            ts = ts.replace(tzinfo=UTC)
        return ts <= datetime.now(UTC)
