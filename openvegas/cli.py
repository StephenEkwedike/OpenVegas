"""OpenVegas CLI — Terminal Arcade for Developers."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from openvegas import __version__
from openvegas.tui.confetti import render_confetti
from openvegas.tui.hints import verify_hint_for_result

console = Console()


def run_async(coro):
    """Run an async function from sync Click context."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version=__version__)
def cli():
    """OpenVegas -- Terminal Arcade for Developers"""
    pass


# ---------------------------------------------------------------------------
# Auth commands
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--otp", is_flag=True, help="Use magic link (OTP) login")
def login(otp: bool):
    """Log in to OpenVegas."""
    from openvegas.auth import SupabaseAuth, AuthError

    try:
        auth = SupabaseAuth()
    except AuthError as e:
        console.print(f"[red]{e}[/red]")
        return

    email = Prompt.ask("Email")

    if otp:
        auth.login_with_otp(email)
        console.print("[green]Magic link sent! Check your email.[/green]")
    else:
        password = Prompt.ask("Password", password=True)
        try:
            result = auth.login_with_email(email, password)
            console.print(
                f"[green]Logged in as {result['email']}[/green]\n"
                f"[dim]user_id: {result.get('user_id', '')}[/dim]"
            )
        except Exception as e:
            console.print(f"[red]Login failed: {e}[/red]")


@cli.command()
def signup():
    """Create a new OpenVegas account."""
    from openvegas.auth import SupabaseAuth, AuthError

    try:
        auth = SupabaseAuth()
    except AuthError as e:
        console.print(f"[red]{e}[/red]")
        return

    email = Prompt.ask("Email")
    password = Prompt.ask("Password", password=True)

    try:
        result = auth.signup(email, password)
        console.print(
            f"[green]Account created for {result['email']}[/green]\n"
            f"[dim]user_id: {result.get('user_id', '')}[/dim]"
        )
    except Exception as e:
        console.print(f"[red]Signup failed: {e}[/red]")


@cli.command()
def logout():
    """Log out of OpenVegas."""
    from openvegas.auth import SupabaseAuth
    try:
        auth = SupabaseAuth()
        auth.logout()
    except Exception:
        from openvegas.config import clear_session
        clear_session()
    console.print("Logged out.")


@cli.command()
def status():
    """Show balance, tier, and stats."""
    async def _status():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.get_balance()
            console.print(Panel(
                f"[bold]Balance:[/bold] {data.get('balance', '0.00')} $V\n"
                f"[bold]Tier:[/bold] {data.get('tier', 'free')}\n"
                f"[bold]Lifetime minted:[/bold] {data.get('lifetime_minted', '0.00')} $V\n"
                f"[bold]Lifetime won:[/bold] {data.get('lifetime_won', '0.00')} $V",
                title="OpenVegas Status",
                border_style="cyan",
            ))
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    run_async(_status())


# ---------------------------------------------------------------------------
# Wallet commands
# ---------------------------------------------------------------------------

@cli.command()
def balance():
    """Show your $V balance."""
    async def _balance():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.get_balance()
            console.print(f"[bold]{data.get('balance', '0.00')} $V[/bold]")
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_balance())


@cli.command()
def history():
    """Show transaction history."""
    async def _history():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.get_history()
            entries = data.get("entries", [])
            if not entries:
                console.print("[dim]No transactions yet.[/dim]")
                return

            table = Table(title="Transaction History")
            table.add_column("Time", style="dim")
            table.add_column("Type")
            table.add_column("Amount", justify="right")
            table.add_column("Reference")

            for entry in entries[:20]:
                table.add_row(
                    entry.get("created_at", "")[:19],
                    entry.get("entry_type", ""),
                    entry.get("amount", ""),
                    entry.get("reference_id", "")[:20],
                )
            console.print(table)
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_history())


