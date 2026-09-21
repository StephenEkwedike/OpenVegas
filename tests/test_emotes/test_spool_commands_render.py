import json
import os
import stat

import pytest
from click.testing import CliRunner
from PIL import Image

from openvegas.emotes import Event, EventSpool, Phase, publish_event
from openvegas.emotes.adapters import adapt_hook, publish_hook
from openvegas.emotes.commands import EmoteServices, emote
from openvegas.emotes.manifest import PackError
from openvegas.emotes.render import (
    fit_frame,
    motion_allowed,
    prompt_toolkit_fragments,
    rich_frame,
)
from openvegas.emotes.resources import (
    Catalog,
    CatalogEntry,
    PackRepository,
    preview_catalog,
)
from openvegas.emotes.selection import SelectionStore
from openvegas.emotes.spool import MAX_QUEUE, _locked


def event(seq=0, *, session="s", phase=Phase.START):
    return Event(
        "openvegas",
        session,
        "turn",
        f"e-{session}-{seq}",
        phase,
        0,
        seq,
        "success" if phase == Phase.COMPLETE else None,
    )


def test_event_strict_and_content_free():
    e = event()
    assert Event.from_bytes(e.to_bytes()) == e
    for fields in (
        {"prompt": "secret"},
        {"schema_version": True},
        {"sequence": -1},
        {"session_id": "\x1b[2J"},
        {"phase": "tool_result"},
        {"phase": "turn_completed", "outcome": "failed"},
    ):
        raw = json.loads(e.to_bytes())
        raw.update(fields)
        with pytest.raises(ValueError):
            Event.from_bytes(json.dumps(raw).encode())
    with pytest.raises(ValueError):
        Event.from_bytes(b"x" * 4097)


def test_spool_private_bounded_order_filter(tmp_path, capsys):
    spool = EventSpool(tmp_path / "events")
    assert spool.publish(event(2, phase=Phase.COMPLETE))
    assert spool.publish(event(0))
    assert spool.publish(event(1, session="other"))
    assert stat.S_IMODE(spool.directory.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(p.stat().st_mode) == 0o600 for p in spool.directory.iterdir()
    )
    assert [e.sequence for e in spool.drain(source="openvegas", session_id="s")] == [
        0,
        2,
    ]
    assert len(spool.drain(source="openvegas", session_id="other")) == 1
    assert spool.drain(source="openvegas", session_id="s") == []
    for i in range(MAX_QUEUE):
        assert spool.publish(event(i))
    assert not spool.publish(event(500))
    assert capsys.readouterr() == ("", "")


def test_spool_missing_busy_or_unsafe_fails_open(tmp_path, capsys):
    path = tmp_path / "events"
    with _locked(path):
        assert not publish_event(event(), directory=path)
    path.chmod(0o755)
    assert not publish_event(event(), directory=path)
    path.chmod(0o700)
    link = tmp_path / "symlink"
    link.symlink_to(path, target_is_directory=True)
    assert not publish_event(event(), directory=link)
    assert not publish_event({"prompt": "no"}, directory=path)
    assert capsys.readouterr() == ("", "")


def test_spool_rejects_symlink_hardlink_large_malformed(tmp_path):
    spool = EventSpool(tmp_path / "events")
    assert spool.publish(event())
    spool.drain(source="openvegas", session_id="s")
    target = tmp_path / "private"
    target.write_text("secret")
    target.chmod(0o600)
    (spool.directory / ("a" * 32 + ".json")).symlink_to(target)
    os.link(target, spool.directory / ("b" * 32 + ".json"))
    huge = spool.directory / ("c" * 32 + ".json")
    huge.write_bytes(b"x" * 4097)
    huge.chmod(0o600)
    assert spool.drain(source="openvegas", session_id="s") == []
    assert target.read_text() == "secret"


