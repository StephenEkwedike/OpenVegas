"""Click group for coordinator registration. All studio operations remain local."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import click

from .catalog import catalog, guidelines
from .player import watch
from .render import frame_at
from .schema import CHANNEL_IDS, EpisodeValidationError, load_episode, validate_episode


def _read(path: Path) -> dict:
    try:
        return load_episode(path)
    except EpisodeValidationError as exc:
        raise click.ClickException(str(exc)) from None
    except OSError as exc:
        raise click.ClickException(f"Cannot read local episode: {exc.strerror}") from None


def save_draft(episode: dict, output: Path | str) -> Path:
    """Atomically create a private JSON draft without replacing any existing path."""
    data = validate_episode(episode)
    output = Path(output)
    raw = (json.dumps(data, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".ov-tv-", delete=False) as target:
            temporary = Path(target.name)
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        # link is atomic and fails for existing files, directories, and symlinks.
        os.link(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def _save(episode: dict, output: Path) -> None:
    try:
        save_draft(episode, output)
    except EpisodeValidationError as exc:
        raise click.ClickException(str(exc)) from None
    except OSError as exc:
        raise click.ClickException(f"Cannot create draft (existing paths are never replaced): {exc.strerror}") from None


@click.group()
def tv() -> None:
    """Local captioned TV animatics. Billing disabled; not a live-TV service."""


@tv.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print the reusable catalog as JSON.")
def list_channels(as_json: bool) -> None:
    """List twelve slots and their actual availability."""
    listing = catalog()
    if as_json:
        click.echo(json.dumps(listing, ensure_ascii=True))
        return
    for channel in listing["channels"]:
        click.echo(f"{channel['id']}  {channel['name']}: {channel['message']}")
    billing = listing["billing"]
    click.echo(
        f"Billing disabled. Approved: US${billing['pay_as_you_go']['price_usd']:.2f}/hour PAYG "
        f"or US${billing['monthly_all_channel_pass']['price_usd']:g}/month unlimited all-channel pass. "
        "Not available for purchase."
    )


@tv.command("watch")
@click.argument("channel", type=click.Choice(CHANNEL_IDS), default="01")
def watch_channel(channel: str) -> None:
    """Open an owned alternate-screen player. Q or Ctrl+C closes it."""
    try:
        watch(channel)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from None


@tv.group()
def studio() -> None:
    """Create and check local data-only drafts. Never publishes or installs."""


@studio.command("init")
@click.argument("output", type=click.Path(path_type=Path))
@click.option("--channel", type=click.Choice(CHANNEL_IDS), default="01", show_default=True)
def studio_init(output: Path, channel: str) -> None:
    """Write a new editable starter JSON file; never overwrite an existing file."""
    episode = {
        "schema_version": 1, "id": "my-original-pilot", "channel_id": channel,
        "title": "My original pilot", "description": "An unpublished local storyboard draft.",
        "duration_seconds": 10, "canvas": {"width": 320, "height": 180},
        "credits": "Local draft. Creator must verify rights before any submission.",
        "scenes": [{
            "id": "opening", "title": "Opening scene", "description": "Replace with original art.",
            "start": 0, "end": 10, "background": "#f4ead5",
            "shapes": [{"id": "card", "kind": "rect", "x": 70, "y": 40,
                        "width": 180, "height": 100, "fill": "#278b7b"}],
        }],
        "captions": [{"start": 0, "end": 10, "text": "Write an original opening caption here."}],
    }
    _save(episode, output)
    click.echo("Local starter created. Edit the JSON, then run tv studio validate FILE.")


@studio.command("validate")
@click.argument("source", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="Print the normalized validated episode.")
def studio_validate(source: Path, as_json: bool) -> None:
    """Validate schema and safety limits, not copyright ownership or publication."""
    episode = _read(source)
    click.echo(json.dumps(episode, ensure_ascii=True) if as_json else
               f"Valid local storyboard: {episode['id']} ({episode['duration_seconds']:g}s). Not published.")


@studio.command("preview")
@click.argument("source", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="Return one evaluated frame instead of opening TV.")
@click.option("--at", type=click.FloatRange(min=0, max=1800), default=0, show_default=True,
              help="Frame time in seconds for --json.")
def studio_preview(source: Path, as_json: bool, at: float) -> None:
    """Preview a local draft in the player, or inspect its timed frame as JSON."""
    episode = _read(source)
    if as_json:
        try:
            click.echo(json.dumps(frame_at(episode, at), ensure_ascii=True))
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        return
    try:
        watch(episode=episode)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from None


@studio.command("apply")
@click.argument("source", type=click.Path(path_type=Path))
@click.option("--output", required=True, type=click.Path(path_type=Path),
              help="New local draft path. No backend submission; never overwritten.")
def studio_apply(source: Path, output: Path) -> None:
    """Save a validated local draft only. No application, publication, or billing."""
    episode = _read(source)
    _save(episode, output)
    click.echo(json.dumps({
        "status": "local_draft", "episode_id": episode["id"], "path": str(output),
        "submitted": False, "published": False, "installed": False, "billing": {"enabled": False},
    }, ensure_ascii=True))


@studio.command("guidelines")
def studio_guidelines() -> None:
    """Print authoring limits and the local-only publication boundary as JSON."""
    click.echo(json.dumps(guidelines(), ensure_ascii=True))