@cli.command()
@click.argument("amount")
def deposit(amount: str):
    """Buy $V with cash (returns Stripe checkout URL)."""
    async def _deposit():
        from openvegas.client import OpenVegasClient, APIError
        try:
            amt = Decimal(amount)
        except Exception:
            console.print("[red]Invalid amount. Example: openvegas deposit 10[/red]")
            return

        try:
            client = OpenVegasClient()
            data = await client.create_topup_checkout(amt)
            console.print(f"[green]Top-up ID:[/green] {data.get('topup_id')}")
            console.print(f"[green]Status:[/green] {data.get('status')}")
            if data.get("checkout_url"):
                console.print(f"[bold cyan]Checkout URL:[/bold cyan] {data['checkout_url']}")
            else:
                console.print("[yellow]No checkout URL returned. Try again or check deposit status.[/yellow]")
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_deposit())


@cli.command("deposit-status")
@click.argument("topup_id")
def deposit_status(topup_id: str):
    """Check status of a Stripe top-up."""
    async def _status():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.get_topup_status(topup_id)
            console.print(
                f"[bold]Status:[/bold] {data.get('status')} | "
                f"[bold]Credit:[/bold] {data.get('v_credit', '0')} $V"
            )
            if data.get("checkout_url"):
                console.print(f"[dim]Checkout URL: {data['checkout_url']}[/dim]")
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_status())


# ---------------------------------------------------------------------------
# Mint commands
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--amount", type=float, required=True, help="USD amount to burn")
@click.option(
    "--provider", type=click.Choice(["anthropic", "openai", "gemini"]), required=True
)
@click.option(
    "--mode", type=click.Choice(["solo", "split", "sponsor"]), default="solo"
)
def mint(amount: float, provider: str, mode: str):
    """Mint $V by burning LLM tokens (BYOK)."""
    from openvegas.config import get_provider_key

    api_key = get_provider_key(provider)
    if not api_key:
        console.print(
            f"[red]No API key for {provider}. Run: openvegas keys set {provider}[/red]"
        )
        return

    rates_display = {"solo": "standard rate", "split": "+8% $V bonus", "sponsor": "+15% $V bonus"}

    async def _mint():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()

            # 1. Get challenge
            challenge = await client.create_mint_challenge(amount, provider, mode)

            # 2. Show disclosure
            console.print(Panel(
                f"[bold]Mint Mode:[/bold] {mode.title()} Mint ({rates_display[mode]})\n"
                f"[bold]Provider:[/bold] {provider} ({challenge.get('model', '')})\n"
                f"[bold]Target burn ceiling:[/bold] up to ~${amount:.2f} on your account\n"
                f"[bold]Max $V credit cap:[/bold] {challenge.get('max_credit_v', '')} $V\n"
                f"[bold]Note:[/bold] actual burn depends on generated token usage and may be lower.\n"
                f"[bold]Your task:[/bold] {challenge.get('task_prompt', '')[:80]}...",
                title="OpenVegas Mint",
                border_style="green",
            ))

            if not Confirm.ask("Proceed with mint?"):
                console.print("[yellow]Mint cancelled.[/yellow]")
                return

            # 3. Send to backend for proxied mint
            console.print(
                "[dim]Sending key to server for proxied mint "
                "(key used once, never stored)...[/dim]"
            )

            result = await client.verify_mint(
                challenge["id"], challenge["nonce"],
                provider, challenge["model"], api_key,
            )

            console.print(
                f"[bold green]Minted {result['v_credited']} $V[/bold green] "
                f"(actual burn ~${float(result['cost_usd']):.4f} on {provider})"
            )

        except APIError as e:
            console.print(f"[red]Mint failed: {e.detail}[/red]")

    run_async(_mint())


# ---------------------------------------------------------------------------
# Keys management
# ---------------------------------------------------------------------------

@cli.group()
def keys():
    """Manage provider API keys."""
    pass


