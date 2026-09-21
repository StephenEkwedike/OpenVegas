"""Emote discovery, online ownership, artist tools and opt-in companion commands."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

import click
from rich.console import Console
from rich.live import Live

from .artist import artist
from .controller import EmoteController
from .events import IDENTITY, MAX_EVENT_BYTES, Event, Phase
from .hooks import hooks
from .manifest import PackError
from .online import LeaseRefresher, remote_library
from .render import fit_frame, motion_allowed, rich_frame
from .resources import Catalog, PackRepository, preview_catalog
from .selection import SelectionStore
from .spool import (
    EVENT_FILE,
    EventSpool,
    SpoolError,
    _locked,
    _names,
    _read,
    atomic_write,
    state_stat,
    state_unlink,
)


@dataclass
class EmoteServices:
    repository: PackRepository
    catalog: Catalog
    selection: SelectionStore
    spool: EventSpool
    remote_factory: Callable | None = None


def default_services() -> EmoteServices:
    repository = PackRepository()
    return EmoteServices(
        repository, preview_catalog(repository), SelectionStore(), EventSpool(), remote_library
    )


def _services(ctx: click.Context) -> EmoteServices:
    services = ctx.find_object(EmoteServices)
    if services is None:
        try:
            services = default_services()
        except (OSError, ValueError) as exc:
            raise click.ClickException("Emote resources unavailable") from exc
        ctx.obj = services
    return services


def _load(services: EmoteServices, pack_id: str, *, preview: bool = False):
    resource = services.catalog.resource_for(pack_id, preview=preview)
    pack = services.repository.load(resource)
    entry = services.catalog.get(pack_id)
    if resource == entry.resource_name and pack.manifest.pack_id != pack_id:
        raise PackError("Catalog and pack identity mismatch")
    return pack


def _fail(exc):
    if isinstance(exc, (PackError, SpoolError)):
        raise click.ClickException(str(exc)) from exc
    raise click.ClickException(
        "Emote operation unavailable; check resources and private state permissions"
    ) from exc


def _list(services: EmoteServices, query: str = "") -> None:
    matches = [
        entry
        for entry in services.catalog.entries.values()
        if query.lower() in (entry.pack_id + " " + entry.display_name).lower()
    ]
    if not matches:
        click.echo("No matching packs. Approved package assets have not been installed.")
        return
    for entry in sorted(matches, key=lambda entry: entry.pack_id):
        click.echo(f"{entry.pack_id}  {entry.display_name}  [{entry.access}]")


@click.group(invoke_without_command=True)
@click.pass_context
def emote(ctx):
    """Choose owned emotes interactively, or list local previews when redirected."""
    if ctx.invoked_subcommand is None:
        services = _services(ctx)
        if sys.stdin.isatty() and sys.stdout.isatty():
            from .picker import choose

            try:
                choose(ctx, services)
            except (OSError, ValueError) as exc:
                _fail(exc)
        else:
            _list(services)


@emote.command(name="list")
@click.option("--search", default="", help="Search catalog names and identifiers.")
@click.pass_context
def list_packs(ctx, search):
    """List installed catalog packs without granting ownership."""
    _list(_services(ctx), search)


@emote.command()
@click.argument("query", required=False, default="")
@click.pass_context
def browse(ctx, query):
    """Search the local catalog (no network or purchases)."""
    _list(_services(ctx), query)


@emote.command()
@click.argument("pack_id")
@click.option("--slot", type=click.Choice(["companion", "completion"]), default="companion", show_default=True)
@click.pass_context
def equip(ctx, pack_id, slot):
    """Save a preference only after catalog and entitlement validation."""
    services = _services(ctx)
    try:
        if services.remote_factory:
            library = services.remote_factory(services.selection)
            try:
                asyncio.run(library.equip(pack_id) if slot == "companion" else library.equip(pack_id, slot=slot))
            finally:
                library.close()
        else:
            if slot == "companion":
                services.selection.equip(
                    pack_id, catalog=services.catalog, repository=services.repository
                )
            else:
                slots, revision = services.selection.snapshot()
                pack = _load(services, pack_id)
                if "completion" not in pack.manifest.tags:
                    raise PackError("Choose a completion pack for this slot")
                slots["completion"] = pack_id
                if services.selection.compare_and_write_slots(
                    slots, expected_revision=revision
                ) is None:
                    raise PackError("Local emote selection changed; retry if intended")
    except (OSError, ValueError) as exc:
        _fail(exc)
    click.echo(f"Equipped {pack_id}. Ownership remains server-controlled.")


@emote.command()
@click.pass_context
def sync(ctx):
    """Restore owned packs and both equipment slots. Never purchases."""
    services = _services(ctx)
    library = None
    try:
        library = (services.remote_factory or remote_library)(services.selection)
        report = asyncio.run(library.sync())
        count = len(report["available"])
        click.echo(f"Restored {count} owned pack(s). No purchase or top-up was made.")
        if report["selected"]:
            click.echo(
                f"Companion: {report['selected']}. In chat, /emote shows the watcher command."
            )
        else:
            click.echo(
                "No companion equipped. Use openvegas emote equip PACK_ID after choosing an owned companion."
            )
        for pack_id in report["available"]:
            click.echo(f"  {pack_id}")
        if report.get("selected_slots", {}).get("completion"):
            click.echo(f"Completion: {report['selected_slots']['completion']}")
        click.echo("Private packs require online verification; public previews work offline.")
    except (OSError, ValueError) as exc:
        _fail(exc)
    finally:
        if library is not None:
            library.close()


emote.add_command(sync, name="restore")


@emote.command()
@click.pass_context
def owned(ctx):
    """Check the signed-in account's emote library without buying anything."""
    services = _services(ctx)
    library = None
    try:
        library = (services.remote_factory or remote_library)(services.selection)
        report = asyncio.run(library.refresh())
        rows = report["owned"]["entitlements"]
        if not rows:
            click.echo("No owned emotes yet. Public previews: openvegas emote list")
        for row in rows:
            # Validate display identifiers rather than printing arbitrary server text.
            from .manifest import safe_token

            pack_id = safe_token(row.get("pack_id"))
            active = row.get("effective_status") == "active" and row.get("activatable") is True
            click.echo(f"{pack_id}  [{'available' if active else 'activation unavailable'}]")
    except (OSError, ValueError) as exc:
        _fail(exc)
    finally:
        if library is not None:
            library.close()


