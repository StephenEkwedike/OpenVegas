"""Map tool lifecycle events to CLI avatar/dealer states."""

from __future__ import annotations

READING_TOOLS = {
    "fs_read",
    "fs_search",
    "glob",
    "web_fetch",
    "web_search",
    "list",
    "fs_list",
}


def map_tool_event_to_avatar_state(tool_name: str, status: str) -> str:
    tool = str(tool_name or "").strip().lower()
    state = str(status or "").strip().lower()
    if state == "waiting":
        return "waiting"
    if state in {"succeeded", "success"}:
        return "success"
    if state in {"failed", "error", "timed_out", "blocked"}:
        return "error"
    if tool in READING_TOOLS:
        return "reading"
    return "typing"


def map_lifecycle_event_to_state(event_type: str, tool_name: str = "", status: str = "") -> str:
    evt = str(event_type or "").strip().lower()
    if evt == "approval_wait":
        return "waiting"
    if evt == "finalize":
        return "idle"
    if evt in {"tool_start", "tool_result"}:
        return map_tool_event_to_avatar_state(tool_name, status)
    return "idle"
