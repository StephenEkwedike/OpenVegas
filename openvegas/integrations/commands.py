"""One-time setup, status and removal. No replacement coding-agent launcher."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import subprocess
from pathlib import Path

import click

from .broker import Broker
from .credentials import IntegrationError
from .installer import (HOSTS, detect, host_environment, identity, install, registration, uninstall,
                        other_installations, retire_installation)
from .controls import action_catalog
from .game_result import amount as format_v
from .credentials import backend_url


def _interactive_terminal():
    return sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("CI")


def _default_terminal_consent():
    click.echo("Recommended for OpenVegas games and Codex animations in VS Code: connects game tables and enables images.")
    click.echo("This changes ALL new VS Code integrated terminals, not just Codex. Existing tabs stay unchanged.")
    click.echo("Undo: ov integration terminal --install --restore-default. Host billing and shell startup files stay unchanged.")
    if not _interactive_terminal():
        click.echo("Interactive confirmation required; default terminal unchanged.")
        return False
    try:
        return click.confirm(click.style("Make OpenVegas Graphics your default VS Code terminal?", fg="blue"), default=False)
    except click.Abort:
        click.echo("Default terminal unchanged.")
        return False


def _offer_default_terminal():
    """A separate opt-in; plugin --yes is never consent for editor defaults."""
    if sys.platform != "darwin":
        return
    from .terminal_profile import configure
    env = dict(host_environment() or os.environ)
    home = Path(env.get("HOME", str(Path.home())))
    if not (home / "Library/Application Support/Code/User").is_dir():
        return
    try:
        if configure(home, env, replace_owned=True).get("is_default"):
            configure(home, env, apply=True, replace_owned=True)
            click.echo("OpenVegas Graphics is already your VS Code default. Use a NEW terminal tab.")
        elif _default_terminal_consent():
            configure(home, env, apply=True, make_default=True, replace_owned=True)
            click.echo("Default saved. Open a NEW VS Code terminal, then run codex or claude normally.")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        click.echo("Optional terminal setup was not applied: " + str(exc))


async def browser_login(*, allow_file=False):
    broker = Broker()
    try:
        return await broker.authorize(allow_file=allow_file, emit=click.echo)
    finally:
        await broker.close()


@click.command("go")
@click.option("--host", type=click.Choice([*HOSTS, "both"]))
@click.option("--dry-run", is_flag=True, help="Show proposed setup without login or config changes.")
@click.option("--yes", is_flag=True, help="Approve the displayed plugin setup and narrow game-entry permission.")
@click.option("--keep-game-approvals", is_flag=True, help="Keep repeated host approval prompts for the game-entry tool.")
@click.option("--verbose", is_flag=True, help="Show plugin paths and setup diagnostics.")
@click.option("--allow-file-credential", is_flag=True, help="Accept POSIX 0600 storage if the OS keychain is unavailable.")
def go(host, dry_run, yes, allow_file_credential, keep_game_approvals, verbose):
    """Connect OpenVegas to Claude/Codex once; then launch claude/codex normally."""
    detected = [name for name in HOSTS if detect(name)]
    if not detected:
        raise click.ClickException("Install Claude Code or Codex first. No host settings were changed.")
    if host is None:
        host = "both" if dry_run else click.prompt("Integrate with", type=click.Choice(detected + ["both"]), default="both")
    hosts = detected if host == "both" else [host]
    try:
        from urllib.parse import urlsplit
        site = backend_url()
        local = urlsplit(site).hostname in {"localhost", "127.0.0.1", "::1"}
        click.echo("OpenVegas / " + ("Local development" if local else "Hosted") + " / " + site)
        plans = [install(name, dry_run=True) for name in hosts]
        replacements = {name: other_installations(name) for name in hosts}
        for plan in plans:
            if verbose or dry_run:
                click.echo(f"{plan['host']} {plan['version']}: {plan['mode']}")
                click.echo("  Plugin: " + plan["path"])
                click.echo("  Host home: " + plan["host_home"])
        click.echo("Connect " + " + ".join(hosts) + ". Host billing stays unchanged.")
        if any(replacements.values()):
            click.echo("Replace the older OpenVegas connection. Saved accounts and other plugins stay unchanged.")
        if not keep_game_approvals:
            click.echo("Fast game entry: trust only OpenVegas play, not other tools. You still enter each wager yourself.")
        if dry_run:
            return
        if not yes:
            click.confirm("Connect your account and apply this OpenVegas plugin/game-entry setup?", abort=True)
        account = asyncio.run(browser_login(allow_file=allow_file_credential))
        click.echo("Wallet: " + format_v(account["balance"]))
        for name in hosts:
            result = install(name, upgrade=True)
            for previous in replacements[name]:
                retire_installation(previous)
            if verbose:
                click.echo(name + ": " + ("already installed" if result.get("unchanged") else result["reload"]))
            if not keep_game_approvals:
                from .game_permissions import configure
                try:
                    permission = configure(name, apply=True)
                    if verbose:
                        click.echo(name + " game entry: " + permission["status"] + ". " + permission.get("message", ""))
                except (IntegrationError, OSError):
                    click.echo(name + ": game-entry permission unchanged; use the host's approval prompt.")
        if "codex" in hosts:
            try:
                from .codex_pets import install_previews
                info = detect("codex")
                if info and tuple(map(int, info["version"].split("."))) >= (0, 163, 0):
                    env = host_environment() or os.environ
                    home = Path(env.get("CODEX_HOME") or str(Path(env.get("HOME", str(Path.home()))) / ".codex"))
                    install_previews(home, upgrade=True)
                    install_previews(home, demos=True, upgrade=True)
            except (OSError, ValueError):
                click.echo("Public pets were not updated; existing files preserved. Run ov integration pets --install --upgrade for details.")
        click.echo("Ready. Restart Claude/Codex; accept its one-time plugin trust prompt.")
        click.echo("Play: Claude /openvegas play | Codex $openvegas play. Pets: /pets.")
        if verbose:
            click.echo("Controls: " + ", ".join(action["name"] for action in action_catalog()) + ".")
            click.echo("Restore game approvals: ov integration game-permissions --restore")
        _offer_default_terminal()
    except (IntegrationError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, IntegrationError) else "Local integration setup failed; existing application data was preserved."
        raise click.ClickException(message) from None


@click.group("integration")
def integration():
    """Inspect or remove native-host integration (no automatic spending)."""


@integration.command("game-permissions")
@click.option("--host", type=click.Choice([*HOSTS, "both"]), default="both")
@click.option("--restore", is_flag=True, help="Remove only unchanged permission rules added by OpenVegas.")
@click.option("--dry-run", is_flag=True)
def game_permissions(host, restore, dry_run):
    """Allow only human game-entry without repeat MCP approval, or undo that choice."""
    from .game_permissions import configure
    for name in (HOSTS if host == "both" else [host]):
        try:
            click.echo(json.dumps(configure(name, apply=not dry_run, restore=restore)))
        except (IntegrationError, OSError) as exc:
            raise click.ClickException(str(exc)) from None


@integration.command("game-view")
@click.option("--install", "install_view", is_flag=True, help="Install and enable the local VS Code graphical game experiment.")
@click.option("--disable", is_flag=True, help="Restore Codex's native game forms; keep wallet and rounds unchanged.")
def game_view(install_view, disable):
    """Reversible VS Code game tab for $openvegas play (local preview)."""
    import tempfile
    from .credentials import profile
    from .game_view import private_directory, settings, editor_scheme, editor_launcher
    if install_view and disable:
        raise click.ClickException("Choose --install or --disable, not both.")
    try:
        if not install_view and not disable:
            click.echo("VS Code game tab enabled" if settings() else "Native game forms enabled")
            return
        if disable:
            value = {"enabled": False}
        else:
            if os.name != 'posix':
                raise click.ClickException("This local preview currently requires macOS or Linux.")
            env = dict(host_environment() or os.environ)
            launcher = editor_launcher(env)
            if not launcher:
                raise click.ClickException("Install VS Code's code command in PATH first.")
            from .game_view_package import build, ROOT
            scheme = editor_scheme(launcher)
            artifact = build(ROOT / '.local/artifacts/openvegas-game-table-0.1.0.vsix')
            result = subprocess.run([launcher, '--install-extension', str(artifact), '--force'],
                                    env=env, capture_output=True, text=True, timeout=45)
            if result.returncode:
                raise click.ClickException("VS Code did not install the local extension. Native forms unchanged.")
            home = Path(env.get('HOME', str(Path.home())))
            directory = private_directory(home / '.openvegas/game-view/requests')
            value = {'enabled': True, 'requests_dir': str(directory), 'launcher': launcher,
                     'uri_scheme': scheme, 'host_home': str(home)}
        root = profile()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = root / 'game-view.json'
        if any(p.is_symlink() for p in (target, *target.parents)):
            raise click.ClickException('Redirected game-view configuration; unchanged.')
        fd, temporary = tempfile.mkstemp(prefix='.game-view-',dir=root)
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump(value,stream)
            os.replace(temporary,target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        click.echo('Native game forms restored. No wallet or round changes.' if disable else
                   'Local VS Code game tab installed and enabled. Reload VS Code, then use $openvegas play in regular Codex. '
                   'Undo: ov integration game-view --disable. No wallet, provider or terminal settings changed.')
    except (OSError, ValueError, IntegrationError, subprocess.TimeoutExpired) as exc:
        raise click.ClickException('Game-view setup stopped: '+type(exc).__name__) from None


@integration.command("pets")
@click.option("--install", "apply", is_flag=True, help="Install the three public-preview pairs into Codex's native pet picker.")
@click.option("--upgrade", is_flag=True, help="Back up and update verified prior OpenVegas exports; refuse modified/foreign files.")
@click.option("--review-pack", type=click.Choice(["q50-jewelry"]), help="Install only this private local artwork review and its no-prompt demo; no purchase or billing change.")
def pets(apply, upgrade, review_pack):
    """Use Codex's own in-session renderer; no companion window or premium export."""
    from .codex_pets import install_previews
    info = detect("codex")
    if not info or tuple(map(int, info["version"].split("."))) < (0, 163, 0):
        raise click.ClickException("Codex CLI 0.163.0 or newer is required for this native pet adapter.")
    env = host_environment() or os.environ
    home = Path(env.get("CODEX_HOME") or str(Path(env.get("HOME", str(Path.home()))) / ".codex"))
    click.echo("Codex native pets: private local artwork review, not for sale." if review_pack else "Codex native pets: public preview artwork only, not owned/purchased assets.")
    click.echo("Requires a supported graphics terminal (for example iTerm2 3.6+, Kitty, Ghostty or WezTerm); not tmux/Zellij.")
    click.echo("Launch codex normally, then /pets; choose OV Demo Q50 Motion for no-prompt playback, or OV Q50 Motion (Status) for host-status playback." if review_pack else "Launch codex normally, then /pets; choose OV Demo 1/2/3 for no-prompt animation loops.")
    click.echo("Codex 0.163 in VS Code: ov integration terminal --install --make-default (asks [y/N]), or choose OpenVegas Graphics manually.")
    click.echo("Pet folder: " + str(home / "pets"))
    if not apply:
        click.echo("Run ov integration pets --install --review-pack q50-jewelry to add Q50 without changing selection or billing." if review_pack else "Run ov integration pets --install to add the three pairs without changing the selected pet or billing.")
        return
    try:
        options = {"upgrade": True} if upgrade else {}
        if review_pack:
            options["review_pack"] = review_pack
        install_previews(home, **options)
        install_previews(home, demos=True, **options)
    except (OSError, ValueError) as exc:
        raise click.ClickException("Pet installation refused; existing unrelated files were preserved: " + type(exc).__name__) from None
    click.echo("Installed. Start a fresh Codex terminal session and use /pets to select an OpenVegas preview.")
    click.echo("Q50: idle/running stay still; review plays one jewelry sequence. Demo loops independently. /pets off disables it." if review_pack else "Running: dance. Ready: one completion clip. /pets off disables it. This is not native UX certification.")


