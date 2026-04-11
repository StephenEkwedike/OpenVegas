from __future__ import annotations

import os
import stat

import pytest

from openvegas import config as cfg
from openvegas.telemetry import get_metrics_snapshot, reset_metrics


def _bind_temp_config(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")


def test_save_session_force_config_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _bind_temp_config(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENVEGAS_FORCE_CONFIG_REFRESH_STORAGE", "1")

    cfg.save_session("access-1", "refresh-1", access_expires_at=1700000000)
    session = cfg.get_session()
    assert session["access_token"] == "access-1"
    assert session["refresh_token"] == "refresh-1"
    assert session["refresh_storage"] == "config"
    assert int(session["access_expires_at"]) == 1700000000

    mode = stat.S_IMODE(os.stat(cfg.CONFIG_FILE).st_mode)
    assert mode & 0o600 == 0o600


def test_platform_store_then_config_write_failure_emits_structured_degraded_metric(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    _bind_temp_config(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENVEGAS_FORCE_CONFIG_REFRESH_STORAGE", raising=False)
    reset_metrics()

    monkeypatch.setattr(cfg, "platform_keychain_available", lambda: True)
    monkeypatch.setattr(cfg, "save_refresh_to_platform_store", lambda _token: None)
    monkeypatch.setattr(cfg, "save_config_atomic", lambda _config: (_ for _ in ()).throw(RuntimeError("write_failed")))
    monkeypatch.setattr(
        cfg,
        "_clear_platform_refresh_token_only",
        lambda: (_ for _ in ()).throw(RuntimeError("rollback_failed")),
    )

    with pytest.raises(RuntimeError):
        cfg.save_session("access-1", "refresh-1", access_expires_at=1700000000)

    snapshot = get_metrics_snapshot()
    metric_key = next((k for k in snapshot if k.startswith("auth_session_save_degraded_total|")), "")
    assert metric_key
    assert "reason=rollback_failed" in metric_key
    assert "config_write_state=write_attempted_unknown" in metric_key
    assert "platform_saved=true" in metric_key
    assert "rollback_attempted=true" in metric_key
    assert "rollback_succeeded=false" in metric_key


def test_save_config_atomic_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _bind_temp_config(monkeypatch, tmp_path)
    payload = {"session": {"access_token": "a", "refresh_token": "r"}}
    cfg.save_config_atomic(payload)
    assert cfg.CONFIG_FILE.exists()
    loaded = cfg.load_config()
    assert loaded["session"]["access_token"] == "a"


def test_force_config_storage_override_used_in_headless_ci(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _bind_temp_config(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENVEGAS_FORCE_CONFIG_REFRESH_STORAGE", "1")
    monkeypatch.setattr(cfg, "platform_keychain_available", lambda: True)
    cfg.save_session("a2", "r2")
    sess = cfg.get_session()
    assert sess["refresh_storage"] == "config"
    assert sess["refresh_token"] == "r2"


def test_load_config_migrates_stale_localhost_backend_url(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _bind_temp_config(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENVEGAS_BACKEND_URL", raising=False)
    cfg.CONFIG_FILE.write_text('{"backend_url":"http://127.0.0.1:8000"}', encoding="utf-8")

    loaded = cfg.load_config()

    assert loaded["backend_url"] == "https://app.openvegas.ai"
    persisted = cfg.CONFIG_FILE.read_text(encoding="utf-8")
    assert '"backend_url": "https://app.openvegas.ai"' in persisted


def test_load_config_migrates_stale_railway_backend_url(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _bind_temp_config(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENVEGAS_BACKEND_URL", raising=False)
    cfg.CONFIG_FILE.write_text(
        '{"backend_url":"https://openvegasdeployed-production.up.railway.app"}',
        encoding="utf-8",
    )

    loaded = cfg.load_config()

    assert loaded["backend_url"] == "https://app.openvegas.ai"
    persisted = cfg.CONFIG_FILE.read_text(encoding="utf-8")
    assert '"backend_url": "https://app.openvegas.ai"' in persisted


def test_get_backend_url_prefers_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _bind_temp_config(monkeypatch, tmp_path)
    cfg.CONFIG_FILE.write_text('{"backend_url":"https://old.example.com"}', encoding="utf-8")
    monkeypatch.setenv("OPENVEGAS_BACKEND_URL", "https://app.openvegas.ai/")

    assert cfg.get_backend_url() == "https://app.openvegas.ai"


def test_get_backend_url_falls_back_to_api_base_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _bind_temp_config(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENVEGAS_BACKEND_URL", raising=False)
    monkeypatch.delenv("OPENVEGAS_API_URL", raising=False)
    cfg.CONFIG_FILE.write_text('{"backend_url":"https://old.example.com"}', encoding="utf-8")
    monkeypatch.setenv("OPENVEGAS_API_BASE_URL", "https://api-base.example.com/")

    assert cfg.get_backend_url() == "https://api-base.example.com"


def test_load_config_migrates_legacy_backend_url_to_api_alias_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _bind_temp_config(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENVEGAS_BACKEND_URL", raising=False)
    monkeypatch.delenv("OPENVEGAS_API_BASE_URL", raising=False)
    monkeypatch.setenv("OPENVEGAS_API_URL", "https://alias-api.example.com/")
    cfg.CONFIG_FILE.write_text('{"backend_url":"https://api.openvegas.gg"}', encoding="utf-8")

    loaded = cfg.load_config()

    assert loaded["backend_url"] == "https://alias-api.example.com"
    persisted = cfg.CONFIG_FILE.read_text(encoding="utf-8")
    assert '"backend_url": "https://alias-api.example.com"' in persisted
