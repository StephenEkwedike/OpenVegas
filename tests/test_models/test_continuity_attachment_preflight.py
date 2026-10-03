"""Execute the real outer CLI preflight before any upload or paid turn work."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openrouter", "openai", "anthropic"])
@pytest.mark.parametrize("canonical,attached", [(True, True), (True, False), (False, True)])
async def test_text_continuity_blocks_before_upload_preserving_queue(provider, canonical, attached):
    path = Path(__file__).parents[2] / "openvegas/cli.py"
    tree = ast.parse(path.read_text())
    # Extract the contiguous production preflight through the first upload call.
    parents = [node for node in ast.walk(tree) if isinstance(getattr(node, "body", None), list)
               and any(isinstance(child, ast.Assign) and isinstance(child.value, ast.Await)
                       and isinstance(child.value.value, ast.Call)
                       and isinstance(child.value.value.func, ast.Name)
                       and child.value.value.func.id == "_prepare_attachments_for_turn" for child in node.body)]
    assert len(parents) == 1
    body = parents[0].body
    stop = next(i for i, node in enumerate(body) if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Await) and isinstance(node.value.value.func, ast.Name)
                and node.value.value.func.id == "_prepare_attachments_for_turn")
    start = next(i for i, node in enumerate(body[:stop]) if isinstance(node, ast.Try)
                 and "await _validate_openrouter_request(reasoning_effort=current_reasoning_effort)" in ast.unparse(node))
    wrapper = ast.parse("async def run():\n    for turn in [0]:\n        pass\n")
    wrapper.body[0].body[0].body = body[start:stop + 1]
    queued = [SimpleNamespace(local_id="audio", mime_type="audio/wav", state="attached"),
              SimpleNamespace(local_id="text", mime_type="text/plain", state="attached")] if attached else []
    original = list(queued)
    client = SimpleNamespace(_canonical_chat={"revision": "unchanged"} if canonical else None)
    upload = AsyncMock(return_value=([], [], "", []))
    validation = AsyncMock()
    output = []
    namespace = {
        "asyncio": asyncio, "client": client, "pending_attachments": queued,
        "startup_bootstrap_task": None, "current_reasoning_effort": None,
        "current_provider": provider, "current_model": "fixture", "current_model_capabilities": None,
        "_validate_openrouter_request": validation, "APIError": RuntimeError,
        "ModelSelectionError": ValueError,
        "console": SimpleNamespace(print=lambda text, **kw: output.append(text)),
        "_preflight_filter_attachments_for_capabilities": lambda queue, **kw: (queue, 0, False),
        "_render_attachment_status_row": lambda **kw: None,
        "_render_upload_queue_preview": lambda: None, "_prepare_attachments_for_turn": upload,
    }
    exec(compile(ast.fix_missing_locations(wrapper), str(path), "exec"), namespace)  # noqa: S102 - Trusted production AST.
    await namespace["run"]()
    assert queued == original
    assert all(a is b for a, b in zip(queued, original, strict=True))
    assert all(item.state == "attached" for item in queued)
    if canonical and attached:
        validation.assert_not_awaited()
        upload.assert_not_awaited()
        assert "/continuity off" in output[0] and "with these attachments" in output[0]
        assert "Nothing uploaded or sent" in output[0]
    else:
        validation.assert_awaited_once()
        upload.assert_awaited_once()
    assert client._canonical_chat == ({"revision": "unchanged"} if canonical else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,confirm,running,reset", [
    ("off", True, False, True), ("off", False, False, False),
    ("off", True, True, False), ("on", True, False, False),
])
async def test_fresh_coding_mode_retains_attachments(mode, confirm, running, reset):
    path = Path(__file__).parents[2] / "openvegas/cli.py"
    tree = ast.parse(path.read_text())
    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.If)
                and ast.unparse(node.test) == "cmd == '/continuity'"]
    assert len(handlers) == 1
    wrapper = ast.parse("async def run():\n    for turn in [0]:\n        pass\n")
    wrapper.body[0].body[0].body = handlers[0].body
    attachments = [object()]
    transcript = ["prior context"]
    client = SimpleNamespace(_canonical_chat={"revision": "original"}, _request=AsyncMock())
    prompts = []
    def ask(text, **kwargs):
        prompts.append(text)
        return confirm
    async def modal(callback):
        return callback()
    namespace = {
        "asyncio": asyncio, "parts": ["/continuity", mode], "allow_model_switch": True,
        "conversation_mode": "persistent", "pending_attachments": attachments,
        "model_switch_local_tools": SimpleNamespace(_BACKGROUND_JOBS={
            "fixture": SimpleNamespace(process=SimpleNamespace(returncode=None if running else 0)),
        }),
        "console": SimpleNamespace(print=lambda *a, **k: None), "Confirm": SimpleNamespace(ask=ask),
        "_chat_modal": modal, "startup_bootstrap_task": None, "client": client,
        "native_generation_session": SimpleNamespace(history_active=False, prepared_handoff=False),
        "chat_transcript": transcript, "APIError": RuntimeError, "ModelSelectionError": ValueError,
    }
    exec(compile(ast.fix_missing_locations(wrapper), str(path), "exec"), namespace)  # noqa: S102
    await namespace["run"]()
    assert len(attachments) == 1
    client._request.assert_not_awaited()
    assert (client._canonical_chat is None) is reset
    assert transcript == ([] if reset else ["prior context"])
    if prompts:
        assert "pending attachments will be retained" in prompts[0]
        assert "are disabled" not in prompts[0]
