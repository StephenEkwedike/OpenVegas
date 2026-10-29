"""Official MCP transport exposing existing wallet/store services, not agent tools."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent

from .broker import Broker
from .controls import action_catalog, lobby_navigation
from .cosmetics import presentation_status, snapshot
from .credentials import IntegrationError, backend_url
from .game_result import receipt


def make_server(host: str, broker: Broker | None = None):
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    broker = broker or Broker()

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield {}
        finally:
            await broker.close()

    server = MCPServer("openvegas", version="0.1.0", lifespan=lifespan,
                       log_level="WARNING", instructions=(
        "OpenVegas supplements the native host; it does not replace its agent loop. "
        "Never read credentials or configure provider keys. Native subscription billing is unchanged. "
        "Ask before changing cosmetics. No monetary rewards are issued from local events."))
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    if os.getenv("OPENVEGAS_PVP_IDENTITY") or os.getenv("OPENVEGAS_ARENA_IDENTITY"):
        @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                                openWorldHint=False))
        async def pvp() -> str:
            """Open the explicitly configured local human PvP in this same terminal.

            Non-redeemable test chips only. Human controls every move. No $V,
            inference calls, export grants or automatic play. Never read seat files.
            """
            from openvegas.arena.native import open_native
            try:
                return await open_native()
            except Exception:
                return "Arena display/service unavailable. No automatic retry, $V or inference charge. Test-chip deadlines still apply."

    if os.getenv("OPENVEGAS_ARENA_AGENT"):
        from openvegas.arena.client import ArenaClient
        from openvegas.arena.mcp import make_server as register_arena_tools
        try:
            arena_client = ArenaClient.from_file(os.environ["OPENVEGAS_ARENA_AGENT"])
        except (OSError, ValueError, KeyError, TypeError):
            arena_client = None
        if arena_client is not None:
            register_arena_tools(arena_client, server)

        @server.tool(structured_output=True, annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False))
        async def arena(operation: Literal["observe", "find", "act"] = "observe",
                        match_id: str | None = None, revision: int | None = None,
                        version: int | None = None,
                        action: Literal["fold", "check", "call", "raise"] | None = None,
                        amount: int | None = None, request_id: str | None = None) -> dict[str, Any]:
            """Agent-only local test-chip competition under an operator-issued bounded grant.

            Observe limits, find a compatible match, then choose legal moves and
            act with the observed match_id/version/grant revision. No human form
            or per-move confirmation required. Honor paused/stopped/expired and
            budgets. Never read credential files, call real-wager play, change
            billing, extend grants or grant data rights. Poll at most twice per
            second while waiting. Host inference billing is unchanged.
            Use the same request_id for a retry; after uncertain errors observe,
            never silently start another hand. Human interventions are recorded.
            """
            from openvegas.arena.commands import tool_request
            fields = {k: v for k, v in locals().items() if k in {
                "match_id", "revision", "version", "action", "amount", "request_id"
            } and v is not None}
            if operation in {"find", "act"}:
                fields.setdefault("request_id", uuid.uuid4().hex)
            try:
                return await tool_request(operation, **fields)
            except Exception:
                return {"available": False, "message": "Arena unavailable or request rejected. Observe before another action; do not retry or extend the grant automatically. No $V or provider request was made by this tool."}

    async def safely(call):
        try:
            return await call
        except IntegrationError as exc:
            return {"available": False, "message": str(exc)}
        except Exception:  # noqa: BLE001 - don't expose transport/auth internals to the model.
            return {"available": False, "message": "OpenVegas unavailable; native coding is unaffected."}

    @server.tool(annotations=readonly, structured_output=True)
    async def get_wallet() -> dict[str, Any]:
        """Read the signed-in OpenVegas balance. Never reads another user's wallet."""
        state = await safely(broker.state(host))
        return {k: state[k] for k in ("balance", "billing_mode", "spending", "notice", "available", "message") if k in state}

    @server.tool(annotations=readonly, structured_output=True)
    async def get_game_state() -> dict[str, Any]:
        """Show the OpenVegas HUD/lobby: owned cosmetics, status and game links; never wagers or spends."""
        from .terminal_handoff import check
        return {**await safely(broker.state(host)), "presentation": presentation_status(host),
                "game_connection": await safely(check()),
                "controls": action_catalog(),
                "lobby": lobby_navigation(host, getattr(broker, "backend", None) or backend_url())}

    @server.tool(structured_output=True, annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    async def play(ctx: Context, game: Literal["blackjack", "roulette", "poker", "baccarat", "slots", "horse"] | None = None,
                   text_only: bool = False) -> Annotated[CallToolResult, dict[str, Any]]:
        """Open the human-controlled game table in this terminal, then return to coding.

        Call only when the user explicitly asks to play. Never answer forms for
        them. The human submits an amount to place one wager and can lose it all. No
        auto-play, top-ups or native subscription refill. Sign in/up and Purchase
        $V open the browser from the lobby. A full CLI login or separately approved
        human-game login is required; cosmetic-only grants cannot place wagers.
        Optional game selects the requested table, never a wager or move. Quit
        ends a round without refund; the next play starts a fresh game.
        Set text_only only if the user explicitly requests static text forms.
        Never select it to work around missing terminal setup or a failed table.
        """
        from .native_play import play_in_host
        from . import terminal_handoff
        def reply(data):
            return CallToolResult(content=[TextContent(type="text", text=receipt(data))], structured_content=data)
        if text_only:
            return reply(await safely(play_in_host(ctx, broker, requested_game=game)))
        if terminal_handoff.configured():
            return reply(await safely(terminal_handoff.play(ctx, broker, requested_game=game)))
        if host == 'codex':
            from . import game_view
            try:
                config = game_view.settings()
            except IntegrationError as exc:
                return reply({"available": False, "message": str(exc)})
            if config:
                return reply(await safely(game_view.play(ctx, broker, config, requested_game=game)))
        return reply(terminal_handoff.setup_required())

    @server.tool(annotations=readonly, structured_output=True)
    async def redeem_info() -> dict[str, Any]:
        """Explain billing without changing the host's provider or spending credits."""
        state = await safely(broker.state(host))
        return {"billing_mode": state.get("billing_mode", "unavailable"),
                "gateway_available": state.get("gateway_available", False),
                "message": "$V cannot refill Claude or ChatGPT subscription quota. "
                           "Use " + ('/openvegas' if host == 'claude' else '$openvegas') +
                           " use v to review a direct API quote and session ceiling in this terminal; use host switches back. "
                           "The pre-model host command requires explicit consent and a configured backend. "
                           "Never execute a billing switch through a model's shell tool or change billing automatically."}

    @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=True, openWorldHint=False))
    async def select_cosmetic(slot: Literal["companion", "completion"],
                              item_id: str | None = None, confirmed: bool = False) -> dict[str, Any]:
        """Equip an already-owned item (null clears it). Requires explicit user confirmation; never purchases."""
        if not confirmed:
            return {"confirmation_required": True, "message": "Ask the user before changing their equipped cosmetic."}
        if item_id is not None and (not item_id or len(item_id) > 128):
            return {"available": False, "message": "Invalid cosmetic identifier."}
        return await safely(broker.equip(host, slot, item_id))

    return server


