"""Pure picker/turn-boundary decisions plus a thin authenticated backend preflight.

No credential/config imports, provider calls, tool execution, or state mutation.
An available legacy catalog model does not need a new capability review merely
to be selected. Fresh providers still obey server-side access/review gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ModelSelectionError(ValueError):
    pass


def selectable(model: dict) -> bool:
    if model.get("available") is False:
        return False
    return model.get("enabled") is True and model.get("selectable", model.get("available")) is True


def model_options(payload: dict, *, provider: str | None = None, search: str = "") -> list[dict]:
    rows = payload.get("models")
    if not isinstance(rows, list) or len(rows) > 2000:
        raise ModelSelectionError("Invalid or oversized model catalog response.")
    options = []
    for row in rows:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(key), str) and row[key] for key in ("provider", "model_id")
        ):
            raise ModelSelectionError("Invalid model catalog entry.")
        if provider is not None and row["provider"] != provider:
            continue
        if (
            search.casefold()
            not in f"{row['provider']}/{row['model_id']} {row.get('display_name', '')}".casefold()
        ):
            continue
        options.append(dict(row))
    return sorted(options, key=lambda row: (row["provider"], row["model_id"]))


def format_options(options: list[dict]) -> str:
    def safe(value: Any) -> str:
        return "".join(c if c.isprintable() else "?" for c in str(value))[:300]

    return (
        "\n".join(
            f"{safe(row['provider'])}/{safe(row['model_id'])}  "
            + (
                "available"
                if selectable(row)
                else "unavailable: "
                + safe(row.get("unavailable_reason") or "backend validation required")
            )
            for row in options
        )
        or "No matching catalog models."
    )


def select_model(payload: dict, provider: str, model_id: str) -> dict:
    if payload.get("switching_enabled") is False:
        raise ModelSelectionError("Model switching is disabled by the server operator.")
    matches = [
        row for row in model_options(payload, provider=provider) if row["model_id"] == model_id
    ]
    if len(matches) != 1:
        raise ModelSelectionError(
            "Choose an exact model ID from /models; unknown or ambiguous selection."
        )
    if not selectable(matches[0]):
        raise ModelSelectionError(
            str(matches[0].get("unavailable_reason") or "Model is unavailable.")
        )
    return matches[0]


async def validate_selection(client: Any, provider: str, model_id: str) -> dict:
    """Use existing OpenVegasClient.list_models and authenticated _request methods.

    No fallback to local provider defaults or old unvalidated /models responses.
    No required_capabilities are sent for ordinary text model selection.
    """
    select_model(await client.list_models(provider), provider, model_id)
    response = await client._request(
        "POST",
        "/models/validate",
        json={
            "provider": provider,
            "model": model_id,
        },
    )
    if response.get("selection_valid") is not True or response.get("state_changed") is not False:
        raise ModelSelectionError("Backend did not validate the selection; model unchanged.")
    model = response.get("model")
    return select_model({"models": [model]}, provider, model_id)


@dataclass(frozen=True)
class SwitchPlan:
    status: str
    provider: str
    model: str
    thread_id: str | None
    message: str
    reset_context: bool = False


def plan_switch(
    target: dict,
    *,
    current_provider: str,
    current_model: str,
    thread_id: str | None,
    turn_active: bool = False,
    tools_pending: bool = False,
    pending_attachments: bool = False,
    has_history: bool = False,
    confirm_fresh: bool = False,
) -> SwitchPlan:
    """Plan at the command-loop boundary; caller applies the tuple without awaits.

    Same-provider switches reuse the existing scoped thread, not a history replay.
    This preserves existing context policy, not a promise of zero future pruning.
    Cross-provider transfers need the separate server planner/commit integration;
    the working fallback here is a clearly confirmed fresh context.
    """

    def blocked(message: str) -> SwitchPlan:
        return SwitchPlan("blocked", current_provider, current_model, thread_id, message)

    if turn_active or tools_pending:
        return blocked(
            "Wait for generation and tool results, or explicitly cancel before switching."
        )
    if pending_attachments:
        return blocked("Send or remove pending attachments before changing models.")
    if not selectable(target) or not all(
        isinstance(target.get(key), str) and target[key] for key in ("provider", "model_id")
    ):
        return blocked("Target is unavailable; refresh /models and validate again.")
    provider, model = target["provider"], target["model_id"]
    if provider == current_provider:
        return SwitchPlan(
            "ready",
            provider,
            model,
            thread_id,
            "Model selected; existing provider thread retained under its current context policy.",
        )
    if (thread_id or has_history) and not confirm_fresh:
        return SwitchPlan(
            "confirm_fresh",
            current_provider,
            current_model,
            thread_id,
            "Changing provider starts fresh context. Existing history and tool results will not transfer.",
        )
    return SwitchPlan(
        "ready",
        provider,
        model,
        None,
        "Provider/model selected with fresh context; no history or tools replayed.",
        True,
    )
