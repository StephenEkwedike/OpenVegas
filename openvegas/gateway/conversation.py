"""Bounded canonical text history, never a provider prompt or tool executor.

The store envelope is server-owned metadata. It must never be sent as a system
message. Unsupported content is rejected, not flattened, redacted, or summarized.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

MAX_MESSAGES = 200
MAX_TEXT_BYTES = 64_000
MAX_HISTORY_BYTES = 1_000_000
STORAGE_KIND = "openvegas.canonical.text.v1"
ATTACHMENT_WARNING = (
    "Attachment continuity is unsupported. Images, files, audio and their contents "
    "are not transferred; explicitly start fresh to use attachments."
)
TOOL_WARNING = (
    "Tool continuity is unsupported. Finish or cancel tools and explicitly start "
    "fresh; tool calls, results and hidden reasoning are never replayed as text."
)
_SECRET = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|(?:authorization\s*[:=]\s*bearer\s+\S+)"
    r"|(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[\"']?\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_TRACE = re.compile(
    r"[\"'](?:tool_name|tool_calls|function_call|observation|observations|result_status)[\"']\s*:"
    r"|</?(?:tool|observation|function_call|trace)\b|```tool\b"
    r"|<\|(?:im_start|start_header_id|system|developer)\|>",
    re.IGNORECASE,
)


class ContinuityError(ValueError):
    """Messages are static and never include submitted content or credentials."""


def validate_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_BYTES:
        raise ContinuityError("Continuity requires nonempty bounded text; nothing was copied.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ContinuityError("History contains invalid Unicode; nothing was copied.") from None
    if len(encoded) > MAX_TEXT_BYTES:
        raise ContinuityError("A history message exceeds the transfer bound.")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise ContinuityError("History contains unsupported control characters.")
    if _SECRET.search(value):
        raise ContinuityError("Secret-like history is blocked; remove it before continuing.")
    if _TRACE.search(value) or value.lstrip().startswith("conversation_summary_v1"):
        raise ContinuityError(TOOL_WARNING)
    return value


@dataclass(frozen=True)
class CanonicalConversation:
    # Immutable role/content tuples prevent mutation after validation.
    turns: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_messages(cls, messages: Any) -> CanonicalConversation:
        if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
            raise ContinuityError("History exceeds the bounded message count.")
        if len(messages) % 2:
            raise ContinuityError("Complete or explicitly cancel the unfinished turn first.")
        turns = []
        size = 0
        for i, message in enumerate(messages):
            if not isinstance(message, dict) or set(message) != {"role", "content"}:
                raise ContinuityError("Only canonical role/content text records can transfer.")
            role = "assistant" if i % 2 else "user"
            if message["role"] != role:
                raise ContinuityError(
                    "History must contain ordered, completed user/assistant pairs."
                )
            value = validate_text(message["content"])
            size += len(value.encode("utf-8")) + 32
            if size > MAX_HISTORY_BYTES:
                raise ContinuityError(
                    "History exceeds the transfer byte bound; start fresh explicitly."
                )
            turns.append((role, value))
        return cls(tuple(turns))

    def messages(self) -> list[dict[str, str]]:
        return [{"role": role, "content": content} for role, content in self.turns]

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @property
    def byte_bound(self) -> int:
        return sum(len(content.encode("utf-8")) + 32 for _, content in self.turns)

    def to_json(self) -> str:
        return json.dumps(
            {"kind": STORAGE_KIND, "messages": self.messages()},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_storage(cls, value: Any) -> CanonicalConversation:
        if isinstance(value, str):
            if len(value) > MAX_HISTORY_BYTES * 6 + 20_000:
                raise ContinuityError("Canonical storage exceeds the byte bound.")
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                raise ContinuityError("Invalid canonical history record.") from None
        if isinstance(value, dict) and "pending" in value:
            raise ContinuityError(
                "An interrupted or unsafe turn prevents continuity; explicitly start fresh. "
                "Do not automatically retry a possibly billed request."
            )
        if not isinstance(value, dict) or set(value) != {"kind", "messages"}:
            raise ContinuityError("Legacy or incomplete history cannot transfer automatically.")
        if value["kind"] != STORAGE_KIND:
            raise ContinuityError("Unsupported canonical history version.")
        return cls.from_messages(value["messages"])

    def append(self, prompt: str, response: str) -> CanonicalConversation:
        return self.from_messages(
            self.messages()
            + [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": response},
            ]
        )

    def validate_target(self, target: dict, max_output_tokens: int = 1024) -> None:
        if not isinstance(target, dict) or target.get("available") is not True:
            raise ContinuityError("Validate server-managed model access before switching.")
        if type(max_output_tokens) is not int or max_output_tokens < 1:
            raise ContinuityError("A positive output token budget is required.")
        limit = target.get("max_tokens")
        if type(limit) is not int or max_output_tokens > limit:
            raise ContinuityError("Target output limit is unreviewed or insufficient.")
        caps = target.get("capabilities")
        if not isinstance(caps, dict) or caps.get("role_preserving_history") is not True:
            raise ContinuityError(
                "This adapter cannot preserve history roles; explicitly start fresh."
            )
        context = caps.get("context_window_tokens")
        if type(context) is not int or context <= 0:
            raise ContinuityError("Target context limit is unreviewed; no history was truncated.")
        if self.byte_bound + max_output_tokens + 256 > context:
            raise ContinuityError("History may exceed target context; explicitly start fresh.")

    def validate_next_turn(self, prompt: str, target: dict, max_output_tokens: int) -> None:
        validate_text(prompt)
        self.validate_target(target, max_output_tokens)
        # Reserve storage for a worst-case bounded UTF-8 response before any paid call.
        if len(self.turns) + 2 > MAX_MESSAGES or (
            self.byte_bound + len(prompt.encode("utf-8")) + MAX_TEXT_BYTES + 64 > MAX_HISTORY_BYTES
        ):
            raise ContinuityError(
                "Canonical storage is full; explicitly start fresh before inference."
            )
        if (
            self.byte_bound + len(prompt.encode("utf-8")) + 32 + max_output_tokens + 256
            > (target["capabilities"]["context_window_tokens"])
        ):
            raise ContinuityError(
                "Prompt plus full history exceeds target context; no paid call made."
            )
