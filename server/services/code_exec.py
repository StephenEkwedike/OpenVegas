"""Sandboxed code execution job service."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _allow_network() -> bool:
    return str(os.getenv("OPENVEGAS_CODE_EXEC_ALLOW_NETWORK", "0")).strip().lower() in {"1", "true", "yes", "on"}


def _max_output_chars() -> int:
    raw = str(os.getenv("OPENVEGAS_CODE_EXEC_MAX_OUTPUT_CHARS", "12000")).strip()
    try:
        return max(1024, min(200000, int(raw)))
    except Exception:
        return 12000


def _max_code_chars() -> int:
    raw = str(os.getenv("OPENVEGAS_CODE_EXEC_MAX_CODE_CHARS", "40000")).strip()
    try:
        return max(256, min(300000, int(raw)))
    except Exception:
        return 40000


def _runtime_kind() -> str:
    raw = str(os.getenv("OPENVEGAS_CODE_EXEC_RUNTIME", "local")).strip().lower()
    return raw if raw in {"local", "docker"} else "local"


def _container_image() -> str:
    return str(os.getenv("OPENVEGAS_CODE_EXEC_CONTAINER_IMAGE", "python:3.11-alpine")).strip() or "python:3.11-alpine"


def _container_memory_mb() -> int:
    raw = str(os.getenv("OPENVEGAS_CODE_EXEC_CONTAINER_MEMORY_MB", "512")).strip()
    try:
        return max(128, min(8192, int(raw)))
    except Exception:
        return 512


def _container_cpus() -> float:
    raw = str(os.getenv("OPENVEGAS_CODE_EXEC_CONTAINER_CPUS", "1.0")).strip()
    try:
        return max(0.1, min(8.0, float(raw)))
    except Exception:
        return 1.0


def _max_artifacts() -> int:
    raw = str(os.getenv("OPENVEGAS_CODE_EXEC_MAX_ARTIFACTS", "10")).strip()
    try:
        return max(0, min(100, int(raw)))
    except Exception:
        return 10


def _artifact_max_bytes() -> int:
    raw = str(os.getenv("OPENVEGAS_CODE_EXEC_ARTIFACT_MAX_BYTES", "5242880")).strip()
    try:
        return max(1024, min(50_000_000, int(raw)))
    except Exception:
        return 5_242_880


def _artifact_inline_max_bytes() -> int:
    raw = str(os.getenv("OPENVEGAS_CODE_EXEC_ARTIFACT_INLINE_MAX_BYTES", "262144")).strip()
    try:
        return max(256, min(_artifact_max_bytes(), int(raw)))
    except Exception:
        return 262_144


def _artifact_text_preview_chars() -> int:
    raw = str(os.getenv("OPENVEGAS_CODE_EXEC_ARTIFACT_TEXT_PREVIEW_CHARS", "4000")).strip()
    try:
        return max(128, min(20000, int(raw)))
    except Exception:
        return 4000


def _is_text_like(path: Path, mime_type: str) -> bool:
    if mime_type.startswith("text/"):
        return True
    return path.suffix.lower() in {".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".log", ".py", ".js", ".ts", ".html"}


@dataclass
class CodeExecJob:
    id: str
    user_id: str
    language: str
    code: str
    timeout_sec: int
    runtime: str = "local"
    status: str = "queued"
    created_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    completed_at: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)


class CodeExecService:
    def __init__(self):
        self._jobs: dict[str, CodeExecJob] = {}
        self._lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def create_job(self, *, user_id: str, language: str, code: str, timeout_sec: int) -> CodeExecJob:
        lang = str(language or "").strip().lower()
        if lang not in {"python"}:
            raise ValueError("unsupported_language")
        payload = str(code or "")
        if not payload.strip():
            raise ValueError("empty_code")
        if len(payload) > _max_code_chars():
            raise ValueError("code_too_large")

        timeout = max(1, min(int(timeout_sec or 10), int(os.getenv("OPENVEGAS_CODE_EXEC_MAX_TIMEOUT_SEC", "30"))))
        job = CodeExecJob(
            id=str(uuid.uuid4()),
            user_id=str(user_id or ""),
            language=lang,
            code=payload,
            timeout_sec=timeout,
            runtime=_runtime_kind(),
        )
        async with self._lock:
            self._jobs[job.id] = job
            self._tasks[job.id] = asyncio.create_task(self._run_job(job.id))
        return job

    async def get_job(self, *, user_id: str, job_id: str) -> CodeExecJob | None:
        async with self._lock:
            job = self._jobs.get(str(job_id or ""))
            if not job or job.user_id != str(user_id or ""):
                return None
            return job

    async def _run_job(self, job_id: str) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = "running"
            job.started_at = _utc_now()
            language = job.language
            code = job.code
            timeout_sec = job.timeout_sec
            runtime = job.runtime

        if language != "python":
            async with self._lock:
                job = self._jobs.get(job_id)
                if job:
                    job.status = "failed"
                    job.error = "unsupported_language"
                    job.completed_at = _utc_now()
            return

        net_disabled_prefix = ""
        if not _allow_network():
            # Defense-in-depth for local runtime mode.
            net_disabled_prefix = (
                "import builtins as __b\n"
                "__blocked={'socket','requests','httpx','aiohttp','ftplib','urllib3'}\n"
                "__orig=__b.__import__\n"
                "def __guard(name,*a,**k):\n"
                "  base=str(name).split('.',1)[0]\n"
                "  if base in __blocked:\n"
                "    raise RuntimeError('network_access_disabled')\n"
                "  return __orig(name,*a,**k)\n"
                "__b.__import__=__guard\n"
            )

        full_code = f"{net_disabled_prefix}\n{code}"
        stdout = ""
        stderr = ""
        exit_code: int | None = None
        error: str | None = None
        artifacts: list[dict[str, Any]] = []

        tmpdir = Path(tempfile.mkdtemp(prefix="ov_code_exec_"))
        try:
            script_path = tmpdir / "main.py"
            script_path.write_text(full_code, encoding="utf-8")

            if runtime == "docker":
                stdout, stderr, exit_code, error = await self._run_python_docker(tmpdir=tmpdir, timeout_sec=timeout_sec)
            else:
                stdout, stderr, exit_code, error = await self._run_python_local(tmpdir=tmpdir, timeout_sec=timeout_sec)

            artifacts = self._collect_artifacts(tmpdir=tmpdir, skip_names={"main.py"})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        max_chars = _max_output_chars()
        stdout = stdout[:max_chars]
        stderr = stderr[:max_chars]

        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.exit_code = exit_code
            job.stdout = stdout
            job.stderr = stderr
            job.error = error
            job.artifacts = artifacts
            job.status = "succeeded" if error is None and int(exit_code or 0) == 0 else "failed"
            job.completed_at = _utc_now()
            self._tasks.pop(job_id, None)

    async def _run_python_local(self, *, tmpdir: Path, timeout_sec: int) -> tuple[str, str, int | None, str | None]:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3",
                "-I",
                str(tmpdir / "main.py"),
                cwd=str(tmpdir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    "PYTHONNOUSERSITE": "1",
                    "NO_PROXY": "*",
                    "HTTP_PROXY": "",
                    "HTTPS_PROXY": "",
                },
            )
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=float(timeout_sec))
            return (
                (out_b or b"").decode("utf-8", errors="ignore"),
                (err_b or b"").decode("utf-8", errors="ignore"),
                int(proc.returncode or 0),
                None,
            )
        except asyncio.TimeoutError:
            if proc and proc.returncode is None:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass
            return "", "", None, "timeout"
        except Exception as exc:  # pragma: no cover - defensive
            return "", "", None, str(exc) or "execution_error"

    async def _run_python_docker(self, *, tmpdir: Path, timeout_sec: int) -> tuple[str, str, int | None, str | None]:
        docker_bin = shutil.which("docker")
        if not docker_bin:
            return "", "", None, "docker_not_available"

        cmd = [
            docker_bin,
            "run",
            "--rm",
            "--workdir",
            "/workspace",
            "--memory",
            f"{_container_memory_mb()}m",
            "--cpus",
            f"{_container_cpus()}",
            "-v",
            f"{str(tmpdir)}:/workspace",
        ]
        if not _allow_network():
            cmd.extend(["--network", "none"])
        cmd.extend([_container_image(), "python", "-I", "/workspace/main.py"])

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=float(timeout_sec))
            return (
                (out_b or b"").decode("utf-8", errors="ignore"),
                (err_b or b"").decode("utf-8", errors="ignore"),
                int(proc.returncode or 0),
                None,
            )
        except asyncio.TimeoutError:
            if proc and proc.returncode is None:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass
            return "", "", None, "timeout"
        except Exception as exc:  # pragma: no cover - defensive
            return "", "", None, str(exc) or "docker_execution_error"

    def _collect_artifacts(self, *, tmpdir: Path, skip_names: set[str]) -> list[dict[str, Any]]:
        max_artifacts = _max_artifacts()
        if max_artifacts <= 0:
            return []
        max_bytes = _artifact_max_bytes()
        inline_max = _artifact_inline_max_bytes()
        preview_chars = _artifact_text_preview_chars()

        rows: list[dict[str, Any]] = []
        for p in sorted(tmpdir.rglob("*"), key=lambda x: str(x).lower()):
            if len(rows) >= max_artifacts:
                break
            if not p.is_file():
                continue
            if p.name in skip_names:
                continue
            try:
                size = int(p.stat().st_size)
            except OSError:
                continue
            rel_path = str(p.relative_to(tmpdir)).replace(os.sep, "/")
            mime_type = str(mimetypes.guess_type(str(p))[0] or "application/octet-stream")
            item: dict[str, Any] = {
                "path": rel_path,
                "size_bytes": size,
                "mime_type": mime_type,
                "content_truncated": False,
            }

            if size > max_bytes:
                item["content_truncated"] = True
                rows.append(item)
                continue

            try:
                raw = p.read_bytes()
            except OSError:
                rows.append(item)
                continue

            if len(raw) <= inline_max:
                item["content_base64"] = base64.b64encode(raw).decode("ascii")
            else:
                item["content_truncated"] = True

            if _is_text_like(p, mime_type):
                text = raw.decode("utf-8", errors="ignore")
                item["text_preview"] = text[:preview_chars]

            rows.append(item)
        return rows

    @staticmethod
    def serialize(job: CodeExecJob) -> dict[str, Any]:
        return {
            "job_id": job.id,
            "status": job.status,
            "language": job.language,
            "runtime": job.runtime,
            "timeout_sec": job.timeout_sec,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "exit_code": job.exit_code,
            "error": job.error,
            "stdout": job.stdout,
            "stderr": job.stderr,
            "artifacts": list(job.artifacts or []),
        }
