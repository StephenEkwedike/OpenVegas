"""Real CLI closures/guards with an authenticated-server fake, no local review/key config."""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import openvegas.cli as cli
from openvegas.client import APIError
from openvegas.tui.model_picker import (
    ModelSelectionError,
    reviewed_capabilities,
    validate_selection,
)

SOURCE = Path(cli.__file__)
MODEL = "fixture/exact-remote-model"


def descriptor(**caps):
    return {
        "provider": "openrouter", "model_id": MODEL, "enabled": True, "available": True,
        "capabilities": {
            "reviewed": True, "text": True, "tools": True, "image_input": True,
            "file_upload": True, "web_search": True, "stream_events": True,
            "streaming_mode": "buffered", "reasoning_controls": True,
            "reasoning_efforts": ["low", "high", "xhigh"], **caps,
        },
    }


class Server:
    def __init__(self, row=None):
        self.row = row or descriptor()
        self.failure = None
        self.validation_calls = 0
        self.get_mode = AsyncMock(return_value={"conversation_mode": "persistent"})
        self.ask = AsyncMock(return_value={"text": "answer"})
        self.streamed = []
        self.upload_init = AsyncMock(side_effect=lambda **kw: {"upload_id": kw["filename"]})
        self.upload_complete = AsyncMock(side_effect=lambda **kw: {"file_id": kw["upload_id"]})

    async def list_models(self, provider):
        assert provider == "openrouter"
        return {"models": [copy.deepcopy(self.row)]}

    async def _request(self, method, path, *, json):
        assert (method, path) == ("POST", "/models/validate")
        assert json == {"provider": "openrouter", "model": MODEL}
        self.validation_calls += 1
        if self.failure:
            raise self.failure
        return {"model": copy.deepcopy(self.row), "selection_valid": True, "state_changed": False}

    async def ask_stream(self, *args, **kwargs):
        self.streamed.append((args, kwargs))
        yield {"event": "response.completed", "data": {"payload": {"status": "ok", "text": "answer"}}}


def attachment(tmp_path, name="image.png"):
    path = tmp_path / name
    path.write_bytes(b"private-test-file-bytes")
    return cli.PendingAttachment(name, str(path), name,
                                 "image/png" if name.endswith("png") else "text/plain", 23, "test-digest")


def chat_shell(client, pending=(), effort=None):
    """Compile unchanged production helpers and the actual send-preflight AST span.

    Only presentation hooks are no-ops. Uploads, selection, resolver, reasoning,
    streaming, and the turn's web/file routing expressions are production code.
    """
    tree = ast.parse(SOURCE.read_text())
    names = {
        "_chat_capability", "_use_model_capabilities", "_refresh_model_capabilities",
        "_validate_openrouter_request", "_reasoning_status", "_reasoning_for_model",
        "_sync_chat_preferences", "_ask_with_optional_stream", "_env_flag",
        "_prepare_attachments_for_turn", "_set_attachment_state",
    }
    helpers = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in names]
    assert {node.name for node in helpers} == names
    lines = SOURCE.read_text().splitlines()
    anchor = next(i for i, line in enumerate(lines) if "_, unsupported_count, attachments_blocked =" in line)
    start = max(i for i in range(anchor) if lines[i] == "            try:")
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "last_voice_transcribe_used = False")
    # Include validation, whole-queue capability guard, real uploads and batch-failure guard.
    guards = "\n".join(line[12:] for line in lines[start:end])
    routing_names = {"web_search_requested_turn", "web_search_effective_turn", "attachments_effective_turn"}
    routing = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id in routing_names for t in node.targets)]
    assert len(routing) == 3
    code = """def build(client, pending, effort):
    current_provider, current_model, current_thread_id = 'openrouter', MODEL, None
    current_model_capabilities, current_reasoning_efforts = None, ()
    current_reasoning_effort = effort
    conversation_mode, web_search_requested = 'persistent', True
    startup_bootstrap_task, show_stream_status = None, False
    pending_attachments, uploaded_attachment_cache = list(pending), {}
    attachment_preview_max_chars = 1000
"""
    code += "\n".join("    " + line for node in helpers for line in ast.unparse(node).splitlines())
    code += """
    async def send(message='Analyze these attachments'):
        for _ in [0]:
"""
    code += "\n".join("            " + line for line in guards.splitlines())
    code += "\n            user_message = message\n"
    code += "\n".join("            " + line for node in routing for line in ast.unparse(node).splitlines())
    code += """
            return await _ask_with_optional_stream(message, idempotency_key='same-key', enable_tools=False,
                enable_web_search=web_search_effective_turn, attachments=attachment_file_ids_for_turn,
                reasoning_effort=current_reasoning_effort)
    return SimpleNamespace(send=send, sync=_sync_chat_preferences, refresh=_refresh_model_capabilities,
        validate=_validate_openrouter_request, capability=_chat_capability, ask=_ask_with_optional_stream,
        status=_reasoning_status, pending=pending_attachments,
        snapshot=lambda: current_model_capabilities, effort=lambda: current_reasoning_effort)
"""
    output = []
    namespace = vars(cli).copy()
    namespace.update({"MODEL": MODEL, "APIError": APIError, "SimpleNamespace": SimpleNamespace,
                      "validate_selection": validate_selection, "ModelSelectionError": ModelSelectionError,
                      "reviewed_capabilities": reviewed_capabilities,
                      "console": SimpleNamespace(print=lambda *a, **k: output.append(str(a))),
                      "_render_attachment_status_row": lambda **_: None,
                      "_render_upload_queue_preview": lambda: None,
                      "render_status_bar": lambda *a: None, "_status_actor": lambda: "model",
                      "workspace_root": "/tmp", "_emit_attachment_event": lambda *a: None})
    exec(compile(code, str(SOURCE), "exec"), namespace)  # noqa: S102 - Exact repository AST, never user input.
    result = namespace["build"](client, pending, effort)
    result.output = output
    return result