@emote.command()
@click.pass_context
def off(ctx):
    """Disable the saved selection; running consumers must observe this preference."""
    try:
        _services(ctx).selection.disable()
    except (OSError, ValueError) as exc:
        _fail(exc)
    click.echo("Emotes off.")


def _console() -> Console:
    return Console(
        file=sys.stdout,
        force_terminal=False if not sys.stdout.isatty() else None,
        no_color="NO_COLOR" in os.environ,
    )


def _animated(console: Console) -> bool:
    return motion_allowed(is_tty=console.is_terminal) and console.color_system == "truecolor"


def _render(console, frame):
    return rich_frame(
        fit_frame(frame, max_columns=console.width, max_rows=max(0, console.height - 4))
    )


@emote.command()
@click.argument("pack_id")
@click.option("--reduced-motion", is_flag=True, help="Static preview; never repaint.")
@click.pass_context
def preview(ctx, pack_id, reduced_motion):
    """Preview here. Launch in a separate terminal while another CLI is active."""
    services = _services(ctx)
    try:
        pack = _load(services, pack_id, preview=True)
        console = _console()
        if reduced_motion or not _animated(console):
            click.echo(
                f"{pack_id}: static preview ({pack.manifest.width}x{pack.manifest.height}); no animation."
            )
            if console.is_terminal and "NO_COLOR" not in os.environ:
                console.print(_render(console, pack.frame(pack.manifest.reduced_motion_frame)))
            return
        controller = EmoteController(pack, source="preview", session_id="preview")
        controller.handle(Event("preview", "preview", "preview", "start", Phase.START, 0, 0))
        controller.handle(
            Event("preview", "preview", "preview", "done", Phase.COMPLETE, 0, 1, "success")
        )
        click.echo("Local visual preview only; this does not equip or grant ownership.")
        try:
            with Live(
                _render(console, controller.current_frame()),
                console=console,
                auto_refresh=False,
                transient=True,
                screen=False,
            ) as live:
                while controller.current_state == "complete":
                    live.update(_render(console, controller.current_frame()), refresh=True)
                    time.sleep(1 / 30)
        finally:
            controller.close()
    except (OSError, ValueError) as exc:
        _fail(exc)


