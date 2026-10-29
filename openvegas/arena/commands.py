"""Human PvP and bounded agent controls. No provider or wallet access."""

import asyncio
import json
import os
from pathlib import Path
import time
import uuid

import click

from .__main__ import call, local_url, play, private_read, private_write

AGENT_ENV = "OPENVEGAS_ARENA_AGENT"


def request(identity, operation="observe", **fields):
    if operation not in {"observe", "find", "act"}:
        raise ValueError("unknown_agent_operation")
    if operation == "observe" and fields:
        raise ValueError("observe_has_no_fields")
    path = "/agent" if operation == "observe" else "/agent/" + operation
    return call(
        identity["url"], path, identity["token"], None if operation == "observe" else fields
    )


async def tool_request(operation="observe", **fields):
    identity = private_read(Path(os.environ[AGENT_ENV]))
    return await asyncio.to_thread(request, identity, operation, **fields)


def run_policy(identity, *, sleep=time.sleep):
    """Free check/call baseline; real host agents use find/observe/act with their own policy."""
    completed = set()
    view = request(identity)
    while not view.get("blocked"):
        observation = view.get("observation", {})
        if (
            not observation
            or observation.get("resolved")
            or observation.get("status") == "cancelled"
        ):
            if observation.get("resolved"):
                completed.add(observation["match_id"])
            view = request(identity, "find", request_id=uuid.uuid4().hex)
            continue
        options = observation.get("legal_actions", {})
        if options:
            result = request(
                identity,
                "act",
                match_id=observation["match_id"],
                revision=view["grant"]["revision"],
                version=observation["version"],
                action="check" if "check" in options else "call",
                request_id=uuid.uuid4().hex,
            )
            if not result["accepted"]:
                # Refresh only; never repeat the uncertain move or increase limits.
                sleep(0.5)
        else:
            sleep(0.5)
        view = request(identity)
    return {
        "completed_matches": sorted(completed),
        "stopped": view["blocked"],
        "unit": "test_chips",
        "provider_calls": 0,
    }


@click.command()
@click.option(
    "--identity",
    type=click.Path(exists=True, path_type=Path),
    envvar=["OPENVEGAS_PVP_IDENTITY", "OPENVEGAS_ARENA_IDENTITY"],
    required=True,
)
def pvp(identity):
    """Human-only multiplayer with test chips; use /openvegas pvp inside Claude."""
    seat = private_read(identity)
    if seat.get("pool") != "human":
        raise click.ClickException("PvP requires a human seat, not an agent grant.")
    play(seat)


@click.group(invoke_without_command=True)
@click.option("--identity", type=click.Path(exists=True, path_type=Path), envvar=AGENT_ENV)
@click.pass_context
def arena(ctx, identity):
    """Bounded local agent competition. Test chips, never $V or paid inference."""
    ctx.obj = identity
    if ctx.invoked_subcommand is None:
        ctx.invoke(observe)


def identity_from(ctx):
    if not ctx.obj:
        raise click.ClickException(
            "Configure OPENVEGAS_ARENA_AGENT or --identity with an operator-issued private agent file."
        )
    return private_read(ctx.obj)


def output(data):
    click.echo(json.dumps(data, indent=2))


@arena.command("connect")
@click.option("--output", "destination", type=click.Path(path_type=Path), required=True)
@click.pass_context
def connect(ctx, destination):
    """Prepare private client configs; does not install plugins or change providers."""
    import sys
    import secrets
    from .client import ArenaClient
    identity = identity_from(ctx)
    view = ArenaClient(identity).get_context()
    if view.get("blocked"):
        raise click.ClickException("Grant is blocked; no client configuration created.")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    root = str(Path(__file__).resolve().parents[2])
    bootstrap = "import sys; sys.path.insert(0, " + repr(root) + "); from openvegas.arena.mcp import main; main()"
    spec = {"command": sys.executable, "args": ["-I", "-c", bootstrap, "--identity", str(ctx.obj.resolve())]}
    private_write(destination / "claude.json", {"mcpServers": {"openvegas-arena": spec}})
    private_write(destination / "bridge-access.json", {"token": secrets.token_urlsafe(32)})
    toml = '[mcp_servers.openvegas-arena]\ncommand = ' + json.dumps(sys.executable) + '\nargs = ' + json.dumps(spec["args"]) + '\n'
    path = destination / "codex.toml"
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as f:
        f.write(toml)
    click.echo("Private Claude/Codex connection configs prepared. Nothing installed; provider/billing unchanged. See docs/EVAL_LAB.md for review and HTTP testing.")


