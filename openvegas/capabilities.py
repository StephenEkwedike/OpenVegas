"""Provider/model capability resolution with flags and rollout controls."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen
from dataclasses import dataclass, replace

from openvegas.flags import features


@dataclass(frozen=True)
class ModelCapabilities:
    web_search: bool
    image_input: bool
    file_upload: bool
    file_search: bool
    stream_events: bool
    code_exec: bool
    image_gen: bool
    realtime_voice: bool
    speech_to_text: bool


DEFAULT_CAPS = ModelCapabilities(
    web_search=False,
    image_input=False,
    file_upload=False,
    file_search=False,
    stream_events=True,
    code_exec=False,
    image_gen=False,
    realtime_voice=False,
    speech_to_text=False,
)

PROVIDER_DEFAULTS: dict[str, ModelCapabilities] = {
    "openai": replace(
        DEFAULT_CAPS,
        web_search=True,
        image_input=True,
        file_upload=True,
        file_search=True,
        image_gen=True,
        realtime_voice=True,
        speech_to_text=True,
    ),
    "anthropic": replace(DEFAULT_CAPS, image_input=True, file_upload=True),
    "gemini": replace(DEFAULT_CAPS, image_input=True, file_upload=True),
}

MODEL_PATTERN_OVERRIDES: list[tuple[str, str, dict[str, bool]]] = [
    ("openai", "gpt-5*", {"web_search": True, "stream_events": True}),
    ("openai", "gpt-4o*", {"web_search": False}),
    ("openai", "*codex*", {"web_search": True, "stream_events": True}),
    ("anthropic", "claude*", {"web_search": False}),
    ("gemini", "gemini*", {"web_search": False}),
]

FEATURE_FLAG_KEY = {
    "web_search": "web_search",
    "image_input": "vision",
    "file_upload": "files",
    "file_search": "files",
    "stream_events": "global_enabled",
    "code_exec": "code_exec",
    "image_gen": "image_gen",
    "realtime_voice": "realtime_voice",
    "speech_to_text": "speech_to_text",
}

_REMOTE_OVERRIDE_CACHE: dict[str, object] = {"source": "", "loaded_at": 0.0, "overrides": {}}


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _normalize_override_payload(payload: object) -> dict[tuple[str, str], dict[str, bool]]:
    if not isinstance(payload, dict):
        return {}
    out: dict[tuple[str, str], dict[str, bool]] = {}
    for key, value in payload.items():  # type: ignore[attr-defined]
        if not isinstance(key, str) or ":" not in key or not isinstance(value, dict):
            continue
        provider, model_pattern = key.split(":", 1)
        provider_key = provider.strip().lower()
        model_key = model_pattern.strip().lower()
        if not provider_key or not model_key:
            continue
        normalized: dict[str, bool] = {}
        for cap_name in ModelCapabilities.__dataclass_fields__.keys():
            if cap_name in value:
                normalized[cap_name] = _as_bool(value.get(cap_name), default=False)
        if normalized:
            out[(provider_key, model_key)] = normalized
    return out


def _load_env_overrides() -> dict[tuple[str, str], dict[str, bool]]:
    raw = os.getenv("OPENVEGAS_CAPABILITY_OVERRIDES_JSON", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return _normalize_override_payload(payload)


def _read_override_source(raw_source: str) -> str:
    parsed = urlparse(raw_source)
    if parsed.scheme in {"http", "https"}:
        timeout = float(os.getenv("OPENVEGAS_CAPABILITY_OVERRIDES_TIMEOUT_SEC", "1.5"))
        with urlopen(raw_source, timeout=max(0.1, timeout)) as resp:
            return str(resp.read().decode("utf-8", errors="ignore"))
    if parsed.scheme == "file":
        return Path(parsed.path).read_text(encoding="utf-8")
    return Path(raw_source).read_text(encoding="utf-8")


def _load_remote_overrides() -> dict[tuple[str, str], dict[str, bool]]:
    source = str(os.getenv("OPENVEGAS_CAPABILITY_OVERRIDES_URL", "")).strip()
    if not source:
        return {}

    try:
        ttl_sec = float(os.getenv("OPENVEGAS_CAPABILITY_OVERRIDES_CACHE_TTL_SEC", "30"))
    except Exception:
        ttl_sec = 30.0
    ttl_sec = max(0.0, ttl_sec)

    now = time.monotonic()
    cached_source = str(_REMOTE_OVERRIDE_CACHE.get("source") or "")
    cached_loaded_at = float(_REMOTE_OVERRIDE_CACHE.get("loaded_at") or 0.0)
    if cached_source == source and (now - cached_loaded_at) <= ttl_sec:
        cached = _REMOTE_OVERRIDE_CACHE.get("overrides")
        if isinstance(cached, dict):
            return cached  # type: ignore[return-value]
        return {}

    try:
        raw_payload = _read_override_source(source)
        parsed = json.loads(raw_payload)
        normalized = _normalize_override_payload(parsed)
    except Exception:
        normalized = {}

    _REMOTE_OVERRIDE_CACHE["source"] = source
    _REMOTE_OVERRIDE_CACHE["loaded_at"] = now
    _REMOTE_OVERRIDE_CACHE["overrides"] = normalized
    return normalized


def get_caps(provider: str, model: str) -> ModelCapabilities:
    provider_key = str(provider or "").strip().lower()
    model_key = str(model or "").strip().lower()
    caps = PROVIDER_DEFAULTS.get(provider_key, DEFAULT_CAPS)

    for p, pattern, overrides in MODEL_PATTERN_OVERRIDES:
        if provider_key == p and fnmatch.fnmatch(model_key, pattern.lower()):
            caps = replace(caps, **overrides)

    for (p, pattern), overrides in _load_remote_overrides().items():
        if provider_key == p and fnmatch.fnmatch(model_key, pattern):
            caps = replace(caps, **overrides)

    for (p, pattern), overrides in _load_env_overrides().items():
        if provider_key == p and fnmatch.fnmatch(model_key, pattern):
            caps = replace(caps, **overrides)

    return caps


def rollout_bucket(user_id: str, capability: str) -> int:
    digest = hashlib.sha256(f"{user_id}:{capability}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def _rollout_pct(feature: str) -> int:
    key = f"OPENVEGAS_ROLLOUT_{feature.upper()}_PCT"
    raw = os.getenv(key, "100").strip()
    try:
        return max(0, min(100, int(raw)))
    except Exception:
        return 100


def resolve_capability(
    provider: str,
    model: str,
    feature: str,
    *,
    user_id: str | None = None,
) -> bool:
    caps = get_caps(provider, model)
    if not hasattr(caps, feature):
        return False

    enabled_flags = features()
    if not enabled_flags.get("global_enabled", True):
        return False

    enabled = bool(getattr(caps, feature))

    # Explicit env override wins (first direct capability key, then mapped feature flag key).
    cap_env_name = f"OPENVEGAS_ENABLE_{feature.upper()}"
    cap_env_raw = os.getenv(cap_env_name)
    if cap_env_raw is not None:
        enabled = _as_bool(cap_env_raw, default=enabled)

    flag_key = FEATURE_FLAG_KEY.get(feature)
    if flag_key:
        flag_env_name = f"OPENVEGAS_ENABLE_{flag_key.upper()}"
        flag_env_raw = os.getenv(flag_env_name)
        if flag_env_raw is not None and cap_env_raw is None:
            enabled = _as_bool(flag_env_raw, default=enabled)

    if not enabled:
        return False

    if user_id:
        pct = _rollout_pct(feature)
        if pct <= 0:
            return False
        if pct < 100 and rollout_bucket(user_id, feature) >= pct:
            return False

    return True
