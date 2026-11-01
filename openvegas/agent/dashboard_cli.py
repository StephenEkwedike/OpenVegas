"""Thin CLI for existing organization/account APIs. No automatic wagering."""

import asyncio
import json
from urllib.parse import urljoin

import click

from openvegas.client import APIError, OpenVegasClient
from openvegas.config import get_backend_url


@click.group()
def agents():
    """View agent organizations, usage and limits."""


def _run(operation):
    async def run():
        async with OpenVegasClient() as client:
            try:
                data = await operation(client)
            except APIError as exc:
                raise click.ClickException(
                    f"Agent API request failed ({exc.status}). Log in and check organization access."
                ) from None
            click.echo(json.dumps(data, indent=2, default=str))

    asyncio.run(run())


@agents.command("list")
def organizations():
    """List organizations you administer."""
    _run(lambda c: c._request("GET", "/v1/agent/organizations"))


@agents.command()
@click.argument("org_id", type=click.UUID)
def accounts(org_id):
    """List an organization's existing agent accounts."""
    _run(lambda c: c.agent_admin_list_accounts(org_id=str(org_id)))


@agents.command()
@click.argument("org_id", type=click.UUID)
@click.option("--limit", type=click.IntRange(1, 200), default=50)
def usage(org_id, limit):
    """Show bounded inference and game activity, including losses."""
    _run(lambda c: c.agent_admin_get_audit(org_id=str(org_id), limit=limit))


@agents.command()
@click.option("--open", "open_browser", is_flag=True, help="Open the dashboard in your browser.")
def dashboard(open_browser):
    """Show the account-scoped dashboard URL."""
    url = urljoin(get_backend_url().rstrip("/") + "/", "ui/agents")
    click.echo(url)
    if open_browser:
        click.launch(url)
