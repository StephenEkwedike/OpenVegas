"""Offline real CLI/compositor wiring and server-authorized private pack tests."""

from __future__ import annotations

import ast
import asyncio
import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from openvegas.emotes.compositor import create_owned_chat
from openvegas.emotes.controller import State
from openvegas.emotes.remote import RemoteLibrary
from openvegas.emotes.selection import SelectionStore


class NoWorkerLease:
    def __init__(self, library):
        self.library = library

    def start(self):
        return self

    def close(self):
        self.library.close()


class ServerFixture:
    backend_scope = "https://offline.invalid"

    def __init__(self, pack_dir):
        self.user = str(uuid4())
        self.denied = False
        self.calls = []
        self.bundle = {
            "schema_version": 1, "item_id": "test-item", "pack_id": "fixture.pack",
            "version": "1.0.0", "manifest": json.loads((pack_dir / "manifest.json").read_text()),
            "sheet_base64": base64.b64encode((pack_dir / "sheet.png").read_bytes()).decode(),
        }

    def identity(self):
        return self.backend_scope, self.user

    async def owned(self):
        self.calls.append("owned")
        return {
            "account_id": self.user,
            "entitlements": [] if self.denied else [{
                "item_id": "test-item", "pack_id": "fixture.pack", "slot": "companion",
                "effective_status": "active", "activatable": True,
                "acquired_version": "1.0.0", "available_version": "1.0.0",
            }],
            "equipped": {"companion": None if self.denied else "test-item"},
        }

    async def pack(self, item_id):
        self.calls.append("pack")
        return self.bundle


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "off"])
async def test_no_equipment_or_disabled_mode_keeps_existing_prompt(tmp_path, mode):
    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=DummyOutput())
        result = await create_owned_chat(
            session, Console(file=io.StringIO()), session_id="session", mode=mode,
            selection=SelectionStore(tmp_path / "selection"),
            library_factory=lambda _: pytest.fail("No emote network expected"),
        )
        assert result is None
        assert not session.app.is_running


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied_selection", [False, True])
async def test_auto_uncertified_surface_does_not_inspect_equipment_or_start_owner(monkeypatch, supplied_selection):
    from openvegas.emotes import compositor
    from openvegas.emotes import selection as selection_module

    def forbidden(*args, **kwargs):
        pytest.fail("auto must not inspect equipment, contact the server, or start an owner")

    monkeypatch.setattr(selection_module, "SelectionStore", forbidden)
    monkeypatch.setattr(compositor, "OwnedChatCompositor", forbidden)
    selection = SimpleNamespace(snapshot=forbidden) if supplied_selection else None
    assert await create_owned_chat(
        None, Console(file=io.StringIO()), session_id="uncertified", mode="auto",
        selection=selection, library_factory=forbidden, lease_factory=forbidden,
    ) is None


@pytest.mark.asyncio
async def test_explicit_empty_owner_does_not_grant_public_or_private_pack(tmp_path):
    with create_pipe_input() as pipe:
        owner = await create_owned_chat(
            PromptSession(input=pipe, output=DummyOutput()), Console(file=io.StringIO()),
            session_id="session", mode="on", selection=SelectionStore(tmp_path / "selection"),
            library_factory=lambda _: pytest.fail("No grant or network without equipment"),
        )
        try:
            assert owner is not None and owner.controller is None
            assert owner.dock_height == 0
        finally:
            await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", ["refund", "account", "expiry", "selection"])
async def test_equipped_chat_requires_online_library_and_revokes_cached_frames(tmp_path, pack_dir, clock, revoke):
    selection = SelectionStore(tmp_path / "selection")
    selection.write("fixture.pack")
    api = ServerFixture(pack_dir)
    library = RemoteLibrary(api, tmp_path / "cache", selection, clock=clock)
    with create_pipe_input() as pipe:
        owner = await create_owned_chat(
            PromptSession(input=pipe, output=DummyOutput()), Console(file=io.StringIO()),
            session_id="session", mode="on", selection=selection, library_factory=lambda _: library,
            lease_factory=NoWorkerLease,
        )
        try:
            assert owner is not None
            assert api.calls == ["owned", "pack", "owned"]
            assert owner.controller.pack.manifest.pack_id == "fixture.pack"
            if revoke == "refund":
                api.denied = True
                await library.refresh()
            elif revoke == "account":
                api.user = str(uuid4())
            elif revoke == "expiry":
                clock.advance(31)
            else:
                selection.disable()
            owner.tick()
            assert owner.controller.current_state == State.OFF
            assert owner._dock_fragments() == []
        finally:
            await owner.close()
        assert not library.authorize("fixture.pack")


