"""Synthetic ownership only; no login, network, or external processes."""

import sys

import click
import pytest
from click.testing import CliRunner

from openvegas.emotes import commands
from openvegas.emotes.manifest import PackError
from openvegas.emotes.picker import _owned_choices
from openvegas.emotes.resources import Catalog, CatalogEntry, PackRepository
from openvegas.emotes.selection import SelectionStore
from openvegas.emotes.spool import EventSpool


def row(pack_id="fixture.companion", slot="companion", **kwargs):
    return dict(pack_id=pack_id, slot=slot, effective_status="active", activatable=True, **kwargs)


class Library:
    def __init__(self, selection):
        self.selection = selection
        self.rows = [row("fixture.scene", "completion"), row()]
        self.calls = []
        self.closed = 0

    async def refresh(self):
        self.calls.append("refresh")
        return {"owned": {"entitlements": self.rows}}

    async def equip(self, pack_id, *, slot):
        self.calls.append(("equip", pack_id, slot))
        slots = self.selection.read_slots()
        slots[slot] = pack_id
        self.selection.write_slots(slots)

    def close(self):
        self.closed += 1


@pytest.fixture
def picker_services(tmp_path):
    selection = SelectionStore(tmp_path / "selection")
    library = Library(selection)
    services = commands.EmoteServices(
        PackRepository(tmp_path / "empty"),
        Catalog([CatalogEntry("fixture.preview", "preview", "Public preview")]),
        selection,
        EventSpool(tmp_path / "events"),
        lambda _: library,
    )
    return services, library


def invoke(monkeypatch, services, text="", *, stdin_tty=True, stdout_tty=True, args=None):
    @click.command()
    @click.pass_context
    def entry(ctx):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: stdin_tty)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: stdout_tty)
        if args is None:
            ctx.invoke(commands.emote)
        else:
            commands.emote.main(args=args, obj=services, standalone_mode=False)

    return CliRunner().invoke(entry, obj=services, input=text)


@pytest.mark.parametrize("stdin_tty,stdout_tty", [(False, False), (False, True), (True, False)])
def test_redirected_bare_command_is_local_deterministic_list(
    monkeypatch, picker_services, stdin_tty, stdout_tty
):
    services, library = picker_services
    result = invoke(monkeypatch, services, "1\n", stdin_tty=stdin_tty, stdout_tty=stdout_tty)
    assert result.exit_code == 0, result.output
    assert result.output == "fixture.preview  Public preview  [preview_only]\n"
    assert library.calls == [] and library.closed == 0
    assert not services.selection.directory.exists()


@pytest.mark.parametrize(
    "choice,expected,slot",
    [("1", "fixture.companion", "companion"), ("2", "fixture.scene", "completion")],
)
def test_tty_equips_owned_choice_into_its_slot(
    monkeypatch, picker_services, choice, expected, slot
):
    services, library = picker_services
    result = invoke(monkeypatch, services, choice + "\n")
    assert result.exit_code == 0, result.output
    assert library.calls == ["refresh", ("equip", expected, slot)]
    assert library.closed == 1
    assert services.selection.read_slots()[slot] == expected
    assert "No renderer started" in result.output


@pytest.mark.parametrize("text", ["q\n", "Q\n", "\n", "invalid\nq\n", "0\nq\n"])
def test_exit_default_and_invalid_choices_never_change_selection(
    monkeypatch, picker_services, text
):
    services, library = picker_services
    services.selection.write("fixture.saved")
    revision = services.selection.revision()
    result = invoke(monkeypatch, services, text)
    assert result.exit_code == 0, result.output
    assert services.selection.revision() == revision
    assert library.calls == ["refresh"] and library.closed == 1
    assert "preference, not ownership" in result.output


def test_eof_closes_library_without_equipping(monkeypatch, picker_services):
    services, library = picker_services
    result = invoke(monkeypatch, services)
    assert result.exit_code == 1 and "Aborted" in result.output
    assert library.calls == ["refresh"] and library.closed == 1
    assert services.selection.read() is None


@pytest.mark.parametrize(
    "failure", [PackError("synthetic account failure"), OSError("synthetic offline")]
)
def test_offline_still_allows_both_slots_off(monkeypatch, picker_services, failure):
    services, library = picker_services
    services.selection.write_slots({"companion": "fixture.c", "completion": "fixture.s"})

    async def fail():
        raise failure

    monkeypatch.setattr(library, "refresh", fail)
    result = invoke(monkeypatch, services, "o\n")
    assert result.exit_code == 0, result.output
    assert "Owned library unavailable" in result.output and "Emotes off" in result.output
    assert "synthetic" not in result.output
    assert services.selection.read_slots() == {"companion": None, "completion": None}
    assert library.closed == 1


