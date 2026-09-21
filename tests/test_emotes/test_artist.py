"""Artist CLI validation and bounded local-only rendering; synthetic packs only."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from PIL import Image

import openvegas.emotes.artist as module
from openvegas.emotes.selection import SelectionStore


def invoke(command, path, *options):
    return CliRunner().invoke(module.artist, [command, str(path), *options])


def rewrite(root, change):
    path = root / "manifest.json"
    raw = json.loads(path.read_text())
    change(raw)
    path.write_text(json.dumps(raw))


def test_validate_generic_pack_no_install_or_selection(pack_dir, monkeypatch):
    before = {p.name: p.read_bytes() for p in pack_dir.iterdir()}

    def forbidden(*args, **kwargs):
        pytest.fail("Artist tooling must not write selection or ownership state")

    monkeypatch.setattr(SelectionStore, "write", forbidden)
    for command in ("validate", "preview"):
        result = invoke(command, pack_dir)
        assert result.exit_code == 0, result.output
        assert "2x3" in result.output
        assert "\x1b" not in result.output
    assert "4.800s" in invoke("validate", pack_dir).output
    assert before == {p.name: p.read_bytes() for p in pack_dir.iterdir()}


def test_validate_designer_64x80_default(pack_dir):
    with Image.new("RGBA", (256, 80), (20, 30, 40, 255)) as sheet:
        sheet.save(pack_dir / "sheet.png")
    digest = hashlib.sha256((pack_dir / "sheet.png").read_bytes()).hexdigest()
    rewrite(
        pack_dir,
        lambda raw: raw.update(
            frame={"width": 64, "height": 80, "anchor": [32, 79]},
            sha256=digest,
        ),
    )
    result = invoke("validate", pack_dir)
    assert result.exit_code == 0, result.output
    assert "64x80" in result.output


@pytest.mark.parametrize("command", ["validate", "preview"])
@pytest.mark.parametrize(
    "damage", ["traversal", "hash", "bounds", "loop", "duration", "pricing", "json"]
)
def test_rejects_unsafe_or_malformed_pack(pack_dir, command, damage):
    changes = {
        "traversal": lambda raw: raw.update(sheet="../sheet.png"),
        "hash": lambda raw: raw.update(sha256="0" * 64),
        "bounds": lambda raw: raw.update(reduced_motion_frame=4095),
        "loop": lambda raw: raw["animations"]["complete"].update(loop=True),
        "duration": lambda raw: raw["animations"]["complete"].update(frame_ms=3000),
        "pricing": lambda raw: raw.update(cost_v=0, owned=True),
    }
    if damage == "json":
        (pack_dir / "manifest.json").write_text('{"schema_version":')
    else:
        rewrite(pack_dir, changes[damage])
    result = invoke(command, pack_dir)
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("target", ["root", "manifest.json", "sheet.png"])
def test_rejects_symlink_pack_resources(pack_dir, tmp_path, target):
    root = pack_dir
    if target == "root":
        root = tmp_path / "link"
        root.symlink_to(pack_dir, target_is_directory=True)
    else:
        file = pack_dir / target
        outside = tmp_path / target
        file.rename(outside)
        file.symlink_to(outside)
    assert invoke("validate", root).exit_code == 1
    assert invoke("preview", root).exit_code == 1


@pytest.mark.parametrize("kind", ["missing", "empty", "file"])
def test_rejects_missing_empty_or_file_paths(tmp_path, kind):
    path = tmp_path / "input"
    if kind == "empty":
        path.mkdir()
    elif kind == "file":
        path.write_text("not a folder")
    assert invoke("validate", path).exit_code == 1
    assert invoke("preview", path).exit_code == 1


@pytest.fixture
def rendering(monkeypatch):
    state = SimpleNamespace(now=0.0, lives=[], frames=[], sleeps=[], printed=[])
    console = SimpleNamespace(
        is_terminal=True, color_system="truecolor", width=80, height=24, print=state.printed.append
    )
    monkeypatch.setattr(module, "_console", lambda: console)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("OPENVEGAS_REDUCED_MOTION", "0")
    monkeypatch.setenv("TERM", "xterm-256color")

    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: state.now, sleep=sleep))

    class FakeLive:
        def __init__(self, frame, **kwargs):
            self.kwargs = kwargs
            self.exited = False
            state.lives.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.exited = True

        def update(self, frame, *, refresh):
            assert refresh
            state.frames.append(frame)

    monkeypatch.setattr(module, "Live", FakeLive)
    state.console = console
    return state


def test_preview_one_transient_owner_bounded_complete(pack_dir, rendering):
    result = invoke("preview", pack_dir)
    assert result.exit_code == 0, result.output
    assert len(rendering.lives) == 1
    live = rendering.lives[0]
    assert live.exited
    assert live.kwargs["transient"] is True
    assert live.kwargs["auto_refresh"] is False
    assert live.kwargs["screen"] is False
    assert 4 <= rendering.now <= 7
    assert len(rendering.frames) == 3
    assert len(rendering.sleeps) <= 210
    assert not module._RENDER_OWNER.locked()


@pytest.mark.parametrize(
    "mode", ["no-color", "reduced-env", "reduced-option", "no-animate", "pipe", "dumb"]
)
def test_static_modes_never_start_live(pack_dir, rendering, monkeypatch, mode):
    options = []
    if mode == "no-color":
        monkeypatch.setenv("NO_COLOR", "")
    elif mode == "reduced-env":
        monkeypatch.setenv("OPENVEGAS_REDUCED_MOTION", "true")
    elif mode == "reduced-option":
        options = ["--reduced-motion"]
    elif mode == "no-animate":
        options = ["--no-animate"]
    elif mode == "pipe":
        rendering.console.is_terminal = False
    else:
        monkeypatch.setenv("TERM", "dumb")
    result = invoke("preview", pack_dir, *options)
    assert result.exit_code == 0, result.output
    assert "no animation" in result.output
    assert not rendering.lives and not rendering.sleeps
    if mode in {"no-color", "pipe"}:
        assert not rendering.printed


def test_ctrl_c_cleans_live_and_releases_owner(pack_dir, rendering, monkeypatch):
    def interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(module.time, "sleep", interrupt)
    result = invoke("preview", pack_dir)
    assert result.exit_code == 1
    assert rendering.lives[0].exited
    assert not module._RENDER_OWNER.locked()


@pytest.mark.parametrize("option", [[], ["--reduced-motion"]])
def test_existing_owner_rejected(pack_dir, rendering, option):
    module._RENDER_OWNER.acquire()
    try:
        result = invoke("preview", pack_dir, *option)
        assert result.exit_code == 1
        assert "already owns" in result.output
        assert not rendering.lives
    finally:
        module._RENDER_OWNER.release()


def test_stalled_clock_has_iteration_bound(pack_dir, rendering, monkeypatch):
    monkeypatch.setattr(module.time, "sleep", rendering.sleeps.append)
    assert invoke("preview", pack_dir).exit_code == 0
    assert len(rendering.sleeps) == 210
    assert rendering.lives[0].exited


def test_command_group_has_no_install_upload_equip_or_ownership_actions():
    assert set(module.artist.commands) == {"validate", "preview"}


def test_renderer_failure_cleans_up(pack_dir, rendering, monkeypatch):
    original = module._render
    calls = 0

    def fail_after_live_starts(*args):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ValueError("synthetic render failure")
        return original(*args)

    monkeypatch.setattr(module, "_render", fail_after_live_starts)
    result = invoke("preview", pack_dir)
    assert result.exit_code == 1
    assert "Artist preview unavailable" in result.output
    assert rendering.lives[0].exited
    assert not module._RENDER_OWNER.locked()
