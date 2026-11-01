"""Best-effort VSCode bridge adapter."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

from openvegas.ide.bridge_types import IDEContext, ShowDiffResult
from openvegas.ide.show_diff import (
    build_show_diff_result,
    is_valid_show_diff_payload,
    normalize_show_diff_result,
    read_text_best_effort,
    redact_show_diff_payload_shape,
)
from openvegas.telemetry import emit_metric


class VSCodeBridge:
    def __init__(self, workspace_root: str):
        self.workspace_root = str(Path(workspace_root).resolve())
        self.diff_timeout_sec = max(1.0, float(os.getenv("OPENVEGAS_IDE_INTERACTIVE_DIFF_TIMEOUT_SEC", "12")))

    @staticmethod
    def _trace_enabled() -> bool:
        return os.getenv("OPENVEGAS_IDE_BRIDGE_TRACE", "").strip().lower() in {"1", "true", "yes", "on"}

    def _trace(self, message: str) -> None:
        if self._trace_enabled():
            print(f"[ide-bridge-trace][vscode] {message}", flush=True)

    @staticmethod
    def _bridge_dir() -> Path:
        base = Path.home() / ".openvegas" / "ide_bridge"
        base.mkdir(parents=True, exist_ok=True)
        return base

    @staticmethod
    def _is_uri_path(path: str) -> bool:
        return "://" in path

    def _normalize_open_target(self, path: str) -> str:
        raw = str(path).strip()
        if self._is_uri_path(raw):
            return raw
        return str(Path(raw).resolve())

    async def open_file(self, path: str, line: int | None = None, col: int | None = None) -> None:
        code_bin = shutil.which("code")
        if not code_bin:
            raise RuntimeError("vscode launcher not available")
        target = self._normalize_open_target(path)
        if line is not None:
            goto = f"{target}:{max(1, int(line))}:{max(1, int(col or 1))}"
            proc = await asyncio.create_subprocess_exec(code_bin, "--goto", goto)
        else:
            proc = await asyncio.create_subprocess_exec(code_bin, target)
        rc = await proc.wait()
        if rc != 0:
            raise RuntimeError("editor_open_failed")

    async def run_command(self, command: str, terminal_name: str | None = None) -> None:
        code_bin = shutil.which("code")
        if not code_bin:
            raise RuntimeError("vscode launcher not available")
        args = [code_bin, "--command", command]
        if terminal_name:
            args.extend(["--reuse-window"])
        proc = await asyncio.create_subprocess_exec(*args)
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
        if os.getenv("OPENVEGAS_IDE_SHOW_DIFF_INTERACTIVE_TIMEOUT", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            emit_metric(
                "tool_diff_fallback_total",
                {"from": "ide_interactive", "to": "terminal", "reason": "timeout"},
            )
            raise asyncio.TimeoutError("interactive_show_diff_timeout")

        payload_raw = os.getenv("OPENVEGAS_IDE_SHOW_DIFF_INTERACTIVE_PAYLOAD", "").strip()
        if payload_raw:
            try:
                parsed = json.loads(payload_raw)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                normalized = normalize_show_diff_result(parsed, default_path=path)
                if is_valid_show_diff_payload(parsed):
                    return normalized
                self._trace(
                    "interactive malformed payload shape="
                    + json.dumps(redact_show_diff_payload_shape(parsed), sort_keys=True, ensure_ascii=False)
                )
                emit_metric(
                    "tool_diff_fallback_total",
                    {"from": "ide_interactive", "to": "terminal", "reason": "malformed_payload"},
                )
                raise ValueError("malformed_show_diff_payload")

            emit_metric(
                "tool_diff_fallback_total",
                {"from": "ide_interactive", "to": "terminal", "reason": "malformed_payload"},
            )
            raise ValueError("invalid_show_diff_payload_json")

        use_command = os.getenv("OPENVEGAS_IDE_SHOW_DIFF_INTERACTIVE_USE_COMMAND", "1").strip().lower()
        if use_command in {"0", "false", "no", "off"}:
            emit_metric(
                "tool_diff_fallback_total",
                {"from": "ide_interactive", "to": "terminal", "reason": "bridge_error"},
            )
            return await self.show_diff(path, new_contents, allow_partial_accept=allow_partial_accept)

        code_bin = shutil.which("code")
        if not code_bin:
            emit_metric(
                "tool_diff_fallback_total",
                {"from": "ide_interactive", "to": "terminal", "reason": "bridge_error"},
            )
            return await self.show_diff(path, new_contents, allow_partial_accept=allow_partial_accept)

        request_id = f"ov-{int(time.time() * 1000)}"
        bridge_dir = self._bridge_dir()
        request_path = bridge_dir / "show_diff_request.json"
        response_path = bridge_dir / f"show_diff_response_{request_id}.json"
        try:
            response_path.unlink(missing_ok=True)
        except Exception:
            pass
        request_payload = {
            "request_id": request_id,
            "path": str(Path(path).resolve()) if not self._is_uri_path(path) else path,
            "new_contents": new_contents,
            "allow_partial_accept": bool(allow_partial_accept),
        }
        request_path.write_text(json.dumps(request_payload, ensure_ascii=False), encoding="utf-8")

        proc = await asyncio.create_subprocess_exec(
            code_bin,
            "--reuse-window",
            "--command",
            "openvegas.showDiffInteractive",
        )
        rc = await proc.wait()
        if rc != 0:
            emit_metric(
                "tool_diff_fallback_total",
                {"from": "ide_interactive", "to": "terminal", "reason": "bridge_error"},
            )
            return await self.show_diff(path, new_contents, allow_partial_accept=allow_partial_accept)

        deadline = time.monotonic() + self.diff_timeout_sec
        while time.monotonic() < deadline:
            if response_path.exists():
                try:
                    raw = json.loads(response_path.read_text(encoding="utf-8"))
                except Exception:
                    emit_metric(
                        "tool_diff_fallback_total",
                        {"from": "ide_interactive", "to": "terminal", "reason": "malformed_payload"},
                    )
                    raise ValueError("malformed_show_diff_payload")
                normalized = normalize_show_diff_result(raw, default_path=path)
                if not is_valid_show_diff_payload(raw if isinstance(raw, dict) else None):
                    self._trace(
                        "interactive malformed payload shape="
                        + json.dumps(redact_show_diff_payload_shape(raw), sort_keys=True, ensure_ascii=False)
                    )
                    emit_metric(
                        "tool_diff_fallback_total",
                        {"from": "ide_interactive", "to": "terminal", "reason": "malformed_payload"},
                    )
                    raise ValueError("malformed_show_diff_payload")
                return normalized
            await asyncio.sleep(0.2)

        emit_metric(
            "tool_diff_fallback_total",
            {"from": "ide_interactive", "to": "terminal", "reason": "timeout"},
        )
        raise asyncio.TimeoutError("interactive_show_diff_timeout")

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
