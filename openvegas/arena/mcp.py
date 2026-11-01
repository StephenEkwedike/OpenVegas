"""Isolated pilot MCP connection. Public OAuth deployment is deliberately absent."""

import argparse
import asyncio
import secrets
from pathlib import Path
from typing import Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .__main__ import private_read
from .client import ArenaClient
from .service import token_valid


def make_server(client, server=None):
    server = server or MCPServer(
        "openvegas-arena",
        instructions=(
            "Non-redeemable test chips only. Keep your current model provider. "
            "Participation is optional: decline, continue_work and stop are valid choices. "
            "Get context, list games and create a decision checkpoint; choose only within "
            "the operator grant. After joining observe and choose legal actions. "
            "Use stable request IDs and refresh after human intervention. Stop on blocked/error. "
            "Never read credentials, change billing, create grants or bypass host permissions. "
            "Task outcomes are opt-in self-reports, not proof of coding success. "
            "Poll no more than twice per second. MCP is not a background agent runtime."
        ),
        log_level="WARNING",
    )
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    async def invoke(method, **kwargs):
        try:
            return await asyncio.to_thread(getattr(client, method), **kwargs)
        except Exception:  # noqa: BLE001 - errors must not leak private capability contents.
            return {
                "available": False,
                "error": "request_rejected_or_unavailable",
                "message": "Observe before any retry; no automatic replacement wager or provider change.",
            }

    @server.tool(annotations=read)
    async def get_context() -> dict:
        """Read current grant limits and own active state, without joining."""
        return await invoke("get_context")

    @server.tool(annotations=read)
    async def list_games() -> dict:
        """Rules, commitment, fees, clocks and known/unknown returns; no entry."""
        return await invoke("list_games")

    @server.tool(annotations=write)
    async def checkpoint(objective_id: str = "unspecified", task_budget: int | None = None) -> dict:
        """Record an opt-in decision context. IDs only, never code/prompts/secrets."""
        return await invoke("checkpoint", objective_id=objective_id, task_budget=task_budget)

    @server.tool(annotations=write)
    async def choose(
        checkpoint_id: str, context_hash: str, choice: Literal["decline", "continue_work", "stop"]
    ) -> dict:
        """Record a no-play choice. Never call join_game after choosing not to play."""
        return await invoke(
            "choose", checkpoint_id=checkpoint_id, context_hash=context_hash, choice=choice
        )

    @server.tool(annotations=write)
    async def join_game(
        checkpoint_id: str, context_hash: str, game_id: Literal["holdem"] = "holdem"
    ) -> dict:
        """Choose entry using the recorded opportunity and existing bounded grant."""
        return await invoke(
            "join_game", checkpoint_id=checkpoint_id, context_hash=context_hash, game_id=game_id
        )

    @server.tool(annotations=read)
    async def observe() -> dict:
        """Read only this agent's visible game state, including legal actions."""
        return await invoke("observe")

    @server.tool(annotations=write)
    async def act(
        match_id: str,
        version: int,
        revision: int,
        request_id: str,
        action: Literal["check", "call", "fold", "raise"],
        amount: int | None = None,
    ) -> dict:
        """One versioned move; never repeat with a new ID after an uncertain result."""
        return await invoke(
            "act",
            match_id=match_id,
            version=version,
            revision=revision,
            request_id=request_id,
            action=action,
            amount=amount,
        )

    @server.tool(annotations=write)
    async def leave_game(match_id: str, version: int, revision: int, request_id: str) -> dict:
        """Fold only when legal. No refund, top-up or off-turn automatic move."""
        return await invoke(
            "leave_game",
            match_id=match_id,
            version=version,
            revision=revision,
            request_id=request_id,
        )

    @server.tool(annotations=write)
    async def task_outcome(
        checkpoint_id: str,
        status: Literal["succeeded", "failed", "abandoned", "unknown"],
        evidence_id: str | None = None,
    ) -> dict:
        """Optional unverified task outcome. Never infer it from a poker win."""
        return await invoke(
            "task_outcome", checkpoint_id=checkpoint_id, status=status, evidence_id=evidence_id
        )

    return server


def http_app(server, access_token):
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    token_valid(access_token)
    app = server.streamable_http_app(
        stateless_http=True, json_response=True, max_request_body_size=32768, host="127.0.0.1"
    )

    class LocalGrantAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if (
                not request.client
                or request.client.host not in {"127.0.0.1", "::1"}
                or request.headers.get("origin")
                or request.url.hostname != "127.0.0.1"
            ):
                return JSONResponse({"error": "loopback_only"}, status_code=403)
            auth = request.headers.get("authorization", "")
            if not secrets.compare_digest(auth, "Bearer " + access_token):
                return JSONResponse(
                    {"error": "bridge_capability_required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return response

    app.add_middleware(LocalGrantAuth)
    return app


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--identity", type=Path, required=True)
    p.add_argument("--http", action="store_true")
    p.add_argument("--access-file", type=Path)
    p.add_argument("--port", type=int, default=8794)
    args = p.parse_args()
    server = make_server(ArenaClient.from_file(args.identity))
    if args.http:
        if not args.access_file:
            p.error("HTTP requires a separate private bridge access file")
        import uvicorn

        uvicorn.run(
            http_app(server, private_read(args.access_file)["token"]),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
        )
    else:
        server.run()


if __name__ == "__main__":
    main()