@pytest.fixture(autouse=True)
def customer_environment(monkeypatch):
    monkeypatch.delenv("OPENVEGAS_MODEL_REVIEWS_JSON", raising=False)
    monkeypatch.delenv("OPENVEGAS_PROVIDER_KEYS_JSON", raising=False)
    monkeypatch.setenv("OPENVEGAS_CHAT_UPLOAD_RETRY_MAX", "0")
    original = cli.resolve_capability
    def direct_only(provider, *args):
        assert provider != "openrouter", "OpenRouter must never consult local operator configuration"
        return original(provider, *args)
    monkeypatch.setattr(cli, "resolve_capability", direct_only)


@pytest.mark.asyncio
async def test_initial_selection_uses_exact_remote_descriptor_for_status_and_reasoning():
    server = Server()
    shell = chat_shell(server)
    await shell.sync()
    assert server.validation_calls == 1
    assert all(shell.capability(feature) for feature in ("image_input", "file_upload", "web_search", "stream_events"))
    assert "low, high, xhigh" in shell.status()
    # Dictation uses the independent managed speech service, not this chat model.
    assert shell.capability("speech_to_text")
    assert not shell.snapshot().supports("openrouter", MODEL + "-other", "image_input")


@pytest.mark.asyncio
async def test_real_attachment_preflight_upload_and_stream_preserve_all_remote_ids(tmp_path):
    server = Server()
    files = [attachment(tmp_path), attachment(tmp_path, "notes.txt")]
    shell = chat_shell(server, files, "xhigh")
    result = await shell.send()
    assert result["text"] == "answer"
    assert server.validation_calls == 2  # Before upload and before inference.
    assert server.upload_complete.await_count == 2
    args, sent = server.streamed[0]
    assert args[1:3] == ("openrouter", MODEL)
    assert sent["attachments"] == ["image.png", "notes.txt"]
    assert sent["reasoning_effort"] == "xhigh"
    assert len(shell.pending) == 2  # The outer successful-turn commit owns consumption.
    status = cli._format_composer_attachment_status_row(files, provider="openrouter", model=MODEL,
                                                       remote_capabilities=shell.snapshot())
    assert "unsupported" not in status


