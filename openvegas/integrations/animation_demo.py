"""Launch real native hosts for visual tests; no model prompts or $V sessions."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

import click

from .codex_pets import PREVIEW_PAIRS, install_previews
from .installer import detect, host_environment
from .native import run_host


def reports_sixel(fd, *, timeout=0.5):
    """Bounded DA1 query before host startup; restore the terminal on every exit."""
    import termios
    import tty
    old = termios.tcgetattr(fd)
    response = bytearray()
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        os.write(fd, b"\x1b[c")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(response) < 128:
            if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            response.extend(os.read(fd, 1))
            if response.endswith(b"c"):
                break
        match = re.fullmatch(rb"\x1b\[\?([0-9;]+)c", response)
        return bool(match and b"4" in match[1].split(b";")[1:])
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)


def prepare_sixel_terminfo(directory, env):
    """Compile the same xterm-derived profile for temporary or persistent setup."""
    source = Path(directory) / "terminal.src"
    source.write_text("openvegas-sixel|VS Code xterm with Sixel images,\n\tuse=xterm-256color,\n", encoding="ascii")
    result = subprocess.run(["tic", "-o", str(directory), str(source)], env=env,
                            capture_output=True, timeout=5, check=False)
    if result.returncode:
        raise ValueError("Could not compile the Sixel terminal profile")


@contextmanager
def graphics_environment(env):
    """Declare real, verified Sixel support, without impersonating another app.

    Codex 0.163 detects Sixel in TERM but not VS Code's terminal brand. Give
    just this process a valid xterm-derived terminfo; never change the shell.
    """
    if any(k in env for k in ("TMUX", "TMUX_PANE", "STY", "ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_VERSION")):
        raise click.ClickException("Codex pets need a direct terminal, outside tmux/screen/Zellij.")
    program = env.get("TERM_PROGRAM", "").lower()
    term = env.get("TERM", "").lower()
    known = (program in {"ghostty", "kitty", "wezterm"} or
             any(env.get(k) for k in ("KITTY_WINDOW_ID", "WEZTERM_EXECUTABLE", "WEZTERM_VERSION")) or
             any(k in term for k in ("kitty", "ghostty", "wezterm")))
    version = re.match(r"(\d+)\.(\d+)", env.get("TERM_PROGRAM_VERSION", ""))
    if program == "iterm.app" and version:
        known = tuple(map(int, version.groups())) >= (3, 6)
    if known:
        yield env
        return
    with open("/dev/tty", "r+b", buffering=0) as terminal:
        if not reports_sixel(terminal.fileno()):
            raise click.ClickException(
                'The terminal is not reporting Sixel image support. In VS Code USER settings enable '
                '"terminal.integrated.enableImages": true and '
                '"terminal.integrated.gpuAcceleration": "on", then open a new terminal and try again. '
                'No host or paid request was started.')
    with tempfile.TemporaryDirectory(prefix="ov-sixel-") as directory:
        try:
            prepare_sixel_terminfo(directory, env)
        except ValueError:
            raise click.ClickException("Could not prepare the temporary Sixel terminal profile; no host started.")
        child = {**env, "TERM": "openvegas-sixel", "TERMINFO": directory}
        # Codex's brand-first detection discards TERM when TERM_PROGRAM exists.
        # Use the verified generic capability, not a forged Kitty/iTerm identity.
        child.pop("TERM_PROGRAM", None)
        child.pop("TERM_PROGRAM_VERSION", None)
        yield child


@click.command("demo")
@click.argument("host", type=click.Choice(["claude", "codex"]))
@click.option("--pair", type=click.IntRange(1, 3), default=1, show_default=True,
              help="Codex public-art pair: 1 beat/dunk, 2 courier/bicycle, 3 visor/three-point.")
@click.option("--dry-run", is_flag=True, help="Explain without installing or launching anything.")
def demo(host, pair, dry_run):
    """See animations INSIDE Claude/Codex, without submitting an AI request."""
    info = detect(host)
    if not info:
        raise click.ClickException(f"Install {host} first.")
    minimum = (0, 163, 0) if host == "codex" else (2, 1, 289)
    if tuple(map(int, info["version"].split("."))) < minimum:
        raise click.ClickException(f"Update {host}; this animation integration requires {'.'.join(map(str, minimum))} or newer.")
    env = dict(host_environment() or os.environ)
    argv = [info["executable"]]
    click.echo(f"Opening actual {host}, not OpenVegas chat. No AI prompt is submitted.")
    click.echo("Prompts you choose to type afterward use your normal host billing, not $V.")
    if host == "claude":
        click.echo("Inside Claude, type /openvegas demo. It plays your equipped dance and completion once.")
        click.echo("Requires the OpenVegas plugin and an owned companion/completion; maximize the terminal pane.")
    else:
        pet = "openvegas-demo-" + PREVIEW_PAIRS[pair - 1][0]
        argv += ["-c", "tui.pet=" + json.dumps(pet), "-c", "tui.animations=true",
                 "-c", 'tui.pet_anchor="composer"', "-c", "tui.auto_recap=false"]
        click.echo(f"Public animation test, pair {pair}: dance + completion loop while idle. Not a real task result.")
        click.echo("/pets selects other previews; /pets off stops it. No permanent pet selection change.")
    if dry_run:
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise click.ClickException("Run this command in an interactive terminal, not a pipe or output panel.")
    try:
        if host == "codex":
            with graphics_environment(env) as child_env:
                home = Path(env.get("CODEX_HOME") or str(Path(env.get("HOME", str(Path.home()))) / ".codex"))
                install_previews(home, demos=True)
                code = asyncio.run(run_host(argv, child_env))
        else:
            code = asyncio.run(run_host(argv, env))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise click.ClickException("Animation setup failed safely; existing host configuration was preserved.") from None
    raise click.exceptions.Exit(code)