@keys.command("set")
@click.argument("provider", type=click.Choice(["anthropic", "openai", "gemini"]))
def keys_set(provider: str):
    """Set API key for a provider (stored locally)."""
    from openvegas.config import set_provider_key
    api_key = Prompt.ask(f"Enter {provider} API key", password=True)
    set_provider_key(provider, api_key)
    console.print(f"[green]{provider} API key saved to ~/.openvegas/config.json[/green]")


@keys.command("list")
def keys_list():
    """Show which providers have keys configured."""
    from openvegas.config import load_config
    config = load_config()
    providers = config.get("providers", {})
    for p in ["openai", "anthropic", "gemini"]:
        has_key = bool(providers.get(p, {}).get("api_key"))
        status = "[green]configured[/green]" if has_key else "[dim]not set[/dim]"
        console.print(f"  {p}: {status}")


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("game", type=click.Choice(["horse", "skillshot"]))
@click.option("--stake", type=float, required=True, help="Amount of $V to wager")
@click.option("--horse", type=int, default=None, help="Horse number (horse racing only)")
@click.option(
    "--type", "bet_type",
    type=click.Choice(["win", "place", "show"]), default="win",
)
@click.option("--render/--no-render", default=True, help="Render terminal animation/reveal when available")
@click.option(
    "--demo-force-win/--no-demo-force-win",
    default=False,
    help="Use admin-only demo win endpoint (non-canonical).",
)
def play(
    game: str,
    stake: float,
    horse: int,
    bet_type: str,
    render: bool,
    demo_force_win: bool,
):
    """Play a game and wager $V."""
    async def _play():
        from openvegas.client import OpenVegasClient, APIError
        from openvegas.games.base import GameResult
        from openvegas.games.horse_racing import HorseRacing
        from openvegas.games.skill_shot import SkillShotGame

        if game == "horse" and horse is None:
            console.print("[red]Horse racing requires --horse <number>.[/red]")
            return

        bet = {"amount": stake, "type": bet_type}
        if horse is not None:
            bet["horse"] = horse

        try:
            client = OpenVegasClient()
            if demo_force_win:
                result = await client.play_game_demo(game, bet)
            else:
                result = await client.play_game(game, bet)
            net = Decimal(str(result.get("net", "0")))
            payout = Decimal(str(result.get("payout", "0")))
            bet_amount = Decimal(str(result.get("bet_amount", stake)))
            game_id = str(result.get("game_id", ""))
            rendered = False

            if render:
                renderer_cls = {
                    "horse": HorseRacing,
                    "skillshot": SkillShotGame,
                }.get(game)
                if renderer_cls:
                    gr = GameResult(
                        game_id=game_id,
                        player_id="",
                        bet_amount=bet_amount,
                        payout=payout,
                        net=net,
                        outcome_data=result.get("outcome_data", {}) or {},
                        server_seed="",
                        server_seed_hash=str(result.get("server_seed_hash", "")),
                        client_seed="",
                        nonce=0,
                        provably_fair=bool(result.get("provably_fair", True)),
                    )
                    await renderer_cls().render(gr, console)
                    rendered = True

            if not rendered:
                # Fallback text-only result
                if net > 0:
                    console.print(
                        f"[bold green]Won {payout} $V! "
                        f"(+{net} net)[/bold green]"
                    )
                else:
                    console.print(f"[red]Lost {bet_amount} $V.[/red]")

            if net > 0:
                from openvegas.config import load_config
                if load_config().get("animation", True):
                    render_confetti(console)

            if result.get("demo_mode"):
                console.print("[bold yellow]DEMO MODE RESULT[/bold yellow] [dim](canonical: false)[/dim]")

            if result.get("provably_fair"):
                console.print(f"[dim]Verify: {verify_hint_for_result(game_id, False)}[/dim]")
            elif result.get("demo_mode"):
                console.print(f"[dim]Verify (demo): {verify_hint_for_result(game_id, True)}[/dim]")

        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_play())