@integration.command("terminal")
@click.option("--install", "apply", is_flag=True, help="Apply VS Code macOS graphics terminal setup; otherwise dry-run.")
@click.option("--make-default", is_flag=True, help="Ask [y/N] before using the profile for all new VS Code terminals.")
@click.option("--restore-default", is_flag=True, help="Restore the previous default saved by OpenVegas, preserving later user changes.")
@click.option("--game-handoff", is_flag=True, help="Add OpenVegas Games: same-terminal game handoff for ordinary codex/claude, without changing the default.")
@click.option("--attach", is_flag=True, help="Activate game handoff in THIS terminal shell. No new window/tab. Exit the coding host first, then launch it normally again.")
@click.option("--check", "check_connection", is_flag=True, help="Check this shell's game connection without opening a game or accessing a wallet.")
def terminal(apply, make_default, restore_default, game_handoff, attach, check_connection):
    """One-time VS Code setup for plain claude/codex commands, not $V billing."""
    from .terminal_profile import configure
    if check_connection:
        if apply or make_default or restore_default or game_handoff or attach:
            raise click.UsageError("Use --check by itself in the existing terminal shell.")
        from .terminal_handoff import check
        result = asyncio.run(check())
        click.echo(json.dumps(result, indent=2))
        if not result['available']:
            raise click.exceptions.Exit(1)
        return
    if attach:
        if apply or make_default or restore_default or game_handoff:
            raise click.UsageError("Use --attach by itself in the existing terminal shell.")
        if os.name != "posix":
            raise click.ClickException("Same-terminal handoff currently requires macOS/Linux.")
        from .terminal_relay import run
        if os.getenv("OPENVEGAS_TERMINAL_SOCKET"):
            raise click.ClickException("This terminal already has game handoff enabled.")
        env = dict(host_environment() or os.environ)
        # Keep the account selected by the local launcher while restoring the
        # native host HOME. These are routing identifiers, never credentials.
        from .credentials import backend_url, profile
        env.update(OPENVEGAS_BACKEND_URL=backend_url(),
                   OPENVEGAS_INTEGRATION_PROFILE=str(profile()))
        click.echo("Same-terminal game handoff active. Run codex or claude here as usual. No new tab, billing change or host replacement.")
        os.environ.clear()
        os.environ.update(env)
        run([env.get("SHELL", "/bin/zsh"), "-l"])
        return
    if sys.platform != "darwin":
        raise click.ClickException("This VS Code profile installer currently supports macOS only.")
    env = dict(host_environment() or os.environ)
    try:
        home = Path(env.get("HOME", str(Path.home())))
        if make_default and restore_default:
            raise ValueError("Choose make-default or restore-default, not both")
        if make_default and apply and not _default_terminal_consent():
            return
        options = {"apply": apply}
        if game_handoff:
            options["game_handoff"] = True
        if make_default or restore_default:
            options.update(make_default=make_default, restore_default=restore_default)
        result = configure(home, env, **options)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Terminal setup failed; no host was launched."
        raise click.ClickException(message) from None
    click.echo(("Installed: " if result["applied"] else "Proposed: ") + result["profile"])
    click.echo("VS Code terminal '+' dropdown > " + result["profile"] + ". Run claude or codex normally, then /pets.")
    click.echo("Games are included: $openvegas play (Codex) or /openvegas play (Claude), then choose a game. Quit returns to coding.")
    click.echo("Managed zsh terminals use Codex's supported --no-daemon/--no-alt-screen options; ordinary codex commands still work.")
    if restore_default:
        click.echo("Previous default restored." if apply else "Would restore the previous default.")
    elif make_default:
        click.echo("Default applies to NEW terminal tabs." if apply else "Would ask [y/N] before changing the default.")
    else:
        click.echo("Default profile unchanged. To opt in: ov integration terminal --install --make-default")
    click.echo("No host executable, billing or shell startup file changes.")
    click.echo("Requires images/GPU in VS Code, outside tmux/Zellij. Native visual acceptance still needs observation.")