@pytest.mark.asyncio
async def test_web_routing_and_next_turn_refresh_use_server_caps():
    server = Server()
    shell = chat_shell(server, effort="high")
    await shell.send("Search the web for today's news")
    assert server.streamed[-1][1]["enable_web_search"] is True
    server.row["capabilities"]["web_search"] = False
    await shell.send("Search the web for today's news")
    assert server.streamed[-1][1]["enable_web_search"] is False
    assert server.validation_calls == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["image", "files", "unreviewed", "unknown", "refresh", "effort"])
async def test_failed_preflight_keeps_mixed_queue_and_never_uploads_or_infers(tmp_path, failure):
    server = Server()
    files = [attachment(tmp_path), attachment(tmp_path, "notes.txt")]
    shell = chat_shell(server, files, "high")
    await shell.refresh()
    if failure in {"image", "files"}:
        server.row["capabilities"]["image_input" if failure == "image" else "file_upload"] = False
    elif failure == "unreviewed":
        server.row["capabilities"]["reviewed"] = False
    elif failure == "unknown":
        server.row["model_id"] += "-other"
    elif failure == "refresh":
        server.failure = APIError(503, "validation unavailable")
    else:
        server.row["capabilities"]["reasoning_efforts"] = ["low"]
    assert await shell.send() is None
    assert shell.pending == files and all(att.state == cli.AttachmentState.ATTACHED for att in files)
    server.upload_init.assert_not_awaited()
    assert not server.streamed
    server.ask.assert_not_awaited()
    assert shell.effort() == "high"  # Never silently revert a request setting during a turn.
    if failure in {"unreviewed", "unknown", "refresh"}:
        assert shell.snapshot() is None and not shell.capability("image_input")


@pytest.mark.asyncio
async def test_partial_upload_failure_never_sends_successful_subset(tmp_path):
    server = Server()
    files = [attachment(tmp_path), attachment(tmp_path, "notes.txt")]
    server.upload_complete.side_effect = [{"file_id": "image.png"}, APIError(422, "invalid file metadata")]
    shell = chat_shell(server, files)
    assert await shell.send() is None
    assert shell.pending == files
    assert files[0].state == cli.AttachmentState.UPLOADED
    assert files[1].state == cli.AttachmentState.FAILED
    assert not server.streamed
    server.ask.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("capability,kwargs", [
    ("web_search", {"enable_web_search": True}), ("tools", {"enable_tools": True}),
    ("file_upload", {"attachments": ["owned-id"]}),
    ("reasoning_controls", {"reasoning_effort": "xhigh"}),
])
async def test_dispatch_revalidates_and_never_downgrades_requested_features(capability, kwargs):
    server = Server()
    shell = chat_shell(server)
    await shell.refresh()
    server.row["capabilities"][capability] = False
    if capability == "reasoning_controls":
        server.row["capabilities"]["reasoning_efforts"] = []
    args = dict(enable_tools=False, enable_web_search=False, attachments=[], **{})
    args.update(kwargs)
    with pytest.raises(APIError):
        await shell.ask("question", idempotency_key="same-key", **args)
    assert not server.streamed
    server.ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_buffered_nonstream_dispatch_keeps_exact_payload():
    server = Server(descriptor(stream_events=False))
    shell = chat_shell(server, effort="low")
    await shell.send("Search the web for today's news")
    assert not server.streamed
    assert server.ask.call_args.kwargs["reasoning_effort"] == "low"
    assert server.ask.call_args.kwargs["enable_web_search"] is True


@pytest.mark.parametrize("change", [
    {"reviewed": False}, {"reviewed": "true"}, {"image_input": 1}, {"file_upload": "true"},
    {"web_search": None}, {"streaming_mode": []}, {"reasoning_efforts": ["high", "high"]},
    {"reasoning_efforts": ["unbounded"]}, {"reasoning_controls": False},
])
def test_descriptor_rejects_malformed_claims(change):
    with pytest.raises(ModelSelectionError):
        reviewed_capabilities(descriptor(**change), "openrouter", MODEL)


