"""Local tool host for OpenVegas chat runtime (no-plugin mode).

This module executes server-proposed tools inside the registered workspace and
returns structured outcomes for token-bound tool-result callbacks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from openvegas.contracts.errors import APIErrorCode
from openvegas.telemetry import emit_metric

MAX_FS_LIST_ENTRIES = 2000
MAX_FS_READ_BYTES = 262_144
MAX_FS_READ_RESULT_CONTENT_CHARS = 32_000
MAX_FS_SEARCH_FILES = 5000
MAX_FS_SEARCH_MATCHES = 2000
MAX_PATCH_BYTES = 1_048_576
MAX_PATCH_FILES = 50
MAX_PATCH_TOTAL_LINES = 5000
MAX_BACKGROUND_JOBS = 5


@dataclass
class BackgroundJob:
    job_id: str
    command: str
    process: asyncio.subprocess.Process
    start_ts: float
    status: str
    stdout: list[str]
    stderr: list[str]
    exit_code: int | None = None


_BACKGROUND_JOBS: dict[str, BackgroundJob] = {}


@dataclass(frozen=True)
class ToolExecutionResult:
    result_status: str
    result_payload: dict[str, Any]
    stdout: str
    stderr: str


def _sha256_hex_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_prefixed(text: str) -> str:
    return f"sha256:{_sha256_hex_utf8(text)}"


def _is_supported_platform() -> bool:
    system = platform.system().lower()
    return system in {"darwin", "linux"}


def _inside_root(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_under_root(root: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / candidate).resolve()
    if not _inside_root(root, resolved):
        raise ValueError(APIErrorCode.WORKSPACE_PATH_OUT_OF_BOUNDS.value)
    return resolved


def _is_binary_bytes(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _is_binary_file(path: Path) -> bool:
    with path.open("rb") as f:
        prefix = f.read(8192)
    return _is_binary_bytes(prefix)


def _safe_relpath(root: Path, target: Path) -> str:
    return str(target.relative_to(root)).replace(os.sep, "/")


def workspace_fingerprint(workspace_root: str, git_root: str | None = None) -> str:
    """Identity-oriented v1 fingerprint used for runtime/session binding."""
    del git_root
    root_real = str(Path(workspace_root).resolve())
    return _sha256_prefixed(root_real)


def _ok(payload: dict[str, Any], stdout: str = "", stderr: str = "") -> ToolExecutionResult:
    return ToolExecutionResult("succeeded", payload, stdout, stderr)


def _blocked(reason: APIErrorCode, detail: str, *, stdout: str = "", stderr: str = "") -> ToolExecutionResult:
    return ToolExecutionResult(
        "blocked",
        {
            "ok": False,
            "reason_code": reason.value,
            "detail": detail,
        },
        stdout,
        stderr,
    )


def _failed(reason: APIErrorCode, detail: str, *, stdout: str = "", stderr: str = "") -> ToolExecutionResult:
    return ToolExecutionResult(
        "failed",
        {
            "ok": False,
            "reason_code": reason.value,
            "detail": detail,
        },
        stdout,
        stderr,
    )


def _timed_out(detail: str, *, stdout: str = "", stderr: str = "") -> ToolExecutionResult:
    return ToolExecutionResult(
        "timed_out",
        {
            "ok": False,
            "reason_code": APIErrorCode.TOOL_TIMEOUT.value,
            "detail": detail,
        },
        stdout,
        stderr,
    )


def _normalize_tool_path_arg(arguments: dict[str, Any]) -> str:
    raw = arguments.get("path", ".")
    if raw is None:
        return "."
    return str(raw)


def _exec_fs_list(root: Path, arguments: dict[str, Any]) -> ToolExecutionResult:
    try:
        target = _resolve_under_root(root, _normalize_tool_path_arg(arguments))
    except ValueError:
        return _blocked(APIErrorCode.WORKSPACE_PATH_OUT_OF_BOUNDS, "Path escapes workspace root.")

    if not target.exists():
        return _failed(APIErrorCode.TOOL_EXECUTION_FAILED, f"Path does not exist: {target}")
    if not target.is_dir():
        return _failed(APIErrorCode.TOOL_EXECUTION_FAILED, f"Path is not a directory: {target}")

    recursive = bool(arguments.get("recursive", False))
    max_entries = int(arguments.get("max_entries", MAX_FS_LIST_ENTRIES))
    max_entries = max(1, min(max_entries, MAX_FS_LIST_ENTRIES))

    entries: list[dict[str, Any]] = []
    iterable = target.rglob("*") if recursive else target.iterdir()
    for p in sorted(iterable, key=lambda v: str(v).lower()):
        if len(entries) >= max_entries:
            break
        try:
            stat = p.stat()
        except OSError:
            continue
        entries.append(
            {
                "path": _safe_relpath(root, p),
                "kind": "dir" if p.is_dir() else "file",
                "size": int(stat.st_size),
            }
        )

    return _ok(
        {
            "ok": True,
            "path": _safe_relpath(root, target),
            "recursive": recursive,
            "entries": entries,
            "truncated": len(entries) >= max_entries,
            "max_entries": max_entries,
        }
    )


def _exec_fs_read(root: Path, arguments: dict[str, Any]) -> ToolExecutionResult:
    if "path" not in arguments:
        return _failed(APIErrorCode.TOOL_EXECUTION_FAILED, "fs_read requires argument: path")
    try:
        target = _resolve_under_root(root, str(arguments["path"]))
    except ValueError:
        return _blocked(APIErrorCode.WORKSPACE_PATH_OUT_OF_BOUNDS, "Path escapes workspace root.")

    if not target.exists() or not target.is_file():
        return _failed(APIErrorCode.TOOL_EXECUTION_FAILED, f"File not found: {target}")

    max_bytes = int(arguments.get("max_bytes", MAX_FS_READ_BYTES))
    max_bytes = max(1, min(max_bytes, MAX_FS_READ_BYTES))

    try:
        with target.open("rb") as f:
            data = f.read(max_bytes + 1)
    except OSError as e:
        return _failed(APIErrorCode.TOOL_EXECUTION_FAILED, f"Unable to read file: {e}")

    if _is_binary_bytes(data[:8192]):
        return _blocked(APIErrorCode.BINARY_FILE_UNSUPPORTED, "Binary files are not supported by fs_read.")

    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    text = data.decode("utf-8", errors="ignore")
    result_content_cap = int(arguments.get("result_content_max_chars", MAX_FS_READ_RESULT_CONTENT_CHARS))
    result_content_cap = max(256, min(result_content_cap, MAX_FS_READ_RESULT_CONTENT_CHARS))
    payload_content = text[:result_content_cap]
    payload_truncated = len(text) > result_content_cap

    return _ok(
        {
            "ok": True,
            "path": _safe_relpath(root, target),
            "content": payload_content,
            "content_truncated": payload_truncated,
            "truncated": truncated,
            "bytes_read": len(data),
            "max_bytes": max_bytes,
        },
        stdout=text,
    )


def _exec_fs_search(root: Path, arguments: dict[str, Any]) -> ToolExecutionResult:
    pattern = str(arguments.get("pattern", "")).strip()
    if not pattern:
        return _failed(APIErrorCode.TOOL_EXECUTION_FAILED, "fs_search requires argument: pattern")

    try:
        target = _resolve_under_root(root, _normalize_tool_path_arg(arguments))
    except ValueError:
        return _blocked(APIErrorCode.WORKSPACE_PATH_OUT_OF_BOUNDS, "Path escapes workspace root.")

    if not target.exists():
        return _failed(APIErrorCode.TOOL_EXECUTION_FAILED, f"Search path does not exist: {target}")

    recursive = bool(arguments.get("recursive", True))
    max_files = int(arguments.get("max_files", MAX_FS_SEARCH_FILES))
    max_files = max(1, min(max_files, MAX_FS_SEARCH_FILES))
    max_matches = int(arguments.get("max_matches", MAX_FS_SEARCH_MATCHES))
    max_matches = max(1, min(max_matches, MAX_FS_SEARCH_MATCHES))

    try:
        rx = re.compile(pattern)
        use_regex = True
    except re.error:
        rx = re.compile(re.escape(pattern))
        use_regex = False

    matches: list[dict[str, Any]] = []
    files_scanned = 0

    if target.is_file():
        files = [target]
    else:
        files = [p for p in (target.rglob("*") if recursive else target.iterdir()) if p.is_file()]
        files.sort(key=lambda v: str(v).lower())

    for file_path in files:
        if files_scanned >= max_files or len(matches) >= max_matches:
            break
        files_scanned += 1

        try:
            if _is_binary_file(file_path):
                continue
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for line_no, line in enumerate(text.splitlines(), start=1):
            if len(matches) >= max_matches:
                break
            m = rx.search(line)
            if not m:
                continue
            matches.append(
                {
                    "path": _safe_relpath(root, file_path),
                    "line": line_no,
                    "column": int(m.start() + 1),
                    "text": line[:2000],
                }
            )

    return _ok(
        {
            "ok": True,
            "pattern": pattern,
            "regex": use_regex,
            "path": _safe_relpath(root, target),
            "recursive": recursive,
            "files_scanned": files_scanned,
            "max_files": max_files,
            "matches": matches,
            "max_matches": max_matches,
            "truncated": len(matches) >= max_matches or files_scanned >= max_files,
        }
    )


def _extract_patch_targets_and_stats(patch_text: str) -> tuple[list[str], int, int]:
    targets: list[str] = []
    hunks = 0
    line_delta = 0
    for line in patch_text.splitlines():
        if line.startswith("+++"):
            raw = line[3:].strip()
            if raw != "/dev/null":
                if raw.startswith("b/"):
                    raw = raw[2:]
                targets.append(raw)
        elif line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            line_delta += 1
        elif line.startswith("-") and not line.startswith("---"):
            line_delta += 1
    # keep order deterministic, unique
    deduped = list(dict.fromkeys(targets))
    return deduped, hunks, line_delta


def _classify_patch_failure(*, stdout: str, stderr: str) -> str:
    blob = f"{stdout}\n{stderr}".lower()
    if "malformed patch" in blob or "only garbage was found in the patch input" in blob:
        return "patch_parse_invalid"
    if "can't find file to patch" in blob or "no such file or directory" in blob:
        return "patch_target_not_found"
    if "permission denied" in blob or "operation not permitted" in blob:
        return "patch_permission_denied"
    if "hunk #" in blob and "failed" in blob:
        return "patch_context_mismatch"
    return "patch_apply_failed"


def _run_patch(root: Path, patch_text: str, timeout_sec: int) -> tuple[int, str, str, int, list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    last_rc = 1
    last_out = ""
    last_err = ""
    patch_bin = shutil.which("patch")
    if patch_bin:
        for p_level in (1, 0):
            for ignore_ws in (False, True):
                dry_cmd = [patch_bin, f"-p{p_level}", "--batch", "--forward", "--dry-run"]
                if ignore_ws:
                    dry_cmd.insert(2, "-l")
                dry = subprocess.run(
                    dry_cmd,
                    input=patch_text,
                    text=True,
                    cwd=str(root),
                    capture_output=True,
                    timeout=timeout_sec,
                    check=False,
                )
                attempt: dict[str, Any] = {
                    "engine": "patch",
                    "p_level": p_level,
                    "ignore_whitespace": ignore_ws,
                    "dry_run_rc": int(dry.returncode),
                    "dry_run_stdout": dry.stdout or "",
                    "dry_run_stderr": dry.stderr or "",
                    "apply_rc": None,
                    "apply_stdout": "",
                    "apply_stderr": "",
                }
                attempts.append(attempt)
                last_rc = int(dry.returncode)
                last_out = dry.stdout or ""
                last_err = dry.stderr or ""
                if dry.returncode != 0:
                    continue

                apply_cmd = [patch_bin, f"-p{p_level}", "--batch", "--forward"]
                if ignore_ws:
                    apply_cmd.insert(2, "-l")
                apply = subprocess.run(
                    apply_cmd,
                    input=patch_text,
                    text=True,
                    cwd=str(root),
                    capture_output=True,
                    timeout=timeout_sec,
                    check=False,
                )
                attempt["apply_rc"] = int(apply.returncode)
                attempt["apply_stdout"] = apply.stdout or ""
                attempt["apply_stderr"] = apply.stderr or ""
                if apply.returncode == 0:
                    return int(apply.returncode), apply.stdout or "", apply.stderr or "", p_level, attempts
                last_rc = int(apply.returncode)
                last_out = apply.stdout or ""
                last_err = apply.stderr or ""

    git_bin = shutil.which("git")
    if git_bin:
        for p_level in (1, 0):
            for ignore_ws in (False, True):
                check_cmd = [git_bin, "apply", "--no-index", "--check", f"-p{p_level}"]
                apply_cmd = [git_bin, "apply", "--no-index", f"-p{p_level}"]
                if ignore_ws:
                    check_cmd.insert(3, "--ignore-space-change")
                    apply_cmd.insert(3, "--ignore-space-change")
                dry = subprocess.run(
                    check_cmd,
                    input=patch_text,
                    text=True,
                    cwd=str(root),
                    capture_output=True,
                    timeout=timeout_sec,
                    check=False,
                )
                attempt = {
                    "engine": "git_apply",
                    "p_level": p_level,
                    "ignore_whitespace": ignore_ws,
                    "dry_run_rc": int(dry.returncode),
                    "dry_run_stdout": dry.stdout or "",
                    "dry_run_stderr": dry.stderr or "",
                    "apply_rc": None,
                    "apply_stdout": "",
                    "apply_stderr": "",
                }
                attempts.append(attempt)
                last_rc = int(dry.returncode)
                last_out = dry.stdout or ""
                last_err = dry.stderr or ""
                if dry.returncode != 0:
                    continue
                apply = subprocess.run(
                    apply_cmd,
                    input=patch_text,
                    text=True,
                    cwd=str(root),
                    capture_output=True,
                    timeout=timeout_sec,
                    check=False,
                )
                attempt["apply_rc"] = int(apply.returncode)
                attempt["apply_stdout"] = apply.stdout or ""
                attempt["apply_stderr"] = apply.stderr or ""
                if apply.returncode == 0:
                    return int(apply.returncode), apply.stdout or "", apply.stderr or "", p_level, attempts
                last_rc = int(apply.returncode)
                last_out = apply.stdout or ""
                last_err = apply.stderr or ""

    if not patch_bin and not git_bin:
        return 127, "", "Neither patch nor git apply is available", 0, []
    return last_rc, last_out, last_err, -1, attempts


def _exec_fs_apply_patch(root: Path, arguments: dict[str, Any], timeout_sec: int) -> ToolExecutionResult:
    patch_text = str(arguments.get("patch", ""))
    if not patch_text:
        return _failed(APIErrorCode.TOOL_EXECUTION_FAILED, "fs_apply_patch requires argument: patch")

    patch_bytes = patch_text.encode("utf-8", errors="ignore")
    if len(patch_bytes) > MAX_PATCH_BYTES:
        return _blocked(APIErrorCode.TOOL_EXECUTION_FAILED, "Patch exceeds max bytes.")
    if "GIT binary patch" in patch_text or "\x00" in patch_text:
        return _blocked(APIErrorCode.BINARY_FILE_UNSUPPORTED, "Binary patches are not supported in v1.")

    targets, hunks, line_delta = _extract_patch_targets_and_stats(patch_text)
    if len(targets) > MAX_PATCH_FILES:
        return _blocked(APIErrorCode.TOOL_EXECUTION_FAILED, "Patch touches too many files.")
    if line_delta > MAX_PATCH_TOTAL_LINES:
        return _blocked(APIErrorCode.TOOL_EXECUTION_FAILED, "Patch changes too many lines.")

    for t in targets:
        try:
            _resolve_under_root(root, t)
        except ValueError:
            return _blocked(APIErrorCode.WORKSPACE_PATH_OUT_OF_BOUNDS, f"Patch target escapes root: {t}")

    try:
        rc, out, err, p_level, attempts = _run_patch(root, patch_text, timeout_sec=max(5, timeout_sec))
    except subprocess.TimeoutExpired as e:
        return _timed_out("Patch execution timed out.", stdout=e.stdout or "", stderr=e.stderr or "")

    if rc != 0:
        patch_failure_code = _classify_patch_failure(stdout=out, stderr=err)
        return ToolExecutionResult(
            "failed",
            {
                "ok": False,
                "reason_code": APIErrorCode.TOOL_EXECUTION_FAILED.value,
                "detail": "Patch apply failed; no changes were applied.",
                "patch_failure_code": patch_failure_code,
                "patch_diagnostics": {
                    "target_files": targets,
                    "hunks_attempted": hunks,
                    "line_delta": line_delta,
                    "dry_run_rc": int(rc),
                    "apply_rc": None if p_level < 0 else int(rc),
                    "selected_p_level": p_level,
                    "p_levels_attempted": [a.get("p_level") for a in attempts],
                    "attempts": attempts,
                },
            },
            out,
            err,
        )

    return _ok(
        {
            "ok": True,
            "files_targeted": targets,
            "hunks_attempted": hunks,
            "hunks_applied": hunks,
            "failure_reason": None,
            "p_level": p_level,
        },
        stdout=out,
        stderr=err,
    )


def _exec_shell_run(root: Path, arguments: dict[str, Any], timeout_sec: int) -> ToolExecutionResult:
    foreground_job_id = str(arguments.get("foreground_job_id", "")).strip()
    if foreground_job_id:
        job = _BACKGROUND_JOBS.get(foreground_job_id)
        if job is None:
            return _failed(APIErrorCode.TOOL_EXECUTION_FAILED, f"Unknown background job: {foreground_job_id}")
        payload = {
            "ok": job.status == "completed",
            "requested_command": job.command,
            "effective_command": job.command,
            "shell_wrapper": "/bin/bash -lc",
            "execution_cwd": str(root),
            "status": "running_in_background" if job.status == "running" else "foreground_result",
            "job_id": foreground_job_id,
            "exit_code": job.exit_code,
            "final_status_message": (
                f"Command is still running in background (job_id={foreground_job_id})"
                if job.status == "running"
                else (
                    "Background command completed"
                    if job.status == "completed"
                    else "Background command failed"
                )
            ),
        }
        return ToolExecutionResult(
            "succeeded" if job.status in {"running", "completed"} else "failed",
            payload,
            "".join(job.stdout),
            "".join(job.stderr),
        )

    command = str(arguments.get("command", "")).strip()
    if not command:
        return _failed(APIErrorCode.TOOL_EXECUTION_FAILED, "shell_run requires argument: command")

    wrapper = ["/bin/bash", "-lc", command]
    started = time.time()
    try:
        proc = subprocess.run(
            wrapper,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=max(1, timeout_sec),
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        return _timed_out(
            "Shell command timed out.",
            stdout=(e.stdout or ""),
            stderr=(e.stderr or ""),
        )

    duration_ms = int((time.time() - started) * 1000)
    payload = {
        "ok": proc.returncode == 0,
        "requested_command": command,
        "effective_command": command,
        "shell_wrapper": "/bin/bash -lc",
        "execution_cwd": str(root),
        "exit_code": int(proc.returncode),
        "duration_ms": duration_ms,
        "final_status_message": "Command completed" if proc.returncode == 0 else f"Command failed with exit code {proc.returncode}",
    }
    if proc.returncode == 0:
        return _ok(payload, stdout=proc.stdout or "", stderr=proc.stderr or "")
    payload["reason_code"] = APIErrorCode.TOOL_EXECUTION_FAILED.value
    return ToolExecutionResult(
        "failed",
        payload,
        proc.stdout or "",
        proc.stderr or "",
    )


async def execute_shell_run_streaming(
    *,
    workspace_root: str,
    arguments: dict[str, Any],
    timeout_sec: int,
    on_stdout: Callable[[str], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
) -> ToolExecutionResult:
    """Execute shell_run with streaming callbacks for interactive UX."""
    if not _is_supported_platform():
        return _blocked(APIErrorCode.UNSUPPORTED_PLATFORM, "Local runtime supports macOS/Linux in v1.")

    root = Path(workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        return _blocked(APIErrorCode.WORKSPACE_PATH_OUT_OF_BOUNDS, "Registered workspace root is invalid.")

    foreground_job_id = str((arguments or {}).get("foreground_job_id", "")).strip()
    if foreground_job_id:
        job = _BACKGROUND_JOBS.get(foreground_job_id)
        if job is None:
            return _failed(APIErrorCode.TOOL_EXECUTION_FAILED, f"Unknown background job: {foreground_job_id}")
        if job.status == "running":
            return _ok(
                {
                    "ok": True,
                    "requested_command": job.command,
                    "effective_command": job.command,
                    "shell_wrapper": "/bin/bash -lc",
                    "execution_cwd": str(root),
                    "status": "running_in_background",
                    "job_id": foreground_job_id,
                    "final_status_message": f"Command is still running in background (job_id={foreground_job_id})",
                },
                stdout="".join(job.stdout),
                stderr="".join(job.stderr),
            )
        payload = {
            "ok": job.status == "completed",
            "requested_command": job.command,
            "effective_command": job.command,
            "shell_wrapper": "/bin/bash -lc",
            "execution_cwd": str(root),
            "status": "foreground_result",
            "job_id": foreground_job_id,
            "exit_code": job.exit_code,
            "final_status_message": (
                "Background command completed" if job.status == "completed" else "Background command failed"
            ),
        }
        result = ToolExecutionResult(
            "succeeded" if job.status == "completed" else "failed",
            payload,
            "".join(job.stdout),
            "".join(job.stderr),
        )
        _BACKGROUND_JOBS.pop(foreground_job_id, None)
        return result

    command = str((arguments or {}).get("command", "")).strip()
    if not command:
        return _failed(APIErrorCode.TOOL_EXECUTION_FAILED, "shell_run requires argument: command")

    run_in_background = bool((arguments or {}).get("background", False))
    if run_in_background and len(_BACKGROUND_JOBS) >= MAX_BACKGROUND_JOBS:
        return _failed(
            APIErrorCode.TOOL_EXECUTION_FAILED,
            f"Background job limit reached ({MAX_BACKGROUND_JOBS}).",
        )

    proc = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-lc",
        command,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    last_chunk_ts = time.time()

    async def _pump(stream: Any, sink: list[str], cb: Callable[[str], None] | None) -> None:
        nonlocal last_chunk_ts
        while True:
            chunk = await stream.readline()
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="ignore")
            now = time.time()
            emit_metric(
                "tool_stream_chunk_lag_ms",
                {"stream": "stdout" if sink is stdout_chunks else "stderr"},
                int(max(0.0, (now - last_chunk_ts) * 1000)),
            )
            last_chunk_ts = now
            sink.append(text)
            if cb is not None:
                try:
                    cb(text)
                except Exception:
                    pass

    start_ts = time.time()
    out_task = asyncio.create_task(_pump(proc.stdout, stdout_chunks, on_stdout))
    err_task = asyncio.create_task(_pump(proc.stderr, stderr_chunks, on_stderr))
    if run_in_background:
        job_id = f"bg-{int(start_ts)}-{proc.pid}"
        _BACKGROUND_JOBS[job_id] = BackgroundJob(
            job_id=job_id,
            command=command,
            process=proc,
            start_ts=start_ts,
            status="running",
            stdout=stdout_chunks,
            stderr=stderr_chunks,
        )

        async def _background_finalize() -> None:
            try:
                await proc.wait()
                await asyncio.gather(out_task, err_task, return_exceptions=True)
                job = _BACKGROUND_JOBS.get(job_id)
                if job is not None:
                    job.exit_code = int(proc.returncode)
                    job.status = "completed" if proc.returncode == 0 else "failed"
            except Exception:
                job = _BACKGROUND_JOBS.get(job_id)
                if job is not None:
                    job.status = "failed"
            finally:
                emit_metric("tool_background_job_total", {"status": _BACKGROUND_JOBS.get(job_id).status if job_id in _BACKGROUND_JOBS else "unknown"})

        asyncio.create_task(_background_finalize())
        return _ok(
            {
                "ok": True,
                "requested_command": command,
                "effective_command": command,
                "shell_wrapper": "/bin/bash -lc",
                "execution_cwd": str(root),
                "status": "running_in_background",
                "job_id": job_id,
                "final_status_message": f"Command is running in the background (job_id={job_id})",
            }
        )

    try:
        await asyncio.wait_for(proc.wait(), timeout=max(1, int(timeout_sec)))
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        await asyncio.gather(out_task, err_task, return_exceptions=True)
        return _timed_out(
            "Shell command timed out.",
            stdout="".join(stdout_chunks),
            stderr="".join(stderr_chunks),
        )

    await asyncio.gather(out_task, err_task, return_exceptions=True)
    stdout_text = "".join(stdout_chunks)
    stderr_text = "".join(stderr_chunks)
    duration_ms = int((time.time() - start_ts) * 1000)
    payload = {
        "ok": proc.returncode == 0,
        "requested_command": command,
        "effective_command": command,
        "shell_wrapper": "/bin/bash -lc",
        "execution_cwd": str(root),
        "exit_code": int(proc.returncode),
        "duration_ms": duration_ms,
        "final_status_message": "Command completed" if proc.returncode == 0 else f"Command failed with exit code {proc.returncode}",
    }
    if proc.returncode == 0:
        return _ok(payload, stdout=stdout_text, stderr=stderr_text)
    payload["reason_code"] = APIErrorCode.TOOL_EXECUTION_FAILED.value
    return ToolExecutionResult("failed", payload, stdout_text, stderr_text)


def _exec_editor_open(root: Path, arguments: dict[str, Any], timeout_sec: int) -> ToolExecutionResult:
    raw_path = str(arguments.get("path", "")).strip()
    if not raw_path:
        return _failed(APIErrorCode.TOOL_EXECUTION_FAILED, "editor_open requires argument: path")

    try:
        target = _resolve_under_root(root, raw_path)
    except ValueError:
        return _blocked(APIErrorCode.WORKSPACE_PATH_OUT_OF_BOUNDS, "Path escapes workspace root.")

    editor_cmd = str(arguments.get("command") or os.getenv("OPENVEGAS_EDITOR") or "code").strip()
    bin_path = shutil.which(editor_cmd)
    if not bin_path:
        return _blocked(APIErrorCode.EDITOR_UNAVAILABLE, f"Editor launcher not found: {editor_cmd}")

    line = int(arguments.get("line", 1) or 1)
    col = int(arguments.get("col", 1) or 1)

    cmd: list[str]
    if Path(editor_cmd).name in {"code", "codium"}:
        cmd = [bin_path, "-g", f"{str(target)}:{line}:{col}"]
    else:
        cmd = [bin_path, str(target)]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=max(1, min(timeout_sec, 15)),
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        return _timed_out("Editor open timed out.", stdout=e.stdout or "", stderr=e.stderr or "")

    payload = {
        "opened": proc.returncode == 0,
        "dispatch_only": True,
        "path": _safe_relpath(root, target),
        "line": line,
        "col": col,
        "command": editor_cmd,
    }
    if proc.returncode == 0:
        return _ok(payload, stdout=proc.stdout or "", stderr=proc.stderr or "")
    payload["reason_code"] = APIErrorCode.EDITOR_OPEN_FAILED.value
    return ToolExecutionResult("failed", payload, proc.stdout or "", proc.stderr or "")


def execute_tool_request(
    *,
    workspace_root: str,
    tool_name: str,
    arguments: dict[str, Any],
    shell_mode: str | None,
    timeout_sec: int,
) -> ToolExecutionResult:
    del shell_mode  # server validates tool mutability and policy at propose-time

    if not _is_supported_platform():
        return _blocked(APIErrorCode.UNSUPPORTED_PLATFORM, "Local runtime supports macOS/Linux in v1.")

    root = Path(workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        return _blocked(APIErrorCode.WORKSPACE_PATH_OUT_OF_BOUNDS, "Registered workspace root is invalid.")

    name = str(tool_name).strip()
    args = arguments or {}
    t_sec = max(1, int(timeout_sec or 30))

    if name == "fs_list":
        return _exec_fs_list(root, args)
    if name == "fs_read":
        return _exec_fs_read(root, args)
    if name == "fs_search":
        return _exec_fs_search(root, args)
    if name == "fs_apply_patch":
        return _exec_fs_apply_patch(root, args, timeout_sec=t_sec)
    if name == "shell_run":
        return _exec_shell_run(root, args, timeout_sec=t_sec)
    if name == "editor_open":
        return _exec_editor_open(root, args, timeout_sec=t_sec)

    return _blocked(APIErrorCode.INVALID_TRANSITION, f"Unknown tool: {name}")


def extract_tool_instruction(text: str) -> tuple[dict[str, Any] | None, str]:
    """Parse a tool-call JSON object if present.

    Returns `(tool_request_or_none, display_text)` where display_text is the assistant
    content with optional fenced JSON removed when parse succeeds.
    """
    raw = (text or "").strip()
    if not raw:
        return None, ""

    fence_match = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", raw, flags=re.IGNORECASE)
    candidate = fence_match.group(1) if fence_match else None
    if candidate is None:
        brace_start = raw.find("{")
        brace_end = raw.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            candidate = raw[brace_start : brace_end + 1]

    if not candidate:
        return None, raw

    try:
        obj = json.loads(candidate)
    except Exception:
        return None, raw

    if not isinstance(obj, dict):
        return None, raw

    if obj.get("type") not in {"tool_call", "tool_request"}:
        return None, raw

    if "tool_name" not in obj or "arguments" not in obj:
        return None, raw

    cleaned = raw
    if fence_match:
        cleaned = raw.replace(fence_match.group(0), "").strip()

    return obj, cleaned