@arena.command()
@click.pass_context
def observe(ctx):
    """Get current player-visible state and remaining grant limits."""
    output(request(identity_from(ctx)))


@arena.command()
@click.option("--request-id", default=lambda: uuid.uuid4().hex)
@click.pass_context
def find(ctx, request_id):
    """Find/join a compatible table once. Reuses any unfinished hand."""
    output(request(identity_from(ctx), "find", request_id=request_id))


@arena.command()
@click.option("--match-id", required=True)
@click.option("--version", type=int, required=True)
@click.option("--revision", type=int, required=True)
@click.option("--action", type=click.Choice(["check", "call", "fold", "raise"]), required=True)
@click.option("--amount", type=int)
@click.option("--request-id", default=lambda: uuid.uuid4().hex)
@click.pass_context
def act(ctx, **fields):
    """Submit one versioned agent move within the existing grant."""
    output(request(identity_from(ctx), "act", **fields))


@arena.command("run-example")
@click.pass_context
def run_example(ctx):
    """Run a check/call baseline until its grant ends; no model calls."""
    output(run_policy(identity_from(ctx)))


@arena.command("grant")
@click.option("--home", type=click.Path(path_type=Path), default=".local/arena")
@click.option("--url", default="http://127.0.0.1:8793")
@click.option("--agent-file", type=click.Path(path_type=Path), required=True)
@click.option("--controller-file", type=click.Path(path_type=Path), required=True)
@click.option("--seats", type=int, default=2)
@click.option("--chips", type=int, default=200)
@click.option("--max-matches", type=int, default=10)
@click.option("--entry-budget", type=int, default=2000)
@click.option("--lifetime-seconds", type=int, default=3600)
@click.option("--model-label", default="unspecified")
def grant(home, url, agent_file, controller_file, model_label, **limits):
    """Operator authorizes bounded test-chip play. Secrets saved, never printed."""
    if (
        agent_file.resolve() == controller_file.resolve()
        or agent_file.exists()
        or controller_file.exists()
    ):
        raise click.ClickException("Choose two distinct new private file paths.")
    result = call(
        url,
        "/grants",
        private_read(home / "operator.json")["token"],
        {**limits, "config": {"model": model_label}},
    )
    common = {"url": local_url(url), "grant_id": result["grant_id"]}
    private_write(agent_file, {**common, "token": result["agent_token"]})
    private_write(controller_file, {**common, "token": result["controller_token"]})
    output(result["limits"])


@arena.command("control")
@click.argument("command", type=click.Choice(["status", "pause", "resume", "stop", "override"]))
@click.option("--controller", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--revision", type=int)
@click.option("--version", type=int)
@click.option("--action", type=click.Choice(["check", "call", "fold", "raise"]))
@click.option("--amount", type=int)
@click.option("--request-id", default=lambda: uuid.uuid4().hex)
def control(command, controller, **fields):
    """Human controller only. Pausing blocks agent moves, not the turn clock."""
    identity = private_read(controller)
    if command != "status" and fields["revision"] is None:
        raise click.ClickException("Read control status, then supply its --revision.")
    output(
        call(
            identity["url"],
            "/controller",
            identity["token"],
            None if command == "status" else {"command": command, **fields},
        )
    )


@arena.command("rights")
@click.option("--controller", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--match-id", required=True)
@click.option(
    "--purpose",
    type=click.Choice(["none", "internal_evaluation", "training", "evaluation_delivery"]),
    required=True,
)
@click.option("--record-id")
def rights(controller, match_id, purpose, record_id):
    """Separate per-hand export consent. Never grants commercial resale rights."""
    identity = private_read(controller)
    output(
        call(
            identity["url"],
            "/controller/rights/" + match_id,
            identity["token"],
            {"purpose": None if purpose == "none" else purpose, "record_id": record_id},
        )
    )
