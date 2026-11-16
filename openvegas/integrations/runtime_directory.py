"""Select the parent for private POSIX terminal runtime files, not user settings."""
from __future__ import annotations

import os
import re
import stat
import sys
import hashlib
import json
from pathlib import Path
from uuid import UUID

from .credentials import IntegrationError

RUNTIME_ENV = "OPENVEGAS_PRIVATE_RUNTIME_DIR"
TEST_REF_ENV = "OPENVEGAS_TEST_WALLET_RECEIPT"


def runtime_parent(default=None):
    raw = os.environ.get(RUNTIME_ENV)
    if raw is None:
        return default
    base = Path("/tmp").resolve(strict=True)
    root = Path(raw)
    error = "Expected an existing, owner-only OpenVegas runtime directory; no fallback."
    if (os.name != "posix" or raw != str(root) or root.parent != base
            or not re.fullmatch(r"ov-run-[0-9a-f]{16}", root.name)):
        raise IntegrationError(error)
    try:
        parent, info = base.stat(), root.lstat()
    except OSError as exc:
        raise IntegrationError(error) from exc
    # A direct child of root-owned sticky /tmp cannot be replaced by another uid.
    if (parent.st_uid != 0 or not parent.st_mode & stat.S_ISVTX
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise IntegrationError(error)
    return root


def hosted_mac_test_root():
    if (sys.platform == "darwin" and os.environ.get("OPENVEGAS_HOSTED_LAUNCH") == "1"
            and os.environ.get(TEST_REF_ENV) == "1"):
        root = runtime_parent()
        if root is None:
            raise IntegrationError("Private Mac test launch is missing its runtime directory.")
        return root
    return None


def router_port():
    root = hosted_mac_test_root()
    # The QA supervisor can name one endpoint before spawning the router.
    # This is a routing address, never authentication or permission to spend.
    return 49152 + int(root.name[-4:], 16) % 16384 if root is not None else 0


def qa_grant_reference(session_id):
    if (sys.platform == "darwin" and os.environ.get(TEST_REF_ENV) == "1" and os.environ.get(RUNTIME_ENV)
            and isinstance(session_id, str) and re.fullmatch(r"[0-9a-f]{32}", session_id)):
        return hashlib.sha256(session_id.encode("ascii")).hexdigest()
    return None


def record_test_wallet_switch(host, chat, session):
    """Optional local evidence only. Failure to record cannot change a committed payer."""
    if sys.platform != "darwin" or os.environ.get(TEST_REF_ENV) != "1" or not os.environ.get(RUNTIME_ENV):
        return
    if host not in ("codex", "claude") or not isinstance(chat, str):
        return
    try:
        if str(UUID(chat)) != chat or session.state.mode != "v" or session.state.pending is not None:
            return
        access = session.controls.wallet_grant
        ref = getattr(getattr(access, "grant", None), "qa_reference", None)
        if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{64}", ref):
            return
        generation = session.state.generation
        if type(generation) is not int or generation < 1:
            return
        chat_hash = hashlib.sha256(chat.encode("ascii")).hexdigest()
        data = {"host":host, "chat_sha256":chat_hash, "grant_sha256":ref, "generation":generation}
        parent = os.open(runtime_parent(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(parent)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                return
            name = f"wallet-switch-{host}-{chat_hash}-{generation}.json"
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, "w") as dest:
                json.dump(data, dest, separators=(",", ":"))
        finally:
            os.close(parent)
    except (IntegrationError, OSError, ValueError):
        return
