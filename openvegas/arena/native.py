"""Opt-in human controls over the existing same-terminal relay, never $V tools."""

import asyncio
import json
import os
from pathlib import Path
import uuid

from pydantic import BaseModel
from rich.console import Group
from rich.text import Text

from .__main__ import call, private_read

IDENTITY_ENV = "OPENVEGAS_PVP_IDENTITY"


class ArenaCommand(BaseModel):
    command: str


def table(view, width):
    from openvegas.tui.game_screen import card_faces

    if view.get("status") == "waiting":
        return Text(
            f"Waiting for players: {view['joined']}/{view['capacity']}\nTest chips only.",
            style="cyan",
        )
    rows = [
        Text(f"POKER ARENA / {view['street'].upper()} / pot {view['pot']} chips", style="bold cyan")
    ]
    for seat, chips in enumerate(view["stacks"]):
        label = "you" if seat == view["seat"] else "opponent"
        state = "folded" if view["folded"][seat] else "acting" if seat == view["actor"] else ""
        rows.append(Text(f"Seat {seat + 1} ({label}): {chips} chips {state}"))
    rows.extend(
        [
            Text("Your cards"),
            card_faces(view["cards"], width),
            Text("Shared cards"),
            card_faces(view["board"], width),
        ]
    )
    if view.get("resolved"):
        for seat, cards in view.get("showdown", {}).items():
            rows.extend([Text(f"Seat {int(seat) + 1} shows"), card_faces(cards, width)])
        rows.append(Text(f"Hand finished. Payouts: {view['payouts']} test chips.", style="bold"))
    return Group(*rows)


def instructions(view):
    if view.get("resolved"):
        return "Hand finished. Enter done or press Esc to return to coding. No $V changed."
    options = view.get("legal_actions", {})
    if not options:
        return "Waiting for players / the next turn. The table updates automatically. Esc exits; the turn clock continues."
    moves = ", ".join(a for a in options if a != "raise")
    if "raise" in options:
        moves += f"; raise N ({options['raise']['min_to']}-{options['raise']['max_to']} total chips this stage)"
    return f"Your move: {moves}. Enter submits. Esc / quit exits; timeout folds. No $V is at stake."


async def session(view, identity, request=None):
    """Polling only observes; every move requires a human input from the relay."""

    async def http(path, body=None):
        return await asyncio.to_thread(call, identity["url"], path, identity["token"], body)

    request = request or http
    path = "/matches/" + identity["match_id"]
    current = await request(path)
    if current["pool"] != "human":
        return "This human-controlled screen requires a human Arena pool. No move submitted."

    async def publish():
        await view.send(kind="arena_table", observation=current)

    await publish()

    async def poll():
        nonlocal current
        while True:
            await asyncio.sleep(0.5)
            updated = await request(path)
            # A slower in-flight read must never roll the displayed version back.
            if updated["version"] >= current["version"] and updated != current:
                current = updated
                await publish()

    polling = asyncio.create_task(poll())
    try:
        while True:
            answering = asyncio.create_task(view.ask(instructions(current), ArenaCommand))
            try:
                done, _ = await asyncio.wait(
                    [answering, polling], return_when=asyncio.FIRST_COMPLETED
                )
                if polling in done:
                    await polling  # Surface disconnects instead of leaving a dead input form.
                answer = await answering
            finally:
                if not answering.done():
                    answering.cancel()
                await asyncio.gather(answering, return_exceptions=True)
            if answer is None or answer.command.strip().lower() in {"quit", "done", "exit"}:
                return (
                    "Arena closed. No $V or inference billing changed. "
                    "If this hand is unfinished, its test-chip turn clock continues."
                )
            fields = answer.command.strip().lower().split()
            snapshot = current
            options = snapshot.get("legal_actions", {})
            if not fields or fields[0] not in options:
                await view.status("Choose a displayed move on your turn, or quit.")
                continue
            action = fields[0]
            amount = None
            if action == "raise":
                try:
                    if len(fields) != 2:
                        raise ValueError()
                    amount = int(fields[1])
                except ValueError:
                    await view.status("Use raise followed by a whole-chip amount, or quit.")
                    continue
            elif len(fields) != 1:
                await view.status("Enter just the move name, or quit.")
                continue
            result = await request(
                path + "/actions",
                {
                    "version": snapshot["version"],
                    "action": action,
                    "amount": amount,
                    "request_id": uuid.uuid4().hex,
                },
            )
            if result["observation"]["version"] >= current["version"]:
                current = result["observation"]
                await publish()
            if not result["accepted"]:
                await view.status(
                    "Move not accepted: " + result["reason"] + ". Refresh shown; try again or quit."
                )
    finally:
        polling.cancel()
        await asyncio.gather(polling, return_exceptions=True)


async def open_native():
    from openvegas.integrations import terminal_handoff as handoff

    configured_identity = os.getenv(IDENTITY_ENV) or os.getenv("OPENVEGAS_ARENA_IDENTITY")
    if not configured_identity:
        return "Arena pilot is not configured. No game, wager or inference request started."
    if not handoff.configured():
        return handoff.setup_required()["message"]
    # Capability stays inside the helper. Neither the model nor renderer receives it.
    identity = private_read(Path(configured_identity))
    reader, writer = await handoff.connect()
    view = handoff.TerminalView(reader, writer)
    try:
        result = await asyncio.wait_for(session(view, identity), 1800)
    finally:
        try:
            await view.send(kind="done")
            reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
            if reply.get("closed") is not True:
                raise handoff.TerminalDisconnected()
        finally:
            await handoff.close_writer(writer)
    return result
