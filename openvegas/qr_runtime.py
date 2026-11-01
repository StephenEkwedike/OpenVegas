"""Shared QR runtime dependency bootstrap for server and terminal surfaces."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from threading import Lock
from typing import Tuple

_LOCK = Lock()
_READY: bool | None = None
_REASON: str | None = None
_AUTO_INSTALL_ATTEMPTED = False


def _truthy_env(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _install_cmd() -> str:
    return f'{sys.executable} -m pip install "qrcode[pil]>=7.4"'


def _import_qrcode() -> bool:
    try:
        importlib.import_module("qrcode")
        importlib.import_module("qrcode.image.svg")
        return True
    except Exception:
        return False


def ensure_qrcode_available() -> Tuple[bool, str | None]:
    """Ensure qrcode import is available in the active interpreter.

    Returns `(ok, reason)` where `reason` is populated when unavailable.
    When `OPENVEGAS_QR_AUTO_INSTALL=1` (default), a one-time local install
    attempt is made in the current interpreter.
    """

    global _READY, _REASON, _AUTO_INSTALL_ATTEMPTED
    with _LOCK:
        if _READY is True:
            return True, None
        if _import_qrcode():
            _READY = True
            _REASON = None
            return True, None

        missing_reason = (
            "ModuleNotFoundError: No module named 'qrcode' "
            f"(python={sys.executable}). Install with: {_install_cmd()}"
        )

        if _truthy_env("OPENVEGAS_QR_AUTO_INSTALL", "1") and not _AUTO_INSTALL_ATTEMPTED:
            _AUTO_INSTALL_ATTEMPTED = True
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "qrcode[pil]>=7.4"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=45,
                )
            except Exception as exc:
                err = str(exc).strip()[:220]
                _READY = False
                _REASON = f"{missing_reason}; auto-install failed: {err}"
                return False, _REASON

            if _import_qrcode():
                _READY = True
                _REASON = None
                return True, None

        _READY = False
        _REASON = missing_reason
        return False, _REASON

