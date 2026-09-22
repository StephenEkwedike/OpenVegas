"""Bounded OpenRouter request controls; never handle or expose native thoughts."""

from __future__ import annotations

from openvegas.capabilities import reviewed_reasoning_efforts
from openvegas.contracts.errors import APIErrorCode, ContractError


def reasoning_payload(effort: object, caps: dict) -> dict:
    """Merge into build_payload; None preserves provider-default behavior.

    caps must come from the server's exact-model review, never request input.
    Keep provider.require_parameters=True so unsupported endpoints are excluded.
    Source: https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
    """
    if effort is None:
        return {}
    if not isinstance(effort, str) or effort not in reviewed_reasoning_efforts(
        caps.get("reasoning_efforts")
    ):
        raise ContractError(
            APIErrorCode.INVALID_TRANSITION,
            "Reasoning effort is not reviewed for this exact model; no request was sent.",
        )
    return {"reasoning": {"effort": effort, "exclude": True}}
