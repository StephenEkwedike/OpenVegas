"""Local config management (~/.openvegas/)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import sys
import time
import threading
from pathlib import Path
from typing import Any

from openvegas.telemetry import emit_metric

CONFIG_DIR = Path.home() / ".openvegas"
CONFIG_FILE = CONFIG_DIR / "config.json"
LEGACY_DEFAULT_BACKEND_URL = "https://api.openvegas.gg"
DEFAULT_BACKEND_URL = (
    str(os.getenv("OPENVEGAS_BACKEND_URL", "")).strip()
    or str(os.getenv("OPENVEGAS_API_BASE_URL", "")).strip()
    or str(os.getenv("OPENVEGAS_API_URL", "")).strip()
    or "https://app.openvegas.ai"
)
DEFAULT_OPENAI_MODEL = os.getenv("OPENVEGAS_DEFAULT_OPENAI_MODEL", "gpt-5.4")
_PLATFORM_STORE_SERVICE = "openvegas"
_PLATFORM_STORE_ACCOUNT = "refresh_token"

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "session": {},
    "providers": {},
    "default_provider": "openai",
    "default_model_by_provider": {
        "openai": DEFAULT_OPENAI_MODEL,
        "anthropic": "claude-sonnet-4-20250514",
        "gemini": "gemini-2.0-flash",
    },
    "theme": "default",
    "animation": True,
    "chat_style": "codex",
    "tool_event_density": "compact",
    "approval_ui": "menu",
    "backend_url": DEFAULT_BACKEND_URL,
    "supabase_url": "",
    "supabase_anon_key": "",
    "avatar_id": "ov_user_01",
    "avatar_palette": "default",
    "dealer_skin_id": "ov_dealer_female_tux_v1",
}

_SESSION_CLAIMS_CACHE: dict[str, Any] | None = None


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(0o700)
    except Exception:
        # Best-effort only; this can fail on Windows.
        pass


def load_config() -> dict:
    ensure_config_dir()
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            stored = json.loads(f.read())
        if stored.get("backend_url") == LEGACY_DEFAULT_BACKEND_URL:
            stored["backend_url"] = DEFAULT_BACKEND_URL
        # Migrate legacy OpenAI default model to current GPT-5.4 default
        # unless user explicitly set a non-legacy value.
        stored_models = dict(stored.get("default_model_by_provider") or {})
        openai_model = str(stored_models.get("openai", "") or "").strip()
        if openai_model in {"", "gpt-4o-mini", "gpt-5.3-codex"}:
            stored_models["openai"] = DEFAULT_OPENAI_MODEL
            stored["default_model_by_provider"] = stored_models
        return {**DEFAULT_CONFIG, **stored}
    return dict(DEFAULT_CONFIG)


def save_config_atomic(config: dict) -> None:
    """Persist config atomically.

    Contract:
    1) write temp file in same directory
    2) fsync temp file
    3) atomic replace
    4) fsync parent directory
    """

    ensure_config_dir()
    payload = json.dumps(config, indent=2).encode("utf-8")
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=str(CONFIG_DIR),
            prefix=".config.",
            suffix=".tmp",
        ) as tmp:
            tmp_name = tmp.name
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, CONFIG_FILE)
        dir_fd = os.open(str(CONFIG_DIR), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except Exception:
                pass


def save_config(config: dict) -> None:
    save_config_atomic(config)
    try:
        CONFIG_FILE.chmod(0o600)
    except Exception:
        pass


def _force_config_refresh_storage() -> bool:
    return str(os.getenv("OPENVEGAS_FORCE_CONFIG_REFRESH_STORAGE", "0")).strip() == "1"


def _try_import_keyring():
    try:
        import keyring  # type: ignore

        return keyring
    except Exception:
        return None


def platform_keychain_available() -> bool:
    if _force_config_refresh_storage():
        return False
    keyring = _try_import_keyring()
    if keyring is None:
        return False
    try:
        backend = keyring.get_keyring()
        backend_name = f"{backend.__class__.__module__}.{backend.__class__.__name__}".lower()
        if "fail" in backend_name:
            return False
        return True
    except Exception:
        return False


def touchid_enabled() -> bool:
    default = "1" if sys.platform == "darwin" else "0"
    return str(os.getenv("OPENVEGAS_ENABLE_TOUCHID", default)).strip().lower() in {"1", "true", "yes", "on"}


def touchid_supported() -> bool:
    if sys.platform != "darwin":
        return False
    return platform_keychain_available()


def require_touchid_unlock_for_refresh_storage(refresh_storage: str | None = None) -> bool:
    if not touchid_enabled():
        return False
    storage = str(refresh_storage or "").strip().lower()
    return storage in {"platform_credential_store", "platform", "keychain"}


def _touchid_prompt_via_local_auth(reason: str = "Unlock OpenVegas session") -> bool:
    if sys.platform != "darwin":
        return False
    try:
        # Optional dependency: pyobjc-framework-LocalAuthentication
        from LocalAuthentication import (  # type: ignore
            LAContext,
            LAPolicyDeviceOwnerAuthenticationWithBiometrics,
        )
    except Exception:
        return False

    completed = threading.Event()
    outcome: dict[str, bool] = {"ok": False}

    try:
        context = LAContext.alloc().init()
        can_eval, _err = context.canEvaluatePolicy_error_(
            LAPolicyDeviceOwnerAuthenticationWithBiometrics,
            None,
        )
        if not bool(can_eval):
            return False

        def _reply(success: bool, _error: object) -> None:
            outcome["ok"] = bool(success)
            completed.set()

        context.evaluatePolicy_localizedReason_reply_(
            LAPolicyDeviceOwnerAuthenticationWithBiometrics,
            str(reason or "Unlock OpenVegas session"),
            _reply,
        )
        completed.wait(timeout=20.0)
    except Exception:
        return False

    return bool(outcome.get("ok", False))


def request_touchid_unlock() -> bool:
    """Best-effort biometric + keychain unlock gate on macOS.

    1) Attempt explicit LocalAuthentication biometric prompt when available.
    2) Confirm keychain retrieval succeeds for refresh token entry.

    The refresh token remains stored only in keychain/config according to existing
    storage policy. This function never broadens secret scope.
    """
    if not touchid_enabled():
        return True
    if not touchid_supported():
        return False

    require_biometric = str(os.getenv("OPENVEGAS_TOUCHID_REQUIRE_BIOMETRIC", "1")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    biometric_ok = _touchid_prompt_via_local_auth("Unlock OpenVegas session")
    if require_biometric and not biometric_ok:
        return False

    keyring = _try_import_keyring()
    if keyring is None:
        return False
    try:
        token = keyring.get_password(_PLATFORM_STORE_SERVICE, _PLATFORM_STORE_ACCOUNT)
    except Exception:
        return False
    return bool(str(token or "").strip())


def save_refresh_to_platform_store(refresh_token: str) -> None:
    keyring = _try_import_keyring()
    if keyring is None:
        raise RuntimeError("keyring_unavailable")
    keyring.set_password(_PLATFORM_STORE_SERVICE, _PLATFORM_STORE_ACCOUNT, str(refresh_token))


def load_refresh_from_platform_store() -> str:
    keyring = _try_import_keyring()
    if keyring is None:
        return ""
    try:
        token = keyring.get_password(_PLATFORM_STORE_SERVICE, _PLATFORM_STORE_ACCOUNT)
    except Exception:
        return ""
    return str(token or "").strip()


def _clear_platform_refresh_token_only() -> None:
    keyring = _try_import_keyring()
    if keyring is None:
        return
    try:
        keyring.delete_password(_PLATFORM_STORE_SERVICE, _PLATFORM_STORE_ACCOUNT)
    except Exception:
        # Key may not exist; treat as already cleared.
        pass


def clear_persisted_refresh_token() -> None:
    _clear_platform_refresh_token_only()
    cfg = load_config()
    session = dict(cfg.get("session") or {})
    if not session:
        return
    session["refresh_token"] = ""
    if session.get("refresh_storage") == "platform_credential_store":
        session["refresh_storage"] = "platform_credential_store"
    cfg["session"] = session
    save_config(cfg)


def get_provider_key(provider: str) -> str | None:
    config = load_config()
    return config.get("providers", {}).get(provider, {}).get("api_key")


def set_provider_key(provider: str, api_key: str) -> None:
    config = load_config()
    if "providers" not in config:
        config["providers"] = {}
    config["providers"][provider] = {"api_key": api_key}
    save_config(config)


def get_session() -> dict:
    config = load_config()
    session = dict(config.get("session", {}))
    if session.get("refresh_storage") == "platform_credential_store":
        session["refresh_token"] = load_refresh_from_platform_store()
    return session


def clear_session_claim_cache() -> None:
    global _SESSION_CLAIMS_CACHE
    _SESSION_CLAIMS_CACHE = None


def invalidate_session_cache() -> None:
    clear_session_claim_cache()


def save_session(access_token: str, refresh_token: str, access_expires_at: int | None = None) -> None:
    cfg = load_config()
    next_cfg = dict(cfg)
    # INTENTIONAL CLI-ONLY COMPROMISE (DO NOT REMOVE SILENTLY):
    # Persist access token for restart UX parity with user expectations.
    # Browser auth remains memory-only.
    session_payload: dict[str, Any] = {
        "access_token": str(access_token or ""),
        "refresh_token": "",
    }
    if access_expires_at is not None:
        try:
            session_payload["access_expires_at"] = int(access_expires_at)
        except Exception:
            session_payload["access_expires_at"] = 0

    force_fallback = _force_config_refresh_storage()
    platform_saved = False
    cfg_write_state = "untouched"  # untouched | write_attempted_unknown | replaced
    rollback_attempted = False
    rollback_succeeded = False

    try:
        if not force_fallback and platform_keychain_available():
            save_refresh_to_platform_store(refresh_token)
            platform_saved = True
            session_payload["refresh_storage"] = "platform_credential_store"
        else:
            session_payload["refresh_storage"] = "config"
            session_payload["refresh_token"] = str(refresh_token or "")
        next_cfg["session"] = session_payload
        cfg_write_state = "write_attempted_unknown"
        save_config_atomic(next_cfg)
        cfg_write_state = "replaced"
        try:
            CONFIG_FILE.chmod(0o600)
        except Exception:
            pass
    except Exception:
        if platform_saved:
            rollback_attempted = True
            try:
                _clear_platform_refresh_token_only()
                rollback_succeeded = True
            except Exception as rollback_err:
                emit_metric(
                    "auth_session_save_degraded_total",
                    {
                        "reason": "rollback_failed",
                        "config_write_state": cfg_write_state,
                        "platform_saved": "true" if platform_saved else "false",
                        "rollback_attempted": "true" if rollback_attempted else "false",
                        "rollback_succeeded": "true" if rollback_succeeded else "false",
                    },
                )
                logger.error(
                    "session_save_degraded rollback_failed err=%s config_write_state=%s "
                    "platform_saved=%s rollback_attempted=%s rollback_succeeded=%s",
                    str(rollback_err),
                    cfg_write_state,
                    platform_saved,
                    rollback_attempted,
                    rollback_succeeded,
                )
        raise
    finally:
        clear_session_claim_cache()


def clear_session() -> None:
    _clear_platform_refresh_token_only()
    cfg = load_config()
    cfg["session"] = {}
    save_config(cfg)
    clear_session_claim_cache()


def clear_access_token_keep_refresh() -> None:
    """Clear only access token fields while preserving refresh-token storage."""
    cfg = load_config()
    session = dict(cfg.get("session") or {})
    if not session:
        return
    # Preserve refresh storage location/token; only drop access token material.
    session["access_token"] = ""
    session["access_expires_at"] = 0
    cfg["session"] = session
    save_config(cfg)
    clear_session_claim_cache()


def get_default_provider() -> str:
    config = load_config()
    return config.get("default_provider", "openai")


def get_default_model(provider: str) -> str:
    config = load_config()
    models = config.get("default_model_by_provider", {})
    if provider == "openai":
        return models.get(provider, DEFAULT_OPENAI_MODEL)
    return models.get(provider, "gpt-4o-mini")


def get_bearer_token() -> str | None:
    session = get_session()
    token = str(session.get("access_token", "")).strip()
    return token or None


def token_expires_soon(session: dict | None = None, leeway_sec: int = 300) -> bool:
    sess = dict(session or get_session() or {})
    token = str(sess.get("access_token", "")).strip()
    if not token:
        return True
    try:
        exp = int(sess.get("access_expires_at", 0) or 0)
    except Exception:
        exp = 0
    if exp <= 0:
        return True
    return (exp - int(time.time())) <= int(leeway_sec)


def get_backend_url() -> str:
    config = load_config()
    return config.get("backend_url", DEFAULT_BACKEND_URL)