@pytest.mark.parametrize(
    "provider,payload,expected",
    [
        ("claude", {"hook_event_name": "UserPromptSubmit"}, Phase.START),
        ("claude", {"hook_event_name": "Stop"}, None),
        ("claude", {"hook_event_name": "PostToolUse"}, None),
        ("gemini", {"hook_event_name": "BeforeAgent"}, Phase.START),
        ("gemini", {"hook_event_name": "AfterAgent"}, None),
        ("codex", {"type": "agent-turn-complete"}, None),
        ("unknown", {}, None),
    ],
)
def test_external_passive_adapters(provider, payload, expected):
    payload["prompt"] = "never persisted"
    result = adapt_hook(
        provider,
        payload,
        session_id="s",
        turn_id="t",
        generation=1,
        sequence=1,
        event_id="e",
    )
    assert (result.phase if result else None) == expected
    if result:
        assert b"persisted" not in result.to_bytes()


def test_adapters_no_native_success_guess_and_failopen(tmp_path, capsys):
    identity = {
        "session_id": "s",
        "turn_id": "t",
        "generation": 1,
        "sequence": 1,
        "event_id": "e",
        "authoritative_success": True,
    }
    assert (
        adapt_hook(
            "codex", {"type": "agent-turn-complete", "thread-id": "foreign"}, **identity
        )
        is None
    )
    assert (
        adapt_hook(
            "claude", {"hook_event_name": "Stop", "stop_hook_active": True}, **identity
        )
        is None
    )
    assert (
        adapt_hook("codex", {"type": "agent-turn-complete"}, **identity).phase
        == Phase.COMPLETE
    )
    assert publish_hook(
        "codex",
        {"type": "agent-turn-complete"},
        directory=tmp_path / "events",
        **identity,
    )
    assert not publish_hook("claude", {}, nonsense=True)
    assert capsys.readouterr() == ("", "")


def test_render_odd_alpha_blank_and_bounds(pack):
    assert rich_frame(pack.frame(0)).plain.count("\n") == 1
    assert rich_frame(pack.frame(3)).plain == "  \n  "
    assert "#ff0000" in prompt_toolkit_fragments(pack.frame(0))[0][0]
    assert (
        "#7f7fff"
        in prompt_toolkit_fragments(pack.frame(2), background=(255, 255, 255))[0][0]
    )
    assert prompt_toolkit_fragments(None) == []
    assert rich_frame(None).plain == ""
    with pytest.raises(ValueError):
        rich_frame(pack.frame(0), scale=0)
    for env in (
        {"NO_COLOR": ""},
        {"TERM": "dumb"},
        {"OPENVEGAS_REDUCED_MOTION": "true"},
    ):
        assert not motion_allowed(is_tty=True, environ=env)
    assert not motion_allowed(is_tty=False, environ={})


@pytest.mark.parametrize("cols,rows", [(80, 20), (20, 10), (1, 1), (6, 2)])
def test_autofit_native_64x80(cols, rows):
    native = Image.new("RGBA", (64, 80))
    fitted = fit_frame(native, max_columns=cols, max_rows=rows)
    assert fitted.width <= cols and fitted.height <= rows * 2
    assert native.size == (64, 80)
    if (cols, rows) == (80, 20):
        assert fitted.size == (32, 40)
    assert fit_frame(native, max_columns=0, max_rows=rows) is None


@pytest.fixture
def services(pack_dir, tmp_path):
    repo = PackRepository(pack_dir.parent)
    return EmoteServices(
        repo,
        Catalog([CatalogEntry("fixture.pack", "fixture", "Fixture", "free")]),
        SelectionStore(tmp_path / "state"),
        EventSpool(tmp_path / "events"),
    )