@emote.command()
@click.option(
    "--source",
    default="openvegas",
    show_default=True,
    help="Exact metadata source to listen to.",
)
@click.option(
    "--session",
    "session_id",
    required=True,
    help="Exact opaque session ID; never a transcript.",
)
@click.option(
    "--pack",
    "pack_id",
    help="Explicit catalog preview in this owned companion terminal.",
)
@click.option("--completion-pack", "completion_pack_id", help="Explicit public completion preview, played once after success.")
@click.option("--reduced-motion", is_flag=True)
@click.pass_context
def watch(ctx, source, session_id, pack_id, reduced_motion, completion_pack_id=None):
    """Listen in a user-launched SECOND terminal. Ctrl+C closes only this renderer.

    No automatic hook install, no external TTY access, no universal overlay.
    Startup discards queued events. Only the next turn started after readiness is
    eligible; already-running turns and completion-only events fail closed.
    """
    if not IDENTITY.fullmatch(source) or not IDENTITY.fullmatch(session_id):
        raise click.BadParameter("source/session must be bounded opaque ASCII tokens")
    services = _services(ctx)
    console = _console()
    if not sys.stdin.isatty() or not console.is_terminal:
        click.echo("Companion disabled: watch requires its own interactive terminal.")
        return
    library = None
    lease = None
    try:
        preview_mode = bool(pack_id or completion_pack_id)
        if preview_mode:
            slots = {"companion": None, "completion": None}
            preference_revision = services.selection.revision()
        else:
            slots, preference_revision = services.selection.snapshot()
        selected, completion_selected = slots["companion"], slots["completion"]
        if any(slots.values()) and services.remote_factory:
            library = services.remote_factory(services.selection)
            asyncio.run(library.sync())
            slots, preference_revision = services.selection.snapshot()
            selected, completion_selected = slots["companion"], slots["completion"]
            if not any(slots.values()):
                raise PackError("No active equipped emote. Check openvegas emote owned")
            services.catalog = library.catalog(services.catalog)
            services.repository = library.repository(services.repository)
        if pack_id is None and selected is None:
            public = sorted(
                entry.pack_id
                for entry in services.catalog.entries.values()
                if entry.access in {"preview_only", "free"}
                and "completion" not in services.repository.load(entry.resource_name).manifest.tags
            )
            if public:
                pack_id = public[0]
                click.echo(
                    f"Public companion preview: {pack_id}. Preview is not ownership or art approval."
                )
        pack = (
            _load(services, pack_id, preview=True)
            if pack_id
            else _load(services, selected) if selected else None
        )
        if pack is None:
            raise PackError("No equipped pack. Use --pack for an explicit catalog preview.")
        completion_id = completion_pack_id or completion_selected
        completion_pack = _load(services, completion_id, preview=bool(completion_pack_id)) if completion_id else None
        if completion_pack is not None and "completion" not in completion_pack.manifest.tags:
            raise PackError("Choose a completion pack for --completion-pack")
        if services.selection.revision() != preference_revision:
            raise PackError("Local emote selection changed; restart watch if intended")
        if reduced_motion or not _animated(console):
            click.echo(
                "Static companion only: reduced motion, NO_COLOR, or terminal lacks truecolor."
            )
            if "NO_COLOR" not in os.environ:
                console.print(_render(console, pack.frame(pack.manifest.reduced_motion_frame)))
            return
        watermark = _watch_boundary(services.spool, source, session_id)
        controller = EmoteController(pack, source=source, session_id=session_id, completion_pack=completion_pack)
        if library is not None:
            lease = LeaseRefresher(library).start()
        click.echo(
            "Owned companion terminal ready for the next turn; prior events discarded. "
            "Keep the coding CLI in its original terminal. Ctrl+C exits."
        )
        checked = time.monotonic()
        try:
            if services.selection.revision() != preference_revision:
                raise PackError("Local emote selection changed; restart watch if intended")
            with Live(
                _render(console, controller.current_frame()),
                console=console,
                auto_refresh=False,
                transient=True,
                screen=False,
            ) as live:
                while True:
                    if services.selection.revision() != preference_revision:
                        break
                    for event in services.spool.drain(source=source, session_id=session_id):
                        if event.generation > watermark:
                            controller.handle(event)
                    controller.tick()
                    if controller.current_state == "off":
                        break
                    if time.monotonic() - checked >= 0.1:
                        # Preference changes stop this owner. Restart explicitly for a new pack.
                        if services.selection.revision() != preference_revision:
                            break
                        services.catalog.resource_for(pack_id or selected, preview=bool(pack_id))
                        if completion_id:
                            services.catalog.resource_for(completion_id, preview=bool(completion_pack_id))
                        checked = time.monotonic()
                    signature = (
                        controller.current_state,
                        controller.frame_index,
                        console.size,
                    )
                    if signature != getattr(controller, "_watch_signature", None):
                        if services.selection.revision() != preference_revision:
                            break
                        live.update(_render(console, controller.current_frame()), refresh=True)
                        controller._watch_signature = signature
                    time.sleep(1 / 30)
        finally:
            controller.close()
    except KeyboardInterrupt:
        return
    except (OSError, ValueError) as exc:
        _fail(exc)
    finally:
        if lease is not None:
            lease.close()
        elif library is not None:
            library.close()


