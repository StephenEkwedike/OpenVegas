"""Best-effort JetBrains bridge adapter."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from openvegas.ide.bridge_types import IDEContext, ShowDiffResult
from openvegas.ide.show_diff import build_show_diff_result, read_text_best_effort


class JetBrainsBridge:
    def __init__(self, workspace_root: str):
        self.workspace_root = str(Path(workspace_root).resolve())

    @staticmethod
    def _is_uri_path(path: str) -> bool:
        return "://" in path

    def _normalize_open_target(self, path: str) -> str:
        raw = str(path).strip()
        if self._is_uri_path(raw):
            return raw
        return str(Path(raw).resolve())

    async def open_file(self, path: str, line: int | None = None, col: int | None = None) -> None:
        del line, col
        launcher = shutil.which("idea") or shutil.which("charm") or shutil.which("pycharm")
        if not launcher:
            raise RuntimeError("jetbrains launcher not available")
        proc = await asyncio.create_subprocess_exec(launcher, self._normalize_open_target(path))
        rc = await proc.wait()
        if rc != 0:
            raise RuntimeError("editor_open_failed")

    async def run_command(self, command: str, terminal_name: str | None = None) -> None:
        del terminal_name
        launcher = shutil.which("idea") or shutil.which("charm") or shutil.which("pycharm")
        if not launcher:
            raise RuntimeError("jetbrains launcher not available")
        proc = await asyncio.create_subprocess_exec(launcher, "--command", command)
        rc = await proc.wait()
        if rc != 0:
            raise RuntimeError("run_command_failed")

    async def show_diff(
        self, path: str, new_contents: str, allow_partial_accept: bool = True
    ) -> ShowDiffResult:
        current = "" if self._is_uri_path(path) else read_text_best_effort(path)
        return build_show_diff_result(
            path=path,
            current_contents=current,
            new_contents=new_contents,
            allow_partial_accept=allow_partial_accept,
        )

    async def show_diff_interactive(
        self,
        path: str,
        new_contents: str,
        allow_partial_accept: bool = True,
    ) -> ShowDiffResult:
        return await self.show_diff(path, new_contents, allow_partial_accept=allow_partial_accept)

    async def get_open_files(self) -> list[str]:
        return []

    async def read_buffer(self, path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8")
        except Exception:
            return None

    async def get_context(self) -> IDEContext:
        return {
            "open_files": [],
            "active_file": None,
            "cursor": None,
            "selection": None,
            "diagnostics": [],
            "terminal_history": [],
        }
