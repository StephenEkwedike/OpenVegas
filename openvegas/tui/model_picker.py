"""Pure picker/turn-boundary decisions plus a thin authenticated backend preflight.

No credential/config imports, provider calls, tool execution, or state mutation.
An available legacy catalog model does not need a new capability review merely
to be selected. Fresh providers still obey server-side access/review gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openvegas.capabilities import reviewed_reasoning_efforts


class ModelSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewedModelCapabilities:
    """An exact-model snapshot, created only from an authenticated validation reply."""

    provider: str
    model_id: str
    enabled: frozenset[str]
    reasoning_efforts: tuple[str, ...]
    streaming_mode: str

    def supports(self, provider: str, model_id: str, feature: str) -> bool:
        return (provider, model_id) == (self.provider, self.model_id) and feature in self.enabled


def reviewed_capabilities(model: dict, provider: str, model_id: str) -> ReviewedModelCapabilities:
    """Reject malformed/unreviewed descriptors; never infer support from a model family."""
    if (
        not isinstance(model, dict)
        or model.get("provider") != provider
        or model.get("model_id") != model_id
        or not selectable(model)
    ):
        raise ModelSelectionError("Capability descriptor does not match the selected model.")
    caps = model.get("capabilities")
    if not isinstance(caps, dict) or caps.get("reviewed") is not True:
        raise ModelSelectionError("Model has no current reviewed capability descriptor.")
    boolean_fields = (
        "text", "tools", "image_input", "file_upload", "web_search", "stream_events",
        "reasoning_controls",
    )
    if any(type(caps.get(key, False)) is not bool for key in boolean_fields):
        raise ModelSelectionError("Invalid capability flags in model descriptor.")
    raw_efforts = caps.get("reasoning_efforts", [])
    efforts = reviewed_reasoning_efforts(raw_efforts)
    if (not isinstance(raw_efforts, list) or len(efforts) != len(raw_efforts)
            or caps.get("reasoning_controls", False) != bool(efforts)):
        raise ModelSelectionError("Invalid reviewed reasoning efforts in model descriptor.")
    streaming_mode = caps.get("streaming_mode", "buffered")
    if not isinstance(streaming_mode, str) or streaming_mode not in {"buffered", "native", "native_or_buffered"}:
        raise ModelSelectionError("Invalid streaming mode in model descriptor.")
    return ReviewedModelCapabilities(
        provider, model_id, frozenset(key for key in boolean_fields if caps.get(key) is True),
        efforts, streaming_mode,
    )


def selectable(model: dict) -> bool:
    if model.get("available") is False:
        return False
    return model.get("enabled") is True and model.get("selectable", model.get("available")) is True


def model_options(payload: dict, *, provider: str | None = None, search: str = "") -> list[dict]:
    rows = payload.get("models") if isinstance(payload, dict) else None
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

    def features(row: dict) -> str:
        caps = row.get("capabilities")
        if not isinstance(caps, dict) or caps.get("reviewed") is not True:
            return ""
        labels = [label for key, label in (
            ("tools", "tools"), ("image_input", "images"),
            ("file_upload", "files"), ("web_search", "web"),
        ) if caps.get(key) is True]
        efforts = reviewed_reasoning_efforts(caps.get("reasoning_efforts"))
        if efforts:
            labels.append("reasoning: " + "/".join(efforts))
        if caps.get("streaming_mode") == "buffered":
            labels.append("buffered replies")
        return "  [" + "; ".join(labels or ["text"]) + "]"

    return (
        "\n".join(
            f"{safe(row['provider'])}/{safe(row['model_id'])}  "
            + (
                "available"
                if selectable(row)
                else "unavailable: "
                + safe(row.get("unavailable_reason") or "backend validation required")
            )
            + features(row)
            for row in options
        )
        or "No matching catalog models."
    )


def select_model(payload: dict, provider: str, model_id: str) -> dict:
    if isinstance(payload, dict) and payload.get("switching_enabled") is False:
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
    if (not isinstance(response, dict) or response.get("selection_valid") is not True
            or response.get("state_changed") is not False):
        raise ModelSelectionError("Backend did not validate the selection; model unchanged.")
    model = response.get("model")
    selected = select_model({"models": [model]}, provider, model_id)
    if provider == "openrouter":
        reviewed_capabilities(selected, provider, model_id)
    return selected


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