@pytest.mark.asyncio
async def test_forged_selection_never_grants_bundled_preview(tmp_path, pack_dir):
    selection = SelectionStore(tmp_path / "selection")
    selection.write("openvegas.pixel-courier")
    api = ServerFixture(pack_dir)
    api.denied = True
    library = RemoteLibrary(api, tmp_path / "cache", selection)
    console = Console(file=io.StringIO())
    with create_pipe_input() as pipe:
        assert await create_owned_chat(
            PromptSession(input=pipe, output=DummyOutput()), console,
            session_id="session", mode="on", selection=selection, library_factory=lambda _: library,
            lease_factory=NoWorkerLease,
        ) is None
    assert api.calls == ["owned", "owned"]
    assert "chat remains usable" in console.file.getvalue()


@pytest.mark.parametrize("active", [False, True])
def test_emote_guidance_matches_actual_session_transport(active):
    # Execute the actual text-only CLI branch without starting a chat or login.
    source = Path(__file__).resolve().parents[2] / "openvegas" / "cli.py"
    tree = ast.parse(source.read_text())
    branches = [node for node in ast.walk(tree) if isinstance(node, ast.If)
                and ast.unparse(node.test) == "cmd == '/emote'"]
    assert len(branches) == 1
    loop = ast.For(target=ast.Name(id="_", ctx=ast.Store()),
                   iter=ast.List(elts=[ast.Constant(0)], ctx=ast.Load()),
                   body=branches[0].body, orelse=[])
    code = compile(ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])), str(source), "exec")
    output = io.StringIO()
    session_id = str(uuid4())
    exec(code, {"console": Console(file=output, width=240),  # noqa: S102 - trusted local CLI branch, no external input
                "owned_compositor": object() if active else None,
                "runtime_session_id": session_id})
    text = output.getvalue()
    assert "Native compatibility remains under review" in text
    assert "Owned packs require server verification" in text
    assert "Public previews are not purchases" in text
    if active:
        assert "not native-certified" in text
        assert "new session's command" in text
        assert "emote watch" not in text
    else:
        assert f"emote setup --source openvegas --session {session_id} --watch" in text
        assert "ready message" in text and "Ctrl+C" in text
        assert f"emote watch --source openvegas --session {session_id}" in text
        assert "No pane or watcher was launched" in text
        assert "auto mode has no certified native surfaces" in text