def test_preview_is_explicit_and_not_an_equip(monkeypatch, picker_services):
    services, library = picker_services
    seen = []
    monkeypatch.setattr(commands.preview, "callback", lambda **kwargs: seen.append(kwargs))
    result = invoke(monkeypatch, services, "p\n1\n")
    assert result.exit_code == 0, result.output
    assert seen == [{"pack_id": "fixture.preview", "reduced_motion": False}]
    assert library.calls == ["refresh"] and library.closed == 1
    assert services.selection.read() is None


@pytest.mark.parametrize("offline", [False, True])
def test_setup_guide_is_informational_and_never_installs_or_launches(
    monkeypatch, picker_services, offline
):
    import subprocess

    from openvegas.emotes import hooks

    services, library = picker_services
    services.selection.write("fixture.saved")
    before = services.selection.revision()
    if offline:
        async def unavailable():
            library.calls.append("refresh")
            raise OSError("synthetic offline")
        monkeypatch.setattr(library, "refresh", unavailable)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("process launched"))
    monkeypatch.setattr(hooks, "setup", lambda *a, **k: pytest.fail("hook settings touched"))
    monkeypatch.setattr(commands, "Live", lambda *a, **k: pytest.fail("renderer started"))
    result = invoke(monkeypatch, services, "s\n")
    assert result.exit_code == 0, result.output
    assert "s. Setup guide (read-only)" in result.output
    assert "nothing installed, equipped, or launched" in result.output
    assert "session discovery only (no task animation)" in result.output
    assert "Codex native hook installation is unsupported" in result.output
    assert "not each answer" in result.output
    assert "certification are still pending" in result.output
    assert "--apply" not in result.output
    assert library.calls == ["refresh"] and library.closed == 1
    assert services.selection.revision() == before


def test_stale_menu_cannot_override_concurrent_off(monkeypatch, picker_services):
    services, library = picker_services

    def changed(*args, **kwargs):
        services.selection.disable()
        return "1"

    monkeypatch.setattr(click, "prompt", changed)
    result = invoke(monkeypatch, services)
    assert result.exit_code == 1, result.output
    assert "selection changed" in result.output
    assert library.calls == ["refresh"] and library.closed == 1


def test_revocation_during_choice_fails_closed(monkeypatch, picker_services):
    services, library = picker_services

    async def revoked(*args, **kwargs):
        raise PackError("Verified active ownership required for this emote")

    monkeypatch.setattr(library, "equip", revoked)
    result = invoke(monkeypatch, services, "1\n")
    assert result.exit_code == 1
    assert "Equipped" not in result.output and services.selection.read() is None
    assert library.closed == 1


def test_inactive_and_non_terminal_entitlements_not_offered(monkeypatch, picker_services):
    services, library = picker_services
    library.rows = [dict(row(), activatable=False), row("fixture.theme", "theme")]
    result = invoke(monkeypatch, services, "q\n")
    assert result.exit_code == 0
    assert "No active owned emotes" in result.output and "1. Equip" not in result.output


@pytest.mark.parametrize("rows", [[row("\x1b[2J")], [row(), row()], [None], [row()] * 257])
def test_invalid_ownership_never_becomes_choice(rows):
    with pytest.raises(PackError):
        _owned_choices({"owned": {"entitlements": rows}})


def test_no_color_menu_stays_plain_and_never_launches_renderer(monkeypatch, picker_services):
    import subprocess

    services, _ = picker_services
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("process launched"))
    monkeypatch.setattr(commands, "Live", lambda *a, **k: pytest.fail("renderer started"))
    result = invoke(monkeypatch, services, "1\n")
    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output


@pytest.mark.parametrize(
    "args", [["owned"], ["off"], ["preview", "fixture.preview", "--reduced-motion"]]
)
def test_explicit_flows_bypass_picker_even_on_tty(monkeypatch, picker_services, args):
    from types import SimpleNamespace

    from openvegas.emotes import picker

    services, library = picker_services
    monkeypatch.setattr(picker, "choose", lambda *a: pytest.fail("unexpected chooser"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(
        commands,
        "_load",
        lambda *a, **k: SimpleNamespace(manifest=SimpleNamespace(width=2, height=2)),
    )
    result = invoke(monkeypatch, services, args=args)
    assert result.exit_code == 0, result.output
    assert library.calls == (["refresh"] if args == ["owned"] else [])
    assert "Choose" not in result.output
