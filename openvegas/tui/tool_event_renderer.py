"""Human-friendly tool event rendering for chat flow."""

from __future__ import annotations

import re
from typing import Any

from rich.console import Console


FRIENDLY_TOOL_NAMES = {
    "fs_read": "Read file",
    "fs_search": "Search code",
    "fs_apply_patch": "Apply patch",
    "shell_run": "Run shell command",
    "fs_list": "List files",
    "editor_open": "Open editor",
}


def friendly_tool_name(tool_name: str) -> str:
    token = str(tool_name or "").strip()
    return FRIENDLY_TOOL_NAMES.get(token, token or "Tool")


def _patch_target_from_args(arguments: dict[str, Any]) -> str | None:
    for key in ("path", "file_path", "filepath", "file"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    patch = arguments.get("patch")
    if isinstance(patch, str):
        m = re.search(r"(?m)^\+\+\+\s+([^\n]+)$", patch)
        if m:
            return m.group(1).strip()
    return None


def describe_tool_action(tool_name: str, arguments: dict[str, Any]) -> str:
    name = str(tool_name or "").strip()
    args = arguments if isinstance(arguments, dict) else {}
    if name == "fs_read":
        path = args.get("path")
        if isinstance(path, str) and path.strip():
            return f"read file {path.strip()}"
        return "read a file"
    if name == "fs_search":
        pattern = args.get("pattern")
        if isinstance(pattern, str) and pattern.strip():
            return f'search code for "{pattern.strip()}"'
        return "search code"
    if name == "fs_apply_patch":
        target = _patch_target_from_args(args)
        if target:
            return f"apply patch to {target}"
        return "apply a file patch"
    if name == "shell_run":
        command = args.get("command")
        if isinstance(command, str) and command.strip():
            trimmed = command.strip()
            if len(trimmed) > 96:
                trimmed = trimmed[:93] + "..."
            return f'run shell command "{trimmed}"'
        return "run a shell command"
    if name == "fs_list":
        path = args.get("path")
        if isinstance(path, str) and path.strip():
            return f"list files in {path.strip()}"
        return "list files"
    if name == "editor_open":
        path = args.get("path")
        if isinstance(path, str) and path.strip():
            return f"open editor at {path.strip()}"
        return "open editor"
    return friendly_tool_name(name).lower()


def render_tool_event(
    console: Console,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    tool_call_id: str | None = None,
    verbose: bool = False,
) -> None:
    console.print(f"• {friendly_tool_name(tool_name)}")
    if verbose:
        detail = describe_tool_action(tool_name, arguments)
        if detail:
            console.print(f"  [dim]{detail}[/dim]")
        if tool_call_id:
            console.print(f"  [dim]id={tool_call_id}[/dim]")


def render_tool_result(
    console: Console,
    *,
    tool_name: str,
    result_status: str,
    stdout: str = "",
    stderr: str = "",
    verbose: bool = False,
) -> None:
    status = str(result_status or "").strip() or "unknown"
    console.print(f"  [dim]└ {friendly_tool_name(tool_name)}: {status}[/dim]")
    if verbose:
        if stdout:
            lines = len(stdout.splitlines())
            console.print(f"    [dim]stdout lines: {lines}[/dim]")
        if stderr:
            lines = len(stderr.splitlines())
            console.print(f"    [dim]stderr lines: {lines}[/dim]")