async def direct(host, action):
    if action == "review-q50":
        if host != "claude":
            return {"available": False, "message": "Use Codex's native pet installer for this review."}
        from .local_review import q50_snapshot
        try:
            return q50_snapshot()
        except Exception:
            return {"available": False, "message": "Local Q50 motion artwork is unavailable."}
    broker = Broker()
    try:
        if action == "state":
            return await broker.state(host)
        if action == "cosmetics":
            return await snapshot(broker, host)
        if action in {"start", "waiting", "stopped", "failed", "cancelled", "session_end"}:
            return await broker.request("POST", "/events", token=await broker.host_token(host),
                                        data={"event_id": uuid.uuid4().hex, "phase": action})
        raise IntegrationError("Unsupported integration action.")
    except IntegrationError as exc:
        return {"available": False, "message": str(exc)}
    except Exception:  # noqa: BLE001 - a cosmetic helper must not break the native host.
        return {"available": False, "message": "OpenVegas unavailable; native coding is unaffected."}
    finally:
        await broker.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, choices=("claude", "codex"))
    parser.add_argument("--backend")
    parser.add_argument("--profile")
    parser.add_argument("--action", choices=("state", "cosmetics", "review-q50", "start", "waiting", "stopped", "failed", "cancelled", "session_end", "use-v", "use-host"))
    parser.add_argument('--billing-hook', action='store_true')
    parser.add_argument('--supervise', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.backend:
        os.environ["OPENVEGAS_BACKEND_URL"] = args.backend
    if args.profile:
        os.environ["OPENVEGAS_INTEGRATION_PROFILE"] = args.profile
    if args.supervise is not None:
        from .billing_host import main as supervise
        raise SystemExit(supervise(args.host, args.supervise))
    if args.billing_hook:
        from .use import hook_main
        hook_main(args.host)
        return
    if args.action in ('use-v', 'use-host'):
        from .use import request
        from .credentials import IntegrationError
        try:
            result = {'text': asyncio.run(request(args.host, args.action[4:]))}
        except IntegrationError as exc:
            result = {'text': str(exc)}
        print(json.dumps(result))
        return
    from .credentials import profile
    from openvegas.arena.host_setup import load_host
    try:
        load_host(args.host, profile())
    except (OSError, ValueError, KeyError, TypeError):
        # Invalid pilot settings must not affect normal coding or wallet tools.
        for key in ("OPENVEGAS_ARENA_AGENT", "OPENVEGAS_PVP_IDENTITY", "OPENVEGAS_ARENA_IDENTITY"):
            os.environ.pop(key, None)
    # Avoid recording outbound URLs/headers; stdout belongs solely to MCP or the direct JSON result.
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.action:
        print(json.dumps(asyncio.run(direct(args.host, args.action))))
    else:
        make_server(args.host).run(transport="stdio")


if __name__ == "__main__":
    main()