def test_click_list_browse_preview_equip_off_doctor(services):
    runner = CliRunner()
    for args in (
        [],
        ["list"],
        ["browse", "Fixture"],
        ["preview", "fixture.pack"],
        ["doctor"],
    ):
        result = runner.invoke(emote, args, obj=services)
        assert result.exit_code == 0, result.output
        assert "\x1b" not in result.output
    result = runner.invoke(emote, ["equip", "fixture.pack"], obj=services)
    assert result.exit_code == 0, result.output
    assert services.selection.read() == "fixture.pack"
    assert services.selection.load_selected(
        catalog=services.catalog, repository=services.repository
    )
    assert runner.invoke(emote, ["off"], obj=services).exit_code == 0
    assert services.selection.read() is None
    result = runner.invoke(
        emote, ["watch", "--source", "openvegas", "--session", "s"], obj=services
    )
    assert result.exit_code == 0
    assert "interactive terminal" in result.output


def test_pending_and_premium_fail_closed_revalidation(services, monkeypatch):
    services.catalog = preview_catalog(services.repository)
    runner = CliRunner()
    result = runner.invoke(emote, ["equip", "fixture.pack"], obj=services)
    assert result.exit_code == 1
    assert "This pack is preview-only; release verification is still pending" in result.output
    assert "Art approval pending" not in result.output
    assert services.selection.read() is None
    assert (
        runner.invoke(emote, ["preview", "fixture.pack"], obj=services).exit_code == 0
    )
    entry = CatalogEntry("fixture.pack", "fixture", "Premium", "premium")
    services.catalog = Catalog([entry])
    monkeypatch.setenv("OPENVEGAS_EMOTE_ADMIN", "1")
    monkeypatch.setenv("OPENVEGAS_EMOTE_PREVIEW", "1")
    assert runner.invoke(emote, ["equip", "fixture.pack"], obj=services).exit_code == 1
    assert (
        runner.invoke(emote, ["preview", "fixture.pack"], obj=services).exit_code == 1
    )
    services.catalog.authorize = lambda _: True
    assert runner.invoke(emote, ["equip", "fixture.pack"], obj=services).exit_code == 0
    services.catalog.authorize = lambda _: False
    with pytest.raises(PackError):
        services.selection.load_selected(
            catalog=services.catalog, repository=services.repository
        )
    assert (
        services.selection.read() == "fixture.pack"
    )  # Revocation doesn't destroy preference.


def test_premium_public_preview_resource_is_separate(services):
    services.catalog = Catalog(
        [CatalogEntry("premium.pack", "protected", "Premium", "premium", "fixture")]
    )
    runner = CliRunner()
    assert (
        runner.invoke(emote, ["preview", "premium.pack"], obj=services).exit_code == 0
    )
    assert runner.invoke(emote, ["equip", "premium.pack"], obj=services).exit_code == 1


def test_selection_tamper_cannot_grant_access(services):
    services.selection.write("fixture.pack")
    services.catalog = Catalog(
        [CatalogEntry("fixture.pack", "fixture", "Premium", "premium")]
    )
    with pytest.raises(PackError):
        services.selection.load_selected(
            catalog=services.catalog, repository=services.repository
        )


def test_repeated_global_off_changes_revision(services):
    assert services.selection.revision() is None
    services.selection.write(None)
    first = services.selection.revision()
    assert first is not None
    services.selection.write(None)
    assert services.selection.revision() != first


def test_watch_session_only_uses_labeled_public_preview(services, monkeypatch, capsys):
    import io
    import sys

    from rich.console import Console

    from openvegas.emotes import commands

    services.catalog = preview_catalog(services.repository)
    output = io.StringIO()
    console = Console(
        file=output, force_terminal=True, color_system="truecolor", width=80, height=24
    )
    monkeypatch.setattr(commands, "_console", lambda: console)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    # CliRunner replaces stdin again, so use its isolated console entry directly.
    context = commands.click.Context(commands.watch, obj=services)
    with context:
        commands.watch.callback(
            session_id="X", source="openvegas", pack_id=None, reduced_motion=True
        )
    assert services.selection.read() is None
    assert "Public companion preview: fixture.pack" in capsys.readouterr().out
