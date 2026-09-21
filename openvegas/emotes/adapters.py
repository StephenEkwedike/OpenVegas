"""Passive external metadata adapters, NOT installed or version-certified hooks.

The host bridge must provide a stable turn ID and authoritative generation and
sequence. Arrival order, prompt text and shell exit status are not substitutes.
Native Stop/AfterAgent events alone do not establish successful final outcome.
"""

from __future__ import annotations

from .events import Event, Phase
from .spool import publish_event

CODEX_APP_SERVER_VERSION = (0, 153, 4)


def supports_codex_app_server(host_version: object) -> bool:
    return (
        type(host_version) is tuple
        and host_version == CODEX_APP_SERVER_VERSION
        and all(type(v) is int for v in host_version)
    )


def adapt_hook(
    provider: str,
    payload: dict,
    *,
    session_id: str,
    turn_id: str,
    generation: int,
    sequence: int,
    event_id: str,
    authoritative_success: bool = False,
) -> Event | None:
    """Ignore tool completion and all content fields; never infer success."""
    if not isinstance(payload, dict) or not isinstance(provider, str):
        return None
    source = {"claude": "claude", "gemini": "gemini", "codex": "codex"}.get(provider)
    if source is None:
        return None
    if (any(payload.get(key) is not None and payload[key] != session_id
            for key in ("session_id", "thread-id"))
            or any(payload.get(key) is not None and payload[key] != turn_id
                   for key in ("prompt_id", "turn_id", "turn-id"))):
        return None
    name = (
        payload.get("hook_event_name") if provider != "codex" else payload.get("type")
    )
    if not isinstance(name, str):
        return None
    phase = None
    if "agent_id" in payload:
        return None
    if provider == "claude":
        if name == "UserPromptSubmit":
            phase = Phase.START
        elif name == "SessionEnd":
            phase = Phase.EXIT
        elif name in {"PermissionRequest", "PostToolUseFailure"}:
            phase = Phase.PAUSE
        elif name == "StopFailure":
            phase = Phase.ERROR
        # Stop is a pre-decision hook, even with stop_hook_active=False. Another
        # Stop hook can continue work; a boolean assertion cannot fix that race.
    elif provider == "gemini":
        if name == "BeforeAgent":
            phase = Phase.START
        elif name == "Notification" and payload.get("notification_type") == "ToolPermission":
            phase = Phase.PAUSE
        elif name == "SessionEnd":
            phase = Phase.EXIT
        # AfterAgent may reject/retry the response. Never a completion barrier.
    elif (name == "agent-turn-complete" and authoritative_success is True
            and payload.get("error") is None and payload.get("status") in (None, "completed")
            and payload.get("cancelled", False) is False):
        # Notify-only has no start/cancel ordering. The bridge must verify it.
        phase = Phase.COMPLETE
    if phase is None:
        return None
    try:
        return Event(
            source,
            session_id,
            turn_id,
            event_id,
            phase,
            generation,
            sequence,
            outcome="success" if phase == Phase.COMPLETE else None,
        )
    except (TypeError, ValueError):
        return None


def adapt_codex_app_server(
    payload: dict, *, host_version: tuple[int, int, int], session_id: str,
    turn_id: str, generation: int, sequence: int, event_id: str,
) -> Event | None:
    """Reviewed 0.153.4 structured stream, not a native notify installation.

    A host that already owns the app-server stream must supply its authoritative
    generation/sequence and route through the controller's cancellation guards.
    This pure adapter never starts/attaches a process or authorizes an approval.
    """
    if not supports_codex_app_server(host_version) or not isinstance(payload, dict):
        return None
    params = payload.get("params")
    if not isinstance(params, dict) or params.get("threadId") != session_id:
        return None
    method = payload.get("method")
    phase = None
    if method in ("turn/started", "turn/completed"):
        turn = params.get("turn")
        if not isinstance(turn, dict) or turn.get("id") != turn_id:
            return None
        status = turn.get("status")
        if method == "turn/started" and status == "inProgress" and turn.get("error") is None:
            phase = Phase.START
        elif method == "turn/completed":
            if status == "completed" and turn.get("error") is None:
                phase = Phase.COMPLETE
            elif status == "failed":
                phase = Phase.ERROR
            elif status == "interrupted":
                phase = Phase.CANCEL
    elif params.get("turnId") == turn_id:
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                      "item/tool/requestUserInput", "item/permissions/requestApproval"):
            phase = Phase.PAUSE
        elif method == "error" and params.get("willRetry") is False:
            phase = Phase.ERROR
    if phase is None:
        return None
    try:
        return Event("codex", session_id, turn_id, event_id, phase, generation, sequence,
                     outcome="success" if phase == Phase.COMPLETE else None)
    except (TypeError, ValueError):
        return None


def publish_hook(provider: str, payload: dict, *, directory=None, **identity) -> bool:
    """No stdout/stderr, protocol changes, config edits, or upstream exit codes."""
    try:
        event = adapt_hook(provider, payload, **identity)
        return event is not None and publish_event(event, directory=directory)
    except Exception:  # noqa: BLE001 - cosmetics must never fail the upstream hook
        return False
