"""Typed bridge protocol for IDE adapters."""

from __future__ import annotations

from typing import Any, Literal, Protocol, TypedDict


class CursorPosition(TypedDict):
    file_path: str
    line: int
    col: int


class DiagnosticItem(TypedDict):
    file_path: str
    line: int
    col: int
    severity: Literal["error", "warning", "info", "hint"]
    message: str
    source: str | None


class SelectionContext(TypedDict):
    file_path: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    selected_text: str


class TerminalHistoryEntry(TypedDict):
    terminal_name: str
    last_command: str | None
    recent_output: str
    exit_code: int | None


class IDEContext(TypedDict):
    open_files: list[str]
    active_file: str | None
    cursor: CursorPosition | None
    selection: SelectionContext | None
    diagnostics: list[DiagnosticItem]
    terminal_history: list[TerminalHistoryEntry]


class DiffHunkDecision(TypedDict):
    hunk_index: int
    decision: Literal["accepted", "rejected"]


class ShowDiffResult(TypedDict):
    file_path: str
    hunks_total: int
    decisions: list[DiffHunkDecision]
    all_accepted: bool
    timed_out: bool


class IDEEnvelope(TypedDict):
    id: str
    type: Literal["request", "response", "event"]
    method: str
    params: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None


class IDEBridge(Protocol):
    async def open_file(self, path: str, line: int | None = None, col: int | None = None) -> None: ...

    async def run_command(self, command: str, terminal_name: str | None = None) -> None: ...

    async def show_diff(
        self, path: str, new_contents: str, allow_partial_accept: bool = True
    ) -> ShowDiffResult: ...

    async def show_diff_interactive(
        self, path: str, new_contents: str, allow_partial_accept: bool = True
    ) -> ShowDiffResult: ...

    async def get_open_files(self) -> list[str]: ...

    async def read_buffer(self, path: str) -> str | None: ...

    async def get_context(self) -> IDEContext: ...
