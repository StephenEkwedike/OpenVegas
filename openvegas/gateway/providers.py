"""Adapter capabilities, not claims of provider-wide feature parity.

Exact model reviews are operator-controlled JSON, never client input. No model
names, prices, credential values, or consumer sessions are bundled here.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Any

from openvegas.contracts.errors import APIErrorCode, ContractError


class Provider(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    MISTRAL = "mistral"
    OPENROUTER = "openrouter"


@dataclass(frozen=True)
class ProviderDescriptor:
    id: str
    label: str
    adapter_method: str
    credential_env: str
    tools: bool = False
    image_input: bool = False
    web_search: bool = False
    role_preserving_history: bool = True
    requires_review: bool = False


PROVIDERS = MappingProxyType(
    {
        "openai": ProviderDescriptor(
            "openai",
            "OpenAI",
            "_call_openai",
            "OPENAI_API_KEY",
            tools=True,
            image_input=True,
            web_search=True,
        ),
        "anthropic": ProviderDescriptor(
            "anthropic", "Anthropic", "_call_anthropic", "ANTHROPIC_API_KEY"
        ),
        "gemini": ProviderDescriptor("gemini", "Gemini", "_call_gemini", "GEMINI_API_KEY"),
        "mistral": ProviderDescriptor(
            "mistral", "Mistral", "_call_mistral", "MISTRAL_API_KEY", requires_review=True
        ),
        "openrouter": ProviderDescriptor(
            "openrouter",
            "OpenRouter",
            "_call_openrouter",
            "OPENROUTER_API_KEY",
            tools=True,
            requires_review=True,
        ),
    }
)


def get_provider(provider: str) -> ProviderDescriptor:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Unsupported provider.") from None


def get_model_review(provider: str, model_id: str) -> dict[str, Any]:
    """Return only a fresh exact-ID review; wildcard/future-model guesses fail closed."""
    try:
        raw = os.getenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
        if len(raw) > 1_000_000:
            return {}
        review = json.loads(raw).get(f"{provider}:{model_id}", {})
        reviewed = datetime.fromisoformat(review["reviewed_at"])
        expires = datetime.fromisoformat(review["expires_at"])
        now = datetime.now(UTC)
        if not reviewed <= now < expires or expires - reviewed > timedelta(days=30):
            return {}
        return review
    except (ValueError, TypeError, KeyError, AttributeError):
        return {}


def model_capabilities(provider: str, model_id: str) -> dict[str, Any]:
    adapter = get_provider(provider)
    review = get_model_review(provider, model_id)
    reviewed_caps = review.get("capabilities", {})
    if not isinstance(reviewed_caps, dict):
        reviewed_caps = {}
    context = review.get("context_window_tokens")
    if type(context) is not int or not 1 <= context <= 10_000_000:
        context = None
    responses = provider == "openai" and (
        model_id.lower().startswith("gpt-5") or "codex" in model_id.lower()
    )
    return {
        "text": True,
        "tools": adapter.tools and reviewed_caps.get("tools") is True,
        "image_input": adapter.image_input and reviewed_caps.get("image_input") is True,
        "web_search": adapter.web_search and responses and reviewed_caps.get("web_search") is True,
        "json_schema": False,
        "reasoning_controls": False,
        "stream_events": True,
        "streaming_mode": "native_or_buffered" if provider == "openai" else "buffered",
        "role_preserving_history": adapter.role_preserving_history,
        "context_window_tokens": context,
        "reviewed": bool(review),
    }


def provider_descriptors() -> list[dict[str, Any]]:
    # Credential aliases and dispatch method names are server-only metadata.
    return [
        {"id": p.id, "label": p.label, "credential_source": "server_managed"}
        for p in PROVIDERS.values()
    ]


def model_switch_enabled() -> bool:
    """Enabled by default; an explicit zero is the operator rollback switch."""
    return os.getenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "1").strip() == "1"


async def resolve_provider_api_key(db: Any, provider: str) -> str:
    """Preserve registry-first credentials and local-only environment fallback."""
    descriptor = get_provider(provider)
    runtime_env = os.getenv("OPENVEGAS_RUNTIME_ENV", os.getenv("ENV", "local")).strip() or "local"
    row = None
    try:
        row = await db.fetchrow(
            "SELECT key_alias FROM provider_credentials "
            "WHERE provider = $1 AND env = $2 AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 1",
            provider,
            runtime_env,
        )
    except Exception:  # noqa: BLE001 - Preserve local fallback across DB drivers without exposing credential errors.
        # Preserve local fallback without logging secret-bearing database errors.
        row = None
    if row:
        key = os.getenv(str(row["key_alias"]).strip(), "").strip()
        if key:
            return key
    elif runtime_env.lower() in {"local", "dev", "development", "test"}:
        key = os.getenv(descriptor.credential_env, "").strip()
        if key:
            return key
    raise ContractError(
        APIErrorCode.PROVIDER_UNAVAILABLE,
        f"No active provider credentials configured for {provider}.",
    )