@pytest.mark.parametrize("ending", ["success", "failure", "cancel", "ui"])
def test_real_chat_voice_modal_draft_and_turn_cleanup(monkeypatch, tmp_path, ending):
    from openvegas import cli as cli_module
    from openvegas import client as client_module
    from openvegas.emotes import compositor

    async def until(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)

    console = Console(file=io.StringIO(), force_terminal=True)
    client = SimpleNamespace(
        _canonical_chat={"revision": "before"}, auth_preflight=AsyncMock(), aclose=AsyncMock(),
        agent_run_create=AsyncMock(return_value={}), get_balance=AsyncMock(return_value={"balance": "99999"}),
    )
    requested = []
    release = None
    captured = []
    outcomes = []

    async def request(method, path, **kwargs):
        requested.append(kwargs["json"]["prompt"])
        await release.wait()
        if ending == "failure":
            raise client_module.APIError(503, "offline request failed")
        return {"text": "Offline answer fixture.", "revision": "after"}

    client._request = request

    class Voice:
        def __init__(self, console):
            self.state = SimpleNamespace(value="idle")
            self.last_error = None

        @property
        def is_recording(self):
            return self.state.value == "listening"

        def label(self, **kwargs):
            return "Listening fixture" if self.is_recording else "mic"

        async def toggle(self, *, insert_text, transcribe_wav):
            if self.is_recording:
                insert_text("dictated")
                self.state.value = "idle"
            else:
                self.state.value = "listening"

        def stop_if_recording(self):
            self.state.value = "idle"

    async def driver(owner):
        async def ready():
            await until(lambda: owner._pending is not None and not owner._pending.done())

        try:
            await ready()
            if len(captured) > 1:
                owner.request_command("/exit")
                return
            owner.request_command("/emote")
            await until(lambda: "not native-certified" in owner.history_buffer.text)
            await ready()
            assert "emote watch" not in owner.history_buffer.text
            owner.request_command("/voice")
            await until(owner.voice_active)
            await ready()
            owner.default_buffer.document = Document("hello world", 5)
            owner.request_command("/voice")
            await until(lambda: owner.default_buffer.text == "hello dictated world")
            await ready()
            owner.default_buffer.validate_and_handle()
            await until(lambda: requested)
            owner.default_buffer.insert_text("unsent draft")
            if ending == "cancel":
                owner.cancel_turn()
            else:
                release.set()
            await ready()
            expected = {
                "failure": "offline request failed", "cancel": "Task cancelled",
            }.get(ending, "Offline answer fixture.")
            assert expected in owner.history_buffer.text
            assert owner.default_buffer.text == "unsent draft"
            owner.request_command("/continuity off")
            await until(lambda: outcomes)
            await ready()
            assert owner.default_buffer.text == "unsent draft"
            owner.request_command("/ui" if ending == "ui" else "/exit")
        except BaseException:
            if owner.app.is_running:
                owner.app.exit()
            raise

    async def factory(session, console, **kwargs):
        nonlocal release
        release = asyncio.Event()
        kwargs["mode"] = "on"
        owner = await create_owned_chat(
            session, console, **kwargs, selection=SelectionStore(tmp_path / "selection"),
        )
        captured.append(owner)
        owner.app.create_background_task(driver(owner))
        return owner

    def confirm(*args, **kwargs):
        assert captured[0]._suspended
        outcomes.append("modal suspended")
        return False

    monkeypatch.setattr(cli_module, "_load_openvegas_env_defaults_from_dotenv", lambda: None)
    monkeypatch.setattr(cli_module, "load_config", dict)
    monkeypatch.setattr(cli_module, "console", console)
    monkeypatch.setattr(cli_module, "VoiceButton", Voice)
    monkeypatch.setattr(cli_module, "resolve_capability", lambda *a: True)
    monkeypatch.setattr(cli_module, "_clipboard_has_image", lambda: False)
    monkeypatch.setattr(cli_module, "_read_clipboard_text", lambda: "")
    monkeypatch.setattr(cli_module, "emit_metric", lambda *a, **k: None)
    monkeypatch.setattr(cli_module.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_module.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli_module.Confirm, "ask", confirm)
    monkeypatch.setattr(client_module, "OpenVegasClient", lambda: client)
    monkeypatch.setattr(compositor, "create_owned_chat", factory)
    if ending == "ui":
        from openvegas.tui import prompt_ui

        def ui(**kwargs):
            assert captured[0]._closed
            assert compositor.OwnedChatCompositor._active_owner is None
            assert console.file is captured[0]._original_file

        monkeypatch.setattr(prompt_ui, "run_prompt_ui", ui)
    monkeypatch.setenv("OPENVEGAS_CHAT_FULLSCREEN", "0")
    with create_pipe_input() as pipe:
        monkeypatch.setattr(cli_module, "PromptSession", lambda **kwargs: PromptSession(input=pipe, output=DummyOutput(), **kwargs))
        cli_module.chat.callback(provider="openai", model="offline-fixture", dealer_sprite=False)
    assert requested == ["hello dictated world"]
    assert outcomes == ["modal suspended"]
    assert all(owner._closed and owner._tick_task.done() for owner in captured)
    assert len(captured) == (2 if ending == "ui" else 1)
    assert console.file.getvalue().count("Offline answer fixture.") == (0 if ending in {"failure", "cancel"} else 1)
    client.aclose.assert_awaited_once()


