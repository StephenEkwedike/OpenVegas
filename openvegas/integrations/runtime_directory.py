"""Select the parent for private POSIX terminal runtime files, not user settings."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .credentials import IntegrationError

RUNTIME_ENV = "OPENVEGAS_PRIVATE_RUNTIME_DIR"


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