# ---------------------------------------------------------------------------
# AI Inference
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("prompt")
@click.option("--provider", default=None, help="Provider (openai/anthropic/gemini)")
@click.option("--model", default=None, help="Model ID")
def ask(prompt: str, provider: str | None, model: str | None):
    """Use $V for AI inference."""
    from openvegas.config import get_default_provider, get_default_model

    if provider is None:
        provider = get_default_provider()
    if model is None:
        model = get_default_model(provider)

    async def _ask():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            result = await client.ask(prompt, provider, model)
            console.print(result.get("text", ""))
            console.print(
                f"\n[dim]Cost: {result.get('v_cost', '?')} $V | "
                f"Model: {provider}/{model}[/dim]"
            )
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_ask())


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--provider", default=None, help="Filter by provider")
def models(provider: str | None):
    """List available models and $V prices."""
    async def _models():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.list_models(provider)
            models_list = data.get("models", [])

            table = Table(title="Available Models")
            table.add_column("Provider")
            table.add_column("Model")
            table.add_column("Name")
            table.add_column("Input $/1M", justify="right")
            table.add_column("Output $/1M", justify="right")
            table.add_column("$V In/1M", justify="right")
            table.add_column("$V Out/1M", justify="right")
            table.add_column("Status")

            for m in models_list:
                status = "[green]enabled[/green]" if m.get("enabled") else "[red]disabled[/red]"
                table.add_row(
                    m.get("provider", ""),
                    m.get("model_id", ""),
                    m.get("display_name", ""),
                    str(m.get("cost_input_per_1m", "")),
                    str(m.get("cost_output_per_1m", "")),
                    str(m.get("v_price_input_per_1m", "")),
                    str(m.get("v_price_output_per_1m", "")),
                    status,
                )
            console.print(table)
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_models())


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

@cli.group()
def store():
    """Browse and buy from the redemption store."""
    pass


@store.command("list")
def store_list():
    """Browse the redemption catalog."""
    async def _list():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.store_list()
            items = data.get("items", {})

            table = Table(title="OpenVegas Store")
            table.add_column("ID")
            table.add_column("Name")
            table.add_column("Description")
            table.add_column("Cost ($V)", justify="right")
            table.add_column("Type")

            for item_id, item in items.items():
                table.add_row(
                    item_id,
                    item.get("name", ""),
                    item.get("description", ""),
                    str(item.get("cost_v", "")),
                    item.get("type", ""),
                )
            console.print(table)
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_list())


@store.command("buy")
@click.argument("item_id")
@click.option("--idempotency-key", default=None, help="Optional idempotency key for safe retries")
def store_buy(item_id: str, idempotency_key: str | None):
    """Buy an item from the store."""
    async def _buy():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.store_buy(item_id=item_id, idempotency_key=idempotency_key)
            console.print(
                f"[green]Order {data.get('order_id', '')}[/green] "
                f"status={data.get('status', '')} state={data.get('state', '')}"
            )
            console.print(f"[bold]Cost:[/bold] {data.get('cost_v', '0')} $V")
            grants = data.get("grants", [])
            if grants:
                table = Table(title="Granted Inference Credits")
                table.add_column("Provider")
                table.add_column("Model")
                table.add_column("Tokens", justify="right")
                for g in grants:
                    table.add_row(g.get("provider", ""), g.get("model_id", ""), str(g.get("tokens_total", 0)))
                console.print(table)
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_buy())