@pytest.mark.parametrize("mode", ["auto", "off"])
def test_real_legacy_chat_watch_identity_and_readiness(monkeypatch, tmp_path, mode):
    import re

    import httpx

    from openvegas import cli as cli_module
    from openvegas import client as client_module
    from openvegas.emotes import compositor, spool
    from openvegas.emotes.commands import _watch_boundary
    from openvegas.emotes.events import Phase

    console = Console(file=io.StringIO(), force_terminal=True, width=320, highlight=False)
    client = SimpleNamespace(
        _canonical_chat={"revision": "before"}, auth_preflight=AsyncMock(), aclose=AsyncMock(),
        agent_run_create=AsyncMock(return_value={}), get_balance=AsyncMock(return_value={"balance": "99999"}),
    )
    requested, published, owners = [], [], []
    queue = spool.EventSpool(tmp_path / "events")
    original_publish = spool.publish_event
    identity = None
    watermark = None
    prompt_count = 0

    def publish(event):
        published.append(event)
        return original_publish(event, directory=queue.directory)

    async def request(method, path, **kwargs):
        requested.append(kwargs["json"]["prompt"])
        return {"text": "Offline answer fixture.", "revision": "after"}

    client._request = request

    async def factory(session, output, **kwargs):
        assert kwargs["mode"] == mode
        result = await create_owned_chat(session, output, **kwargs)
        owners.append(result)
        assert result is None
        return result

    async def prompt(session, *args, **kwargs):
        nonlocal prompt_count, identity, watermark
        prompt_count += 1
        if prompt_count == 1:
            return "before watcher readiness"
        if prompt_count == 2:
            return "/emote"
        if prompt_count == 3:
            match = re.search(r"emote watch --source (\S+) --session\s+([A-Za-z0-9-]+)",
                              console.file.getvalue())
            assert match is not None, repr(console.file.getvalue())
            identity = match.groups()
            assert identity[0] == "openvegas"
            assert published and all((event.source, event.session_id) == identity for event in published)
            assert [event.phase for event in published] == [Phase.START, Phase.COMPLETE]
            # Exercise the watcher's actual startup discard against real local
            # metadata files; no renderer, terminal process, or provider runs.
            watermark = _watch_boundary(queue, *identity)
            assert watermark == published[-1].generation
            assert queue.drain(source=identity[0], session_id=identity[1]) == []
            return "after watcher readiness"
        assert prompt_count == 4
        future = queue.drain(source=identity[0], session_id=identity[1])
        eligible = [event for event in future if event.generation > watermark]
        assert [event.phase for event in eligible] == [Phase.START, Phase.COMPLETE]
        assert len({event.turn_id for event in eligible}) == 1
        assert eligible[0].turn_id != published[0].turn_id
        assert all((event.source, event.session_id) == identity for event in eligible)
        return "/exit"

    def forbidden(*args, **kwargs):
        pytest.fail("Offline legacy chat regression must not access provider/network or native voice")

    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                "OPENROUTER_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(cli_module, "_load_openvegas_env_defaults_from_dotenv", lambda: None)
    monkeypatch.setattr(cli_module, "load_config", dict)
    monkeypatch.setattr(cli_module, "console", console)
    monkeypatch.setattr(cli_module, "VoiceButton", lambda _: SimpleNamespace(
        is_recording=False, state=SimpleNamespace(value="idle"), last_error=None,
        label=lambda **kwargs: "mic", stop_if_recording=lambda: None, toggle=forbidden))
    monkeypatch.setattr(cli_module, "resolve_capability", lambda *args: True)
    monkeypatch.setattr(cli_module, "_clipboard_has_image", lambda: False)
    monkeypatch.setattr(cli_module, "_read_clipboard_text", lambda: "")
    monkeypatch.setattr(cli_module, "emit_metric", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_module.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_module.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(client_module, "OpenVegasClient", lambda: client)
    monkeypatch.setattr(compositor, "create_owned_chat", factory)
    monkeypatch.setattr(spool, "publish_event", publish)
    monkeypatch.setattr(PromptSession, "prompt_async", prompt)
    monkeypatch.setenv("OPENVEGAS_CHAT_COMPOSITOR", mode)
    monkeypatch.setenv("OPENVEGAS_CHAT_FULLSCREEN", "0")
    with create_pipe_input() as pipe:
        monkeypatch.setattr(cli_module, "PromptSession", lambda **kwargs: PromptSession(
            input=pipe, output=DummyOutput(), **kwargs))
        cli_module.chat.callback(provider="openai", model="offline-fixture", dealer_sprite=False)
    assert owners == [None]
    assert requested == ["before watcher readiness", "after watcher readiness"]
    assert published[-1].phase == Phase.EXIT
    assert all((event.source, event.session_id) == identity for event in published)
    client.aclose.assert_awaited_once()
