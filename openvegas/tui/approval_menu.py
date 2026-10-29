"""Approval menu primitives for mutating tool calls."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from rich.console import Console
from rich.prompt import Confirm

try:
    import curses
except ImportError:  # Standard Windows Python has no curses backend.
    curses = None


class ApprovalDecision(str, Enum):
    ALLOW_ONCE = "allow_once"
    ALWAYS_THIS_SCOPE = "always_this_scope"
    DENY_AND_REPLAN = "deny_and_replan"


@dataclass
class SessionApprovalState:
    allow_scopes: set[str] = field(default_factory=set)


def _looks_network_shell(command: str) -> bool:
    lowered = str(command or "").lower()
    if not lowered:
        return False
    return bool(re.search(r"\b(curl|wget|http://|https://|pip\s+install|npm\s+install)\b", lowered))


def action_scope_for(tool_name: str, arguments: dict | None) -> str:
    name = str(tool_name or "").strip()
    args = arguments if isinstance(arguments, dict) else {}
    if name == "fs_read":
        return "read"
    if name == "fs_search":
        return "search"
    if name == "fs_apply_patch":
        return "edit_apply_patch"
    if name == "shell_run":
        command = str(args.get("command") or "")
        return "shell_networked" if _looks_network_shell(command) else "shell_local"
    if name == "editor_open":
        return "read"
    return f"tool:{name or 'unknown'}"


def should_auto_allow(state: SessionApprovalState, action_scope: str) -> bool:
    scope = str(action_scope or "").strip()
    return bool(scope and scope in state.allow_scopes)


def apply_approval_decision(state: SessionApprovalState, action_scope: str, decision: ApprovalDecision) -> None:
    scope = str(action_scope or "").strip()
    if decision == ApprovalDecision.ALWAYS_THIS_SCOPE and scope:
        state.allow_scopes.add(scope)


def approval_rules_summary(state: SessionApprovalState) -> str:
    scopes = sorted(state.allow_scopes)
    scopes_display = ", ".join(scopes) if scopes else "(none)"
    return f"Always-allowed action scopes this chat: {scopes_display}"


def _render_inline_menu(console: Console, action_label: str) -> None:
    console.print(
        f"Permission Required: Allow to {action_label}?\n"
        "  Yes\n"
        "  Always yes for this action type (this chat)\n"
        "  No, do something different"
    )


def _choose_with_curses(action_label: str) -> ApprovalDecision:
    labels = (
        "Yes",
        "Always yes for this action type (this chat)",
        "No, do something different",
    )

    def _runner(stdscr) -> ApprovalDecision:
        selected = 0
        stdscr.keypad(True)
        curses.curs_set(0)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            # White on blue highlight.
            curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)
        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            header = f"Allow to {action_label}?"
            stdscr.addnstr(0, 0, header, max(1, width - 1))
            for idx, label in enumerate(labels):
                prefix = "› " if idx == selected else "  "
                row = 2 + idx
                if row >= height - 1:
                    break
                attr = curses.A_BOLD if idx == selected else curses.A_NORMAL
                if idx == selected and curses.has_colors():
                    attr |= curses.color_pair(1)
                stdscr.addnstr(row, 0, f"{prefix}{label}", max(1, width - 1), attr)
            hint = "Use ↑/↓ then Enter. Press q or Esc to deny."
            if height > 6:
                stdscr.addnstr(height - 1, 0, hint, max(1, width - 1))
            stdscr.refresh()
            key = stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % len(labels)
                continue
            if key in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % len(labels)
                continue
            if key in (10, 13, curses.KEY_ENTER):
                if selected == 0:
                    return ApprovalDecision.ALLOW_ONCE
                if selected == 1:
                    return ApprovalDecision.ALWAYS_THIS_SCOPE
                return ApprovalDecision.DENY_AND_REPLAN
            if key in (27, ord("q")):
                return ApprovalDecision.DENY_AND_REPLAN

    return curses.wrapper(_runner)


def _is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:  # noqa: BLE001 - unknown terminal wrappers must fail closed
        return False


def choose_approval(
    *,
    tool_name: str,
    arguments: dict | None,
    action_label: str,
    console: Console,
    selector_fn: Callable[[str], ApprovalDecision] | None = None,
) -> ApprovalDecision:
    _render_inline_menu(console, action_label)
    if selector_fn is not None:
        return selector_fn(action_label)
    try:
        if curses is None or not _is_tty():
            allowed = Confirm.ask(f"Allow to {action_label}?", default=False)
            return ApprovalDecision.ALLOW_ONCE if allowed else ApprovalDecision.DENY_AND_REPLAN
        return _choose_with_curses(action_label)
    except KeyboardInterrupt:
        return ApprovalDecision.DENY_AND_REPLAN
    except Exception:  # noqa: BLE001 - every failed approval UI must deny mutation
        # Fail closed to avoid accidental mutation if selector cannot run.
        return ApprovalDecision.DENY_AND_REPLAN


def prompt_approval_menu(
    *,
    tool_name: str,
    action_label: str,
    console: Console,
    selector_fn: Callable[[str], ApprovalDecision] | None = None,
) -> ApprovalDecision:
    """Backward-compatible alias for prior tests/callers."""
    return choose_approval(
        tool_name=tool_name,
        arguments={},
        action_label=action_label,
        console=console,
        selector_fn=selector_fn,
    )
