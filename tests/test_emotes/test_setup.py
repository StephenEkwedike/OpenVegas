"""Offline setup uses the installed catalog, never account or state operations."""
import shlex
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from openvegas.emotes import commands
from openvegas.emotes.commands import EmoteServices, emote
from openvegas.emotes.resources import Catalog, CatalogEntry, PackRepository, preview_catalog


@pytest.fixture
def services():
    repository = PackRepository()
    forbidden = Mock(side_effect=AssertionError("State/network access forbidden"))
    state = Mock()
    state.snapshot = state.revision = state.equip = state.disable = forbidden
    return EmoteServices(repository, preview_catalog(repository), state, state, forbidden)


def ids(services):
    slots = {"companion": [], "completion": []}
    for entry in services.catalog.entries.values():
        pack = services.repository.load(entry.resource_name)
        slot = "completion" if "completion" in pack.manifest.tags else "companion"
        slots[slot].append(entry.pack_id)
    return min(slots["companion"]), min(slots["completion"])


def args():
    return ["setup", "--source", "openvegas", "--session", "session-1:opaque.uuid"]


def test_script_lists_then_prints_exact_validated_command(services):
    runner = CliRunner()
    companion, completion = ids(services)
    result = runner.invoke(emote, args() + ["--no-interactive"], obj=services)
    assert result.exit_code == 0, result.output
    assert companion in result.output and completion in result.output
    assert "openvegas emote watch --source" not in result.output
    result = runner.invoke(emote, args() + ["--pack", companion, "--completion-pack", completion], obj=services)
    assert result.exit_code == 0, result.output
    line = next(line for line in result.output.splitlines() if line.startswith("openvegas emote watch"))
    assert shlex.split(line) == ["openvegas", "emote", "watch", "--source", "openvegas",
                                 "--session", "session-1:opaque.uuid", "--pack", companion,
                                 "--completion-pack", completion]
    assert "not equipment" in result.output and "NEW turn" in result.output
    services.remote_factory.assert_not_called()
    assert not services.selection.mock_calls


def test_interactive_watch_invokes_existing_watcher_once(services, monkeypatch):
    companion, completion = ids(services)
    monkeypatch.setattr(commands.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(commands.sys.stdout, "isatty", lambda: True)
    # CliRunner replaces streams, so patch its stream types for this terminal-only branch.
    from click.testing import _NamedTextIOWrapper
    monkeypatch.setattr(_NamedTextIOWrapper, "isatty", lambda self: True)
    called = Mock()
    monkeypatch.setattr(commands.watch, "callback", called)
    result = CliRunner().invoke(emote, args() + ["--interactive", "--watch"],
                                input=f"{companion}\n{completion}\n", obj=services)
    assert result.exit_code == 0, result.output
    called.assert_called_once_with(source="openvegas", session_id="session-1:opaque.uuid",
                                   pack_id=companion, completion_pack_id=completion, reduced_motion=False)
    assert not services.selection.mock_calls
    services.remote_factory.assert_not_called()


def test_no_tty_watch_declines_without_state_or_watcher(services, monkeypatch):
    called = Mock()
    monkeypatch.setattr(commands.watch, "callback", called)
    result = CliRunner().invoke(emote, args() + ["--watch"], obj=services)
    assert result.exit_code == 0 and "requires its own interactive terminal" in result.output
    called.assert_not_called()
    assert not services.selection.mock_calls


@pytest.mark.parametrize("bad", ["unknown.pack", "$(touch bad)", "--bad"])
def test_invalid_pack_rejected(services, bad):
    _, completion = ids(services)
    result = CliRunner().invoke(emote, args() + ["--pack", bad, "--completion-pack", completion], obj=services)
    assert result.exit_code != 0
    assert "openvegas emote watch --source" not in result.output


def test_wrong_slots_and_unsafe_session_rejected(services):
    companion, completion = ids(services)
    result = CliRunner().invoke(emote, args() + ["--pack", completion, "--completion-pack", companion], obj=services)
    assert result.exit_code != 0
    result = CliRunner().invoke(emote, ["setup", "--source", "openvegas", "--session", "x; echo unsafe"], obj=services)
    assert result.exit_code != 0 and "bounded opaque" in result.output


def test_premium_without_public_preview_never_authorizes(services):
    companion, _ = ids(services)
    entry = services.catalog.get(companion)
    authorize = Mock(side_effect=AssertionError("Entitlement call forbidden"))
    services.catalog = Catalog([CatalogEntry(entry.pack_id, entry.resource_name, entry.display_name, "premium")],
                               authorize=authorize)
    result = CliRunner().invoke(emote, args() + ["--no-interactive"], obj=services)
    assert result.exit_code != 0 and "No public companion" in result.output
    authorize.assert_not_called()
