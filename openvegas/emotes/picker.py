"""Line-oriented chooser. No raw input, background renderer, or process launch."""

from __future__ import annotations

import asyncio

import click

from .manifest import PackError, safe_token
from .online import remote_library


def _owned_choices(report):
    owned = report.get("owned") if isinstance(report, dict) else None
    rows = owned.get("entitlements") if isinstance(owned, dict) else None
    if not isinstance(rows, list) or len(rows) > 256:
        raise PackError("Invalid emote ownership response")
    choices = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PackError("Invalid emote entitlement")
        if row.get("effective_status") != "active" or row.get("activatable") is not True:
            continue
        slot = row.get("slot")
        if slot not in ("companion", "completion"):
            continue
        pack_id = safe_token(row.get("pack_id"))
        if pack_id in choices:
            raise PackError("Ambiguous emote pack ownership")
        choices[pack_id] = slot
    return sorted(choices.items(), key=lambda pair: (pair[1], pair[0]))


def _prompt(choices, label="Choose"):
    # Ordinary canonical line input leaves terminal modes and later input alone.
    return click.prompt(
        label,
        type=click.Choice(choices, case_sensitive=False),
        default="q",
        show_choices=False,
    )


def _previews(ctx, services):
    from .commands import preview

    entries = sorted(
        (
            entry
            for entry in services.catalog.entries.values()
            if entry.access in {"preview_only", "free"} or entry.preview_resource
        ),
        key=lambda entry: entry.pack_id,
    )
    click.echo("Public previews only; previewing does not equip or grant ownership.")
    if not entries:
        click.echo("No public previews installed. Run: openvegas emote doctor")
        return
    for index, entry in enumerate(entries, 1):
        click.echo(f"  {index}. {entry.display_name} ({entry.pack_id})")
    click.echo("  q. Return without previewing")
    choice = _prompt([str(i) for i in range(1, len(entries) + 1)] + ["q"], "Preview")
    if choice != "q":
        ctx.invoke(preview, pack_id=entries[int(choice) - 1].pack_id, reduced_motion=False)


def _setup_help():
    click.echo("Setup guide: nothing installed, equipped, or launched.")
    click.echo("Keep animations in a separate companion pane to preserve your coding terminal.")
    click.echo("OpenVegas chat: /emote shows the exact companion command for that session.")
    click.echo("Check local support: openvegas emote doctor")
    click.echo("Claude activity-only pilot, dry-run first:")
    click.echo("  openvegas emote hooks setup claude --settings /path/to/.claude/settings.local.json")
    click.echo("Gemini session discovery only (no task animation), dry-run first:")
    click.echo("  openvegas emote hooks setup gemini --settings /path/to/.gemini/settings.json")
    click.echo("Codex native hook installation is unsupported; no settings will be changed.")
    click.echo("Whole-process wrapper options: openvegas emote run --help")
    click.echo("The wrapper observes process exit, not each answer inside a running LLM session.")
    click.echo("External-host lifecycle and native UX certification are still pending.")


def choose(ctx, services):
    from .commands import off

    slots, revision = services.selection.snapshot()
    click.echo("Emotes: choose an owned pack, preview locally, or turn emotes off.")
    click.echo("No purchases, hook installs, or companion processes are started here.")
    for slot in ("companion", "completion"):
        click.echo(f"Saved {slot}: {slots[slot] or 'off'} (preference, not ownership)")
    library = None
    try:
        click.echo("Checking owned packs online...")
        try:
            library = (services.remote_factory or remote_library)(services.selection)
            choices = _owned_choices(asyncio.run(library.refresh()))
        except (OSError, ValueError):
            # An unavailable account must not prevent offline previews or local off.
            choices = []
            click.echo(
                "Owned library unavailable. Run: openvegas login, then openvegas emote owned. "
                "Cached packs do not grant offline access."
            )
        else:
            if not choices:
                click.echo("No active owned emotes available to equip.")
        for index, (pack_id, slot) in enumerate(choices, 1):
            click.echo(f"  {index}. Equip {pack_id} [{slot}]")
        click.echo("  p. Public previews (no ownership required)")
        click.echo("  s. Setup guide (read-only)")
        click.echo("  o. Turn both slots off locally")
        click.echo("  q. Exit without changes")
        choice = _prompt([str(i) for i in range(1, len(choices) + 1)] + ["p", "s", "o", "q"])
        if choice == "q":
            return
        if choice == "p":
            _previews(ctx, services)
        elif choice == "s":
            _setup_help()
        elif choice == "o":
            ctx.invoke(off)
        else:
            if services.selection.revision() != revision:
                raise PackError("Local emote selection changed; reopen the chooser if intended")
            pack_id, slot = choices[int(choice) - 1]
            # Recheck ownership at equip time, using the same account-bound client.
            asyncio.run(library.equip(pack_id, slot=slot))
            click.echo(f"Equipped {pack_id} in {slot}. Ownership remains server-controlled.")
            click.echo(
                "No renderer started. In OpenVegas chat, /emote shows the command "
                "to run in a separate companion terminal."
            )
    finally:
        if library is not None:
            library.close()
