"""Feature flag helpers for runtime capability gating."""

from __future__ import annotations

import os


def flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def features() -> dict[str, bool]:
    return {
        "global_enabled": flag("OPENVEGAS_FEATURES_ENABLED", "1"),
        "web_search": flag("OPENVEGAS_ENABLE_WEB_SEARCH", "1"),
        "files": flag("OPENVEGAS_ENABLE_FILES", "0"),
        "vision": flag("OPENVEGAS_ENABLE_VISION", "0"),
        "mcp": flag("OPENVEGAS_ENABLE_MCP", "0"),
        "code_exec": flag("OPENVEGAS_ENABLE_CODE_EXEC", "0"),
        "image_gen": flag("OPENVEGAS_ENABLE_IMAGE_GEN", "0"),
        "realtime_voice": flag("OPENVEGAS_ENABLE_REALTIME_VOICE", "0"),
    }
