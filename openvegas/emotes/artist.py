"""Unregistered local artist tools; never install, equip, upload or grant access."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import click
from rich.console import Console
from rich.live import Live

from .manifest import LoadedPack, PackError, load_pack
from .render import fit_frame, motion_allowed, rich_frame

_RENDER_OWNER = threading.Lock()
_TICK = 1 / 30
_MAX_TICKS = 210


def _load(directory: Path) -> LoadedPack:
    # Do not resolve the path: load_pack must see and reject a symlink pack root.
    try:
        return load_pack(directory)
    except PackError as exc:
        raise click.ClickException(str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise click.ClickException("Artist pack is unavailable or invalid") from exc


def _console() -> Console:
    return Console(
        file=sys.stdout,
        force_terminal=False if not sys.stdout.isatty() else None,
        no_color="NO_COLOR" in os.environ,
    )


def _render(console: Console, pack: LoadedPack, index: int):
    frame = pack.frame(index)
    try:
        fitted = fit_frame(frame, max_columns=console.width, max_rows=max(0, console.height - 4))
        try:
            return rich_frame(fitted)
        finally:
            if fitted is not None:
                fitted.close()
    finally:
        frame.close()


def _preview(pack: LoadedPack, *, reduced_motion: bool) -> None:
    if not _RENDER_OWNER.acquire(blocking=False):
        raise click.ClickException("Another artist preview already owns this terminal")
    try:
        _preview_owned(pack, reduced_motion=reduced_motion)
    finally:
        _RENDER_OWNER.release()


def _preview_owned(pack: LoadedPack, *, reduced_motion: bool) -> None:
    console = _console()
    manifest = pack.manifest
    click.echo("Local artist preview only; no installation, ownership grant or premium access.")
    animated = (
        not reduced_motion
        and motion_allowed(is_tty=console.is_terminal)
        and console.color_system == "truecolor"
    )
    if not animated:
        click.echo(
            f"{manifest.pack_id}: static preview ({manifest.width}x{manifest.height}); no animation."
        )
        if console.is_terminal and console.color_system and "NO_COLOR" not in os.environ:
            console.print(_render(console, pack, manifest.reduced_motion_frame))
        return

    clip = manifest.animations["complete"]
    started = time.monotonic()
    with Live(
        _render(console, pack, clip.frames[0]),
        console=console,
        auto_refresh=False,
        transient=True,
        screen=False,
    ) as live:
        previous = None
        # Deadline plus a fixed iteration cap: no stalled-clock endless repaint.
        for _ in range(_MAX_TICKS):
            elapsed = max(0.0, time.monotonic() - started)
            if elapsed >= clip.duration:
                break
            index = clip.frame_at(elapsed)
            signature = (index, console.width, console.height)
            if signature != previous:
                live.update(_render(console, pack, index), refresh=True)
                previous = signature
            time.sleep(min(_TICK, clip.duration - elapsed))


@click.group()
def artist():
    """Validate and preview your local pack folder in this owned terminal.

    Use a separate terminal while another CLI is active. PATH is a directory
    containing manifest.json and its PNG sheet, not a store/premium identifier.
    """


@artist.command("validate")
@click.argument("directory", metavar="PATH", type=click.Path(path_type=Path, resolve_path=False))
def validate(directory: Path):
    """Check the full manifest, PNG checksum, geometry and animation bounds."""
    manifest = _load(directory).manifest
    click.echo(
        f"Valid: {manifest.pack_id} v{manifest.version}; "
        f"{manifest.width}x{manifest.height}; "
        f"complete {manifest.animations['complete'].duration:.3f}s."
    )
    click.echo("Validation does not approve art, pricing, licensing or ownership.")


@artist.command("preview")
@click.argument("directory", metavar="PATH", type=click.Path(path_type=Path, resolve_path=False))
@click.option("--reduced-motion", "--no-animate", is_flag=True, help="Show one static frame only.")
def preview(directory: Path, reduced_motion: bool):
    """Validate PATH, then show one bounded completion clip. Ctrl+C cleans up."""
    pack = _load(directory)
    try:
        _preview(pack, reduced_motion=reduced_motion)
    except (OSError, ValueError) as exc:
        raise click.ClickException("Artist preview unavailable for this terminal") from exc