@store.command("grants")
def store_grants():
    """List remaining inference grants."""
    async def _grants():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.store_grants()
            grants = data.get("grants", [])
            if not grants:
                console.print("[dim]No inference grants found.[/dim]")
                return
            table = Table(title="Inference Grants")
            table.add_column("Provider")
            table.add_column("Model")
            table.add_column("Remaining", justify="right")
            table.add_column("Total", justify="right")
            table.add_column("Order")
            for g in grants:
                table.add_row(
                    g.get("provider", ""),
                    g.get("model_id", ""),
                    str(g.get("tokens_remaining", 0)),
                    str(g.get("tokens_total", 0)),
                    str(g.get("source_order_id", ""))[:8],
                )
            console.print(table)
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_grants())


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("game_id")
@click.option("--demo", is_flag=True, help="Verify against demo verification endpoint (non-canonical).")
def verify(game_id: str, demo: bool):
    """Verify a provably fair game outcome."""
    async def _verify():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            if demo:
                data = await client.verify_demo_game(game_id)
                console.print("[bold yellow]DEMO VERIFY[/bold yellow] [dim](canonical: false)[/dim]")
                console.print(f"  Server seed hash: {data.get('server_seed_hash', '')[:16]}...")
                console.print(f"  Nonce:            {data.get('nonce', '')}")
                return

            data = await client.verify_game(game_id)
            from openvegas.rng.provably_fair import ProvablyFairRNG

            valid = ProvablyFairRNG.verify(
                data.get("server_seed", ""),
                data.get("server_seed_hash", ""),
            )
            if valid:
                console.print("[bold green]Outcome verified! Seed matches commitment.[/bold green]")
            else:
                console.print("[bold red]Verification failed! Seed does not match.[/bold red]")

            console.print(f"  Server seed: {data.get('server_seed', '')[:16]}...")
            console.print(f"  Commitment:  {data.get('server_seed_hash', '')[:16]}...")
            console.print(f"  Client seed: {data.get('client_seed', '')}")
            console.print(f"  Nonce:       {data.get('nonce', '')}")
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_verify())


@cli.command("ui")
@click.option("--full", is_flag=True, help="Use legacy full-screen Textual UI mode.")
@click.option("--no-render", is_flag=True, help="Skip game animation rendering in inline UI.")
@click.option(
    "--render-timeout-sec",
    type=float,
    default=15.0,
    show_default=True,
    help="Inline UI render timeout in seconds.",
)
def interactive_ui(full: bool, no_render: bool, render_timeout_sec: float):
    """Open guided terminal UI."""
    if full:
        try:
            from openvegas.tui.wizard import run_wizard
        except Exception as e:  # pragma: no cover - runtime-only import fallback
            console.print(f"[red]Unable to load full UI mode: {e}[/red]")
            return
        run_wizard()
        return

    try:
        from openvegas.tui.prompt_ui import run_prompt_ui
    except Exception as e:  # pragma: no cover - runtime-only import fallback
        console.print(f"[red]Unable to load inline UI mode: {e}[/red]")
        return
    run_prompt_ui(no_render=no_render, render_timeout_sec=render_timeout_sec)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@cli.group("config")
def config_group():
    """Manage OpenVegas configuration."""
    pass


@config_group.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str):
    """Set a config value."""
    from openvegas.config import load_config, save_config

    config = load_config()

    if key == "default_provider":
        if value not in ("openai", "anthropic", "gemini"):
            console.print("[red]Provider must be openai, anthropic, or gemini[/red]")
            return
        config["default_provider"] = value
    elif key.startswith("default_model_"):
        provider = key.removeprefix("default_model_")
        models = config.get("default_model_by_provider", {})
        models[provider] = value
        config["default_model_by_provider"] = models
    elif key in ("theme", "animation", "backend_url", "supabase_url", "supabase_anon_key"):
        if key == "animation":
            value = value.lower() in ("true", "1", "yes")
        config[key] = value
    else:
        console.print(f"[red]Unknown config key: {key}[/red]")
        return

    save_config(config)
    console.print(f"[green]Set {key} = {value}[/green]")


@config_group.command("show")
def config_show():
    """Show current configuration."""
    from openvegas.config import load_config
    import json

    config = load_config()
    # Redact sensitive fields
    display = dict(config)
    if "session" in display:
        display["session"] = {
            k: v[:8] + "..." if v else "" for k, v in display["session"].items()
        }
    for p in display.get("providers", {}):
        if "api_key" in display["providers"][p]:
            key = display["providers"][p]["api_key"]
            display["providers"][p]["api_key"] = key[:8] + "..." if key else ""

    console.print(json.dumps(display, indent=2))


if __name__ == "__main__":
    cli()
