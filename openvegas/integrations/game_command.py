"""Finite local commands for host shell escapes, with no AI or nested terminal UI."""
from __future__ import annotations

from openvegas.integrations.game_result import amount as format_v

import asyncio
import hashlib

import click

from openvegas.client import OpenVegasClient
from openvegas.emotes.spool import _locked
from .credentials import IntegrationError, backend_url, profile
from .native_play import Journal, run_round, summary

GAMES = {"blackjack", "roulette", "poker", "baccarat", "slots"}
MOVES = {"hit", "stand", "spin", "bet_red", "bet_black", "bet_odd", "bet_even",
         "bet_number", "bet_player", "bet_banker", "bet_tie", "call", "fold",
         "reveal_river", "showdown"}


async def step(client, journal, account, command, stake, confirm, number):
    live = bool(journal.data) and (journal.data.get("pending") or
           journal.data.get("stage") not in {"terminal", "human_casino_resolve", "horse_settled"})
    if live and journal.data.get('game') == 'horse' and command != 'resume':
        raise IntegrationError('An interrupted horse wager needs explicit recovery, not a casino move. '
                               'Use OpenVegas play, or ov game resume --confirm for the original request only.')
    if command in GAMES and live:
        raise IntegrationError("An existing round must be resumed, not replaced. Run ov game status.")
    if command in MOVES | {"resume"} and not live:
        raise IntegrationError("No active round. Run ov game to choose a game.")
    if command in MOVES and journal.data.get("pending"):
        raise IntegrationError("The last response is uncertain. Review ov game status; use ov game resume --confirm to replay only its original key.")
    if command == "bet_number" and number is None:
        raise IntegrationError("Use ov game bet_number --number 0..36. No action sent.")
    used_move = False

    async def ask(message, schema):
        nonlocal used_move
        if schema.__name__ == "ChooseGame":
            return schema(choice=command) if command in GAMES else None
        if schema.__name__ == "Wager":
            if stake is None:
                raise IntegrationError("Provide --stake AMOUNT, then review and repeat with --confirm.")
            return schema(wager=stake)
        if schema.__name__ == "Confirm":
            click.echo(message)
            if command in MOVES or confirm:
                return schema(confirm=True)
            click.echo("No write sent. Repeat the command with --confirm only if you agree.")
            return schema(confirm=False)
        if schema.__name__ == "Number":
            return schema(number=str(number))
        if schema.__name__ == "RoundAction":
            if command in MOVES and not used_move:
                used_move = True
                return schema(choice=command)
            return None  # A finite step yields; it is not the interactive Quit action.
        raise IntegrationError("Unsupported game input; no action sent.")

    return await run_round(client, ask, journal, account, retry_invalid_input=False, continue_existing=True)


@click.command("game")
@click.argument("command", default="status", type=click.Choice(sorted(GAMES | MOVES | {"status", "resume", "quit"})))
@click.option("--stake", help="Exact $V amount for one new round.")
@click.option("--confirm", is_flag=True, help="Approve the displayed one-round risk or interrupted request replay.")
@click.option("--number", type=click.IntRange(0, 36), help="Roulette number; used only with bet_number.")
def game(command, stake, confirm, number):
    """One step, then back to the host. Codex: !ov game. Never uses inference.

    Review: !ov game blackjack --stake 50
    Confirm: !ov game blackjack --stake 50 --confirm
    Move: !ov game stand

    Games can lose your stake. No automatic repeat, top-up or subscription refill.
    """
    async def execute():
        backend = backend_url()
        async with OpenVegasClient() as client:
            client.base_url = backend
            client._http_client.follow_redirects = False
            account = await client._request("GET", "/integrations/account")
            if not account.get("user_id"):
                raise IntegrationError("A full OpenVegas account login is required.")
            identity = hashlib.sha256((backend + "\n" + account["user_id"]).encode()).hexdigest()
            with _locked(profile() / "native-play" / identity) as directory:
                journal = Journal(directory)
                if journal.data and journal.data.get("user_id") != account["user_id"]:
                    raise IntegrationError("Saved round account mismatch.")
                if command == "quit":
                    click.echo("Back to coding. No action sent; an existing wager is not refunded.")
                    return
                if command != "status":
                    result = await step(client, journal, account, command, stake, confirm, number)
                    click.echo(result["message"])
                    if result["status"] == "resolved":
                        click.echo(f"Payout: {format_v(result.get('payout_v', 'see wallet'))}; net: {format_v(result.get('net_v', 'see wallet'))}")
                click.echo(f"OpenVegas / Games | balance at entry: {format_v(account['balance'])}")
                if journal.data:
                    data = journal.data
                    click.echo(summary(data["game"], data.get("result", {})))
                    if data.get("pending"):
                        click.echo(f"Uncertain {data['game']} round, stake {format_v(data['wager'])}. ov game resume --confirm replays the pending operation, not a new wager.")
                    else:
                        actions = [a for a in data.get("result", {}).get("valid_actions", []) if a in MOVES]
                        if actions:
                            click.echo("Next: " + " | ".join("ov game " + a + (" --number N" if a == "bet_number" else "") for a in actions))
                else:
                    catalog = await client.human_casino_list_games()
                    names = [g["game_code"] for g in catalog.get("games", []) if g.get("game_code") in GAMES]
                    click.echo("Games: " + ", ".join(names))
                    click.echo("Minimum: " + format_v(catalog.get("min_wager_v")) + ". Review: ov game GAME --stake AMOUNT")
                click.echo("In Codex prefix these commands with !. Each command exits; no model request is made.")
    try:
        asyncio.run(execute())
    except IntegrationError as exc:
        raise click.ClickException(str(exc)) from None
    except Exception:
        raise click.ClickException("Game stopped safely. Check login/connectivity. Any pending operation remains recoverable with its original key; no automatic retry.") from None
