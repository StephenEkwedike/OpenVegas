"""Legacy compatibility wrapper for approval selection."""

from __future__ import annotations

from openvegas.tui.approval_menu import ApprovalDecision, choose_approval as choose_approval_minimal


def choose_approval(
    *,
    tool_name: str,
    arguments: dict | None = None,
    action_label: str,
    console,
) -> ApprovalDecision:
    return choose_approval_minimal(
        tool_name=tool_name,
        arguments=arguments if isinstance(arguments, dict) else {},
        action_label=action_label,
        console=console,
    )