@integration.command("status")
def status():
    for host in HOSTS:
        info = detect(host)
        click.echo(f"{host}: " + (info["version"] if info else "not detected"))
        if info:
            try:
                actual = registration(info, identity(host)[0])
                click.echo("  Integration: " + ("enabled" if actual["enabled"] else
                           "installed but disabled" if actual["installed"] else "not installed"))
                if actual["enabled"] and actual.get("path") != install(host, dry_run=True)["path"]:
                    click.echo("  Update available: run openvegas go to approve the current plugin.")
            except IntegrationError as exc:
                click.echo("  " + str(exc))
    async def check():
        broker = Broker()
        try:
            click.echo(json.dumps(await broker.account(), indent=2))
        finally:
            await broker.close()
    try:
        asyncio.run(check())
    except IntegrationError as exc:
        raise click.ClickException(str(exc)) from None


@integration.command("uninstall")
@click.argument("host", type=click.Choice(HOSTS))
def remove(host):
    """Remove only the integration installed by OpenVegas; keep data and other plugins."""
    try:
        from .game_permissions import configure
        configure(host, apply=True, restore=True)
        click.echo(json.dumps(uninstall(host), indent=2))
    except IntegrationError as exc:
        raise click.ClickException(str(exc)) from None


@integration.command("logout")
def logout():
    """Revoke this device and its short-lived host grants, keeping standalone login."""
    async def revoke():
        broker = Broker()
        try:
            await broker.revoke()
        finally:
            await broker.close()
    try:
        asyncio.run(revoke())
    except IntegrationError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo("Integration access revoked for this device.")


@integration.command("serve", context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.pass_context
def serve(ctx):
    """Internal MCP entry point for packaged native hosts."""
    from .mcp_server import main
    main(ctx.args)