@pytest.mark.asyncio
async def test_validation_response_not_catalog_claims_is_authority():
    server = Server()
    async def request(*args, **kwargs):
        return {"selection_valid": True, "state_changed": False,
                "model": descriptor(image_input=False, web_search=False)}
    server._request = request
    selected = await validate_selection(server, "openrouter", MODEL)
    caps = reviewed_capabilities(selected, "openrouter", MODEL)
    assert not caps.supports("openrouter", MODEL, "image_input")
    assert not caps.supports("openrouter", MODEL, "web_search")


@pytest.mark.parametrize("provider,model", [("openai", "gpt-5"), ("anthropic", "claude-test"), ("gemini", "gemini-test")])
def test_direct_providers_keep_their_existing_resolver(provider, model):
    for feature in ("image_input", "file_upload", "stream_events", "web_search"):
        assert cli._model_capability(provider, model, feature) == cli.resolve_capability(provider, model, feature)


@pytest.mark.parametrize("scenario", ["success", "unsupported", "refresh_failed", "partial_upload"])
def test_complete_chat_command_remote_attachments_never_drop_or_send_subset(monkeypatch, tmp_path, scenario):
    from click.testing import CliRunner

    files = [attachment(tmp_path), attachment(tmp_path, "notes.txt")]
    server = Server(descriptor(image_input=scenario != "unsupported"))
    server.get_balance = AsyncMock(return_value={"balance": "1000", "balance_v": "1000"})
    server.suggest_topup = AsyncMock(return_value={"low_balance": False})
    if scenario == "partial_upload":
        server.upload_complete.side_effect = [{"file_id": "image.png"}, APIError(422, "invalid metadata")]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("openvegas.client.OpenVegasClient", lambda: server)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setenv("OPENVEGAS_CHAT_PROMPT_TOOLKIT", "0")
    monkeypatch.setenv("OPENVEGAS_CLI_DEALER_ENABLED", "0")
    commands = iter(["/reasoning high", "/attach image.png", "/attach notes.txt",
                     "Analyze these attachments", "/attachments", "/exit"])
    def prompt(*args, **kwargs):
        command = next(commands)
        if command == "Analyze these attachments" and scenario == "refresh_failed":
            server.failure = APIError(503, "review endpoint unavailable")
        return command
    monkeypatch.setattr(cli.Prompt, "ask", prompt)
    result = CliRunner().invoke(cli.cli, ["chat", "--provider", "openrouter", "--model", MODEL])
    assert result.exit_code == 0, (result.output, result.exception)
    if scenario == "success":
        assert len(server.streamed) == 1, result.output
        sent = server.streamed[0][1]
        assert sent["attachments"] == [att.name for att in files]
        assert sent["reasoning_effort"] == "high"
        assert "No pending attachments" in result.output
    else:
        assert not server.streamed, result.output
        server.ask.assert_not_awaited()
        assert "attachments retained" in result.output.lower()
        assert "No pending attachments" not in result.output
        assert all(name in result.output for name in ("image.png", "notes.txt"))
        if scenario != "partial_upload":
            server.upload_init.assert_not_awaited()


def test_complete_chat_web_status_refreshes_server_descriptor(monkeypatch, tmp_path):
    from click.testing import CliRunner

    server = Server()
    server.get_balance = AsyncMock(return_value={"balance": "1000"})
    server.suggest_topup = AsyncMock(return_value={"low_balance": False})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("openvegas.client.OpenVegasClient", lambda: server)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    commands = iter(["/reasoning", "/web", "/web", "/web", "/exit"])
    web_count = 0
    def prompt(*args, **kwargs):
        nonlocal web_count
        command = next(commands)
        if command == "/web":
            web_count += 1
            if web_count == 2:
                server.row["capabilities"]["web_search"] = False
            if web_count == 3:
                server.failure = APIError(503, "review endpoint unavailable")
        return command
    monkeypatch.setattr(cli.Prompt, "ask", prompt)
    result = CliRunner().invoke(cli.cli, ["chat", "--provider", "openrouter", "--model", MODEL])
    assert result.exit_code == 0, (result.output, result.exception)
    assert "effective=True" in result.output and "effective=False" in result.output
    assert "Capabilities unavailable" in result.output
    assert server.validation_calls == 5  # Initial, reasoning, each of three /web commands.
    assert not server.streamed
