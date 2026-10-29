"""Pure continuity planning; no provider calls, summaries, or local tool execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openvegas.gateway.conversation import CanonicalConversation, ContinuityError


@dataclass(frozen=True)
class ModelSwitchPlan:
    status: str
    reason: str
    messages: list[dict[str, str]] = field(default_factory=list)
    requires_new_thread: bool = True
    context_transferred: bool = False
    tool_replay_allowed: bool = False
    requires_confirmation: bool = True
    history_scope: str = "caller_supplied"
    revision: str | None = None
    thread_id: str | None = None


def plan_context_transfer(
    messages: list[dict],
    target: dict[str, Any],
    *,
    active_generation: bool = False,
    pending_tool_calls: bool = False,
    max_output_tokens: int = 1024,
) -> ModelSwitchPlan:
    """Use a server-validated descriptor; a ready plan is never an applied switch.

    Canonical service commits compare the revision and revalidate catalog access
    while serialized with inference. Caller-supplied history alone cannot prove
    server completeness. Legacy retained history still needs explicit review.
    """
    if active_generation or pending_tool_calls:
        return ModelSwitchPlan(
            "blocked", "Wait for generation and all tool results, or explicitly cancel first."
        )
    try:
        canonical = CanonicalConversation.from_messages(messages)
        canonical.validate_target(target, max_output_tokens)
    except ContinuityError as exc:
        return ModelSwitchPlan("blocked", str(exc))
    return ModelSwitchPlan(
        "ready",
        "Transfer complete plain-text history to a new thread; no tools replayed.",
        canonical.messages(),
        revision=canonical.revision,
    )
