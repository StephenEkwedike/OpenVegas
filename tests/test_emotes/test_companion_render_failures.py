import io
from types import SimpleNamespace

import click
import pytest
from rich.console import Console

from openvegas.emotes import commands
from openvegas.emotes.manifest import PackError


@pytest.mark.parametrize("failure", [None, "fit", "convert"])
def test_render_releases_owned_images_without_closing_pack(pack, monkeypatch, failure):
    source = pack.frame(0)
    fitted = []
    original = commands.fit_frame

    def fit(*args, **kwargs):
        if failure == "fit":
            raise RuntimeError("private diagnostic")
        image = original(*args, **kwargs)
        fitted.append(image)
        return image

    def broken_conversion(*args, **kwargs):
        raise RuntimeError("private diagnostic")

    monkeypatch.setattr(commands, "fit_frame", fit)
    if failure == "convert":
        monkeypatch.setattr(commands, "rich_frame", broken_conversion)
    console = SimpleNamespace(width=80, height=24)
    if failure:
        with pytest.raises(PackError, match="Emote renderer unavailable") as error:
            commands._render(console, source)
        assert "private diagnostic" not in str(error.value)
    else:
        assert commands._render(console, source).plain
    for image in [source, *fitted]:
        with pytest.raises(ValueError, match="closed image"):
            image.getpixel((0, 0))
    with pack.frame(0) as fresh:
        assert fresh.getpixel((0, 0)) == (255, 0, 0, 255)


@pytest.mark.parametrize("failure_at", [1, 2])
@pytest.mark.parametrize("failure", ["frame", "fit", "convert"])
def test_watch_render_failure_exits_cleanly(pack, monkeypatch, failure, failure_at):
    monkeypatch.setenv("TERM", "xterm-256color")
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, color_system="truecolor",
                      width=80, height=24)
    controllers = []
    original_controller = commands.EmoteController

    def controller(*args, **kwargs):
        instance = original_controller(*args, **kwargs)
        controllers.append(instance)
        if failure == "frame":
            monkeypatch.setattr(instance, "current_frame", failing(instance.current_frame))
        return instance

    def failing(original):
        calls = 0

        def invoke(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == failure_at:
                raise RuntimeError("private diagnostic")
            return original(*args, **kwargs)
        return invoke

    services = SimpleNamespace(
        selection=SimpleNamespace(revision=lambda: "same"),
        spool=SimpleNamespace(drain=lambda **kw: []),
        catalog=SimpleNamespace(resource_for=lambda *a, **kw: "fixture"),
        remote_factory=None,
    )
    monkeypatch.setattr(commands, "_services", lambda _: services)
    monkeypatch.setattr(commands, "_load", lambda *a, **kw: pack)
    monkeypatch.setattr(commands, "_watch_boundary", lambda *a: -1)
    monkeypatch.setattr(commands, "_console", lambda: console)
    monkeypatch.setattr(commands, "_animated", lambda _: True)
    monkeypatch.setattr(commands.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(commands, "EmoteController", controller)
    if failure != "frame":
        name = "fit_frame" if failure == "fit" else "rich_frame"
        monkeypatch.setattr(commands, name, failing(getattr(commands, name)))

    with (
        click.Context(commands.watch, obj=services),
        pytest.raises(click.ClickException, match="Emote renderer unavailable") as error,
    ):
        commands.watch.callback(source="test", session_id="session",
                                pack_id="fixture.pack", reduced_motion=False)
    assert "private diagnostic" not in str(error.value)
    assert controllers[0]._closed
    assert not console._live_stack
    if failure_at == 2:
        assert "\x1b[?25h" in output.getvalue()
    assert "private diagnostic" not in output.getvalue()
