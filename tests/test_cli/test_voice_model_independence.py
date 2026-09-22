"""Exercise the real nested voice handler without a microphone or network."""

import ast
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openvegas import cli
from openvegas.client import APIError


def handler(client, provider):
    tree = ast.parse(Path(cli.__file__).read_text())
    functions = [node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in {"_voice_timeout", "_transcribe_voice_wav"}]
    assert len(functions) == 2
    namespace = vars(cli).copy()
    namespace.update(
        client=client, current_provider=provider, voice_transcribe_model="gpt-4o-mini-transcribe",
        voice_transcribe_language=None, uploaded_attachment_cache={}, APIError=APIError,
        _render_capability_status=lambda *args: None, emit_metric=lambda *args: None,
    )
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(cli.__file__), "exec"), namespace)  # noqa: S102 - Repository-owned handler only.
    return namespace["_transcribe_voice_wav"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "openrouter", "anthropic", "gemini", "mistral"])
@pytest.mark.parametrize("upload_fallback", [False, True])
async def test_dictation_keeps_speech_service_when_chat_model_changes(tmp_path, provider, upload_fallback):
    wav = tmp_path / "speech.wav"
    wav.write_bytes(b"RIFFsynthetic audio data")
    speech = AsyncMock(return_value={"text": " dictated text "})
    if upload_fallback:
        speech.side_effect = [APIError(422, "content_base64 unsupported"), {"text": " dictated text "}]
    client = SimpleNamespace(
        speech_transcribe=speech,
        upload_init=AsyncMock(return_value={"upload_id": "owned-fixture"}),
        upload_complete=AsyncMock(return_value={"file_id": "owned-fixture"}),
    )
    assert await handler(client, provider)(str(wav), 1.0) == "dictated text"
    assert speech.await_count == (2 if upload_fallback else 1)
    for call in speech.await_args_list:
        assert call.kwargs["provider"] == "openai"
        assert call.kwargs["model"] == "gpt-4o-mini-transcribe"
    assert speech.await_args_list[0].kwargs["content_base64"] == base64.b64encode(wav.read_bytes()).decode()
    if not upload_fallback:
        client.upload_init.assert_not_awaited()


@pytest.mark.parametrize("provider", ["openai", "openrouter", "anthropic", "gemini", "mistral"])
def test_speech_capability_independent_of_chat_review(monkeypatch, provider):
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    monkeypatch.setenv("OPENVEGAS_ENABLE_SPEECH_TO_TEXT", "1")
    assert cli._model_capability(provider, "unrelated-chat-model", "speech_to_text")
    monkeypatch.setenv("OPENVEGAS_ENABLE_SPEECH_TO_TEXT", "0")
    assert not cli._model_capability(provider, "unrelated-chat-model", "speech_to_text")
