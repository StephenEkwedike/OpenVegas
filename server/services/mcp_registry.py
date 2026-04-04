"""In-memory MCP server registry and health probes."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import shlex
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

import httpx


Transport = Literal["stdio", "streamable-http", "websocket"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _allowlist_patterns() -> list[str]:
    raw = str(os.getenv("OPENVEGAS_MCP_ALLOWLIST", "")).strip()
    if not raw:
        return []
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _target_identity(transport: str, target: str) -> str:
    transport_name = str(transport or "").strip().lower()
    raw_target = str(target or "").strip()
    if transport_name in {"streamable-http", "websocket"}:
        try:
            parsed = urlparse(raw_target)
            return str(parsed.netloc or raw_target).lower()
        except Exception:
            return raw_target.lower()
    return raw_target.split(" ", 1)[0].strip().lower()


def _target_allowed(transport: str, target: str) -> bool:
    patterns = _allowlist_patterns()
    if not patterns:
        return True
    identity = _target_identity(transport, target)
    return any(fnmatch.fnmatch(identity, pattern) for pattern in patterns)


@dataclass
class MCPServerRecord:
    id: str
    user_id: str
    name: str
    transport: Transport
    target: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)


class MCPRegistryService:
    def __init__(self):
        self._servers: dict[str, MCPServerRecord] = {}
        self._lock = asyncio.Lock()

    async def register_server(
        self,
        *,
        user_id: str,
        name: str,
        transport: str,
        target: str,
        metadata: dict[str, Any] | None = None,
    ) -> MCPServerRecord:
        tr = str(transport or "").strip().lower()
        if tr not in {"stdio", "streamable-http", "websocket"}:
            raise ValueError("unsupported_transport")
        if not _target_allowed(tr, target):
            raise PermissionError("mcp_target_not_allowlisted")
        async with self._lock:
            server_id = str(uuid.uuid4())
            rec = MCPServerRecord(
                id=server_id,
                user_id=str(user_id or ""),
                name=str(name or "mcp-server"),
                transport=tr,  # type: ignore[assignment]
                target=str(target or "").strip(),
                metadata=dict(metadata or {}),
            )
            self._servers[server_id] = rec
            return rec

    async def list_servers(self, *, user_id: str) -> list[dict[str, Any]]:
        owner = str(user_id or "")
        async with self._lock:
            out = []
            for rec in self._servers.values():
                if rec.user_id != owner:
                    continue
                out.append(
                    {
                        "id": rec.id,
                        "name": rec.name,
                        "transport": rec.transport,
                        "target": rec.target,
                        "metadata": dict(rec.metadata or {}),
                        "created_at": rec.created_at,
                        "updated_at": rec.updated_at,
                    }
                )
            return out

    async def get_server(self, *, user_id: str, server_id: str) -> MCPServerRecord | None:
        owner = str(user_id or "")
        sid = str(server_id or "")
        async with self._lock:
            rec = self._servers.get(sid)
            if not rec or rec.user_id != owner:
                return None
            return rec

    async def health(self, *, user_id: str, server_id: str) -> dict[str, Any]:
        rec = await self.get_server(user_id=user_id, server_id=server_id)
        if not rec:
            raise KeyError("not_found")
        if rec.transport == "stdio":
            cmd = str(rec.target or "").split(" ", 1)[0].strip()
            ok = bool(cmd and shutil.which(cmd))
            return {
                "server_id": rec.id,
                "transport": rec.transport,
                "status": "ok" if ok else "error",
                "detail": "binary_found" if ok else "binary_not_found",
            }
        parsed = urlparse(str(rec.target or ""))
        scheme = str(parsed.scheme or "").lower()
        if rec.transport == "streamable-http":
            ok = scheme in {"http", "https"}
            return {
                "server_id": rec.id,
                "transport": rec.transport,
                "status": "ok" if ok else "error",
                "detail": "url_valid" if ok else "invalid_url_scheme",
            }
        ok = scheme in {"ws", "wss"}
        return {
            "server_id": rec.id,
            "transport": rec.transport,
            "status": "ok" if ok else "error",
            "detail": "url_valid" if ok else "invalid_ws_scheme",
        }

    async def call_tool(
        self,
        *,
        user_id: str,
        server_id: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        timeout_sec: int = 20,
    ) -> dict[str, Any]:
        rec = await self.get_server(user_id=user_id, server_id=server_id)
        if not rec:
            raise KeyError("not_found")
        tool_name = str(tool or "").strip()
        if not tool_name:
            raise ValueError("tool_required")
        args = dict(arguments or {})
        timeout = max(1, min(120, int(timeout_sec or 20)))

        if rec.transport == "streamable-http":
            url = str(rec.target or "").rstrip("/") + "/tools/call"
            async with httpx.AsyncClient(timeout=float(timeout)) as client:
                resp = await client.post(url, json={"tool": tool_name, "arguments": args})
            if resp.status_code >= 400:
                raise RuntimeError(f"http_error:{resp.status_code}")
            data = resp.json() if resp.content else {}
            return {"server_id": rec.id, "tool": tool_name, "transport": rec.transport, "result": data}

        if rec.transport == "websocket":
            import websockets

            async with websockets.connect(str(rec.target or ""), open_timeout=float(timeout), close_timeout=2.0) as ws:
                await ws.send(json.dumps({"type": "tool.call", "tool": tool_name, "arguments": args}, separators=(",", ":")))
                raw = await asyncio.wait_for(ws.recv(), timeout=float(timeout))
            parsed: Any
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            try:
                parsed = json.loads(str(raw or ""))
            except Exception:
                parsed = {"text": str(raw or "")}
            return {"server_id": rec.id, "tool": tool_name, "transport": rec.transport, "result": parsed}

        # stdio transport:
        # Convention: command receives "<tool>" "<arguments-json>" and outputs JSON on stdout.
        target = str(rec.target or "").strip()
        if not target:
            raise RuntimeError("missing_stdio_target")
        cmd_parts = shlex.split(target)
        proc = await asyncio.to_thread(
            subprocess.run,
            [*cmd_parts, tool_name, json.dumps(args, separators=(",", ":"), ensure_ascii=False)],
            capture_output=True,
            text=True,
            timeout=float(timeout),
            check=False,
        )
        stdout = str(proc.stdout or "").strip()
        stderr = str(proc.stderr or "").strip()
        parsed: Any
        if stdout:
            try:
                parsed = json.loads(stdout)
            except Exception:
                parsed = {"text": stdout}
        else:
            parsed = {}
        if int(proc.returncode) != 0:
            raise RuntimeError(f"stdio_exit_{proc.returncode}:{stderr[:200]}")
        return {
            "server_id": rec.id,
            "tool": tool_name,
            "transport": rec.transport,
            "result": parsed,
            "stderr": stderr,
        }
