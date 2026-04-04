from __future__ import annotations

import json

import pytest

from openvegas.capabilities import get_caps, resolve_capability


@pytest.mark.parametrize(
    "provider,model,feature,expected",
    [
        ("openai", "gpt-5", "web_search", True),
        ("openai", "gpt-4o", "web_search", False),
        ("anthropic", "claude-sonnet-4", "web_search", False),
        ("gemini", "gemini-2.5-pro", "image_input", True),
    ],
)
def test_capability_matrix(provider, model, feature, expected):
    caps = get_caps(provider, model)
    assert getattr(caps, feature) is expected


def test_resolve_capability_honors_flag_mapping(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_FEATURES_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_ENABLE_WEB_SEARCH", "0")
    assert resolve_capability("openai", "gpt-5", "web_search", user_id="u1") is False


def test_capability_resolution_uses_provider_default_for_vision(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_FEATURES_ENABLED", "1")
    monkeypatch.delenv("OPENVEGAS_ENABLE_VISION", raising=False)
    monkeypatch.delenv("OPENVEGAS_ENABLE_IMAGE_INPUT", raising=False)
    assert resolve_capability("openai", "gpt-4o", "image_input") is True


def test_capability_resolution_env_override_wins(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_FEATURES_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_ENABLE_VISION", "0")
    assert resolve_capability("openai", "gpt-4o", "image_input") is False


def test_get_caps_applies_remote_override_from_file(tmp_path, monkeypatch):
    payload = {"openai:gpt-5*": {"web_search": False, "image_input": False}}
    override_file = tmp_path / "cap_overrides.json"
    override_file.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setenv("OPENVEGAS_CAPABILITY_OVERRIDES_URL", str(override_file))
    monkeypatch.setenv("OPENVEGAS_CAPABILITY_OVERRIDES_CACHE_TTL_SEC", "0")
    monkeypatch.delenv("OPENVEGAS_CAPABILITY_OVERRIDES_JSON", raising=False)

    caps = get_caps("openai", "gpt-5.4")
    assert caps.web_search is False
    assert caps.image_input is False


def test_env_override_still_wins_over_remote(tmp_path, monkeypatch):
    payload = {"openai:gpt-5*": {"web_search": False}}
    override_file = tmp_path / "cap_overrides.json"
    override_file.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setenv("OPENVEGAS_CAPABILITY_OVERRIDES_URL", str(override_file))
    monkeypatch.setenv("OPENVEGAS_CAPABILITY_OVERRIDES_CACHE_TTL_SEC", "0")
    monkeypatch.setenv("OPENVEGAS_CAPABILITY_OVERRIDES_JSON", json.dumps({"openai:gpt-5*": {"web_search": True}}))

    caps = get_caps("openai", "gpt-5.4")
    assert caps.web_search is True