def _watch_boundary(spool, source, session_id):
    """Discard pre-readiness events atomically, retaining their generation ceiling.

    Do not use drain here: its best-effort empty result hides lock/read failures.
    Publishers must use increasing generations and never replay dropped events.
    """
    watermark = -1
    with _locked(spool.directory) as fd:
        discard = []
        for name in _names(fd):
            if not EVENT_FILE.fullmatch(name):
                continue
            info = state_stat(fd, name)
            event = Event.from_bytes(_read(fd, name, MAX_EVENT_BYTES))
            if event.source == source and event.session_id == session_id:
                watermark = max(watermark, event.generation)
                discard.append((name, info))
        for name, info in discard:
            state_unlink(fd, name, expected=info)
    return watermark


emote.add_command(watch, name="listen")


@emote.command(context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
@click.option(
    "--session",
    "session_id",
    required=True,
    help="Match a separately launched watcher; use a dedicated session.",
)
@click.option("--source", default="openvegas", show_default=True)
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=True)
@click.pass_context
def run(ctx, session_id, source, command):
    """Run COMMAND with inherited terminal streams; no shell or rendering.

    Example: emote run --session X -- python script.py
    Start emote watch --session X in a SECOND terminal first. One zero process
    exit means one successful process completion, not internal coding turns.
    """
    from .runner import run_command

    services = ctx.find_object(EmoteServices)
    try:
        code = run_command(
            command,
            session_id=session_id,
            source=source,
            spool=services.spool if services else None,
        )
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc
    ctx.exit(code if code >= 0 else 128 - code)


def _probe_private_state(path):
    """Probe only our random temporary file; never consume queued events/preferences."""
    name = "." + uuid4().hex + ".tmp"
    data = b"openvegas-private-state-probe-v1"
    with _locked(path) as fd:
        atomic_write(fd, name, data)
        info = state_stat(fd, name)
        try:
            if _read(fd, name, len(data)) != data:
                raise SpoolError("Private state probe failed")
        finally:
            state_unlink(fd, name, expected=info)


@emote.command()
@click.pass_context
def doctor(ctx):
    """Local diagnostics only; no secrets, network calls, or CLI-version claims."""
    services = _services(ctx)
    # Snapshot resources before the probe creates state directories in custom layouts.
    names = services.repository.names()
    console = _console()
    click.echo(
        f"Terminal: {'owned truecolor candidate' if _animated(console) else 'static/non-TTY fallback'}"
    )
    backend = {"posix": "POSIX descriptor backend", "nt": "Windows ACL/handle backend"}.get(os.name, "unsupported platform")
    state_failures = 0
    for label, path in (("Private event spool", services.spool.directory), ("Private selection", services.selection.directory)):
        try:
            _probe_private_state(path)
            status = "local lock/read/write/delete probe passed"
        except BlockingIOError:
            state_failures += 1
            status = "busy; probe not completed (retry when idle)"
        except (OSError, ValueError):
            state_failures += 1
            status = "unavailable or unsafe; private state probe failed"
        click.echo(f"{label}: {backend}; {status}")
    click.echo("Event queue: bounded to 256 events. Local probes do not certify native terminal UX.")
    if os.name == "nt":
        click.echo("External hooks: Windows automatic hook setup unavailable; use emote run plus a separate emote watch terminal.")
    else:
        click.echo("External hooks: opt-in Claude activity pilot; no native success adapter certified.")
        click.echo("Review settings: emote hooks setup claude (dry-run; no automatic installation).")
    click.echo(
        "Premium access: online verification; emote owned / sync / equip; no offline override."
    )
    click.echo("Rendering: second-terminal companion, not a universal overlay.")
    failures = 0
    for name in names:
        try:
            services.repository.load(name)
            click.echo(f"Pack {name}: valid; art approval not inferred")
        except PackError:
            failures += 1
            click.echo(f"Pack {name}: invalid or missing")
    if failures:
        raise click.ClickException("One or more package resources failed validation")
    if state_failures:
        raise click.ClickException("Private emote state diagnostics failed")


emote.add_command(artist)
emote.add_command(hooks)


if __name__ == "__main__":
    emote()
