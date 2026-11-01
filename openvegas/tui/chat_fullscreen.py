"""Fullscreen chat fallback shim.

This module preserves the `run_chat_fullscreen` interface expected by
`openvegas.cli.chat` while defaulting users back to the classic terminal chat
experience. Fullscreen remains opt-in and can be reintroduced without touching
callers.
"""

from __future__ import annotations

import json
from typing import Any


def run_chat_fullscreen(**_: Any) -> str:
    """Return a legacy handoff payload understood by the classic chat loop."""
    return json.dumps(
        {
            "action": "handoff_legacy",
            "message": "Switched to classic chat.",
        }
    )
