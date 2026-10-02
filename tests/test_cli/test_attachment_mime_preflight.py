"""Local attachment type gate; recorded voice uses a separate speech path."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openvegas import cli


@pytest.mark.parametrize("mime", ["audio/wav", "audio/mpeg", "video/mp4", "image/gif", "image/svg+xml", "application/zip", "application/octet-stream"])
def test_unsupported_openrouter_types_keep_entire_queue(monkeypatch, mime):
    monkeypatch.setattr(cli, "_model_capability", lambda *args: True)
    output = Mock()
    monkeypatch.setattr(cli, "console", output)
    pending = [SimpleNamespace(mime_type="text/plain", path="notes.txt"),
               SimpleNamespace(mime_type=mime, path="input")]
    before = [vars(item).copy() for item in pending]
    kept, count, blocked = cli._preflight_filter_attachments_for_capabilities(
        pending, provider="openrouter", model="reviewed/model")
    assert blocked and count == 1 and kept == pending
    assert all(a is b for a, b in zip(kept, pending, strict=True))
    assert [vars(item) for item in pending] == before
    if mime.startswith("audio/"):
        message = output.print.call_args.args[0]
        assert "/detach" in message and "/voice" in message
        assert "does not transcribe the attached file" in message
    else:
        output.print.assert_not_called()


def test_allowlist_matches_backend_decoder_and_preserves_capability_gates(monkeypatch):
    # Read constants only; importing server code is unnecessary for this local test.
    path = Path(cli.__file__).parents[1] / "server/services/openrouter_attachments.py"
    tree = ast.parse(path.read_text())
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name == "TEXT_MIMES":
                values[name] = ast.literal_eval(node.value.args[0])
            elif name == "IMAGE_MIMES":
                values[name] = ast.literal_eval(node.value)
    allowed = set(values["TEXT_MIMES"]) | set(values["IMAGE_MIMES"]) | {"application/pdf"}
    pending = [SimpleNamespace(mime_type=mime, path="input") for mime in sorted(allowed)]
    monkeypatch.setattr(cli, "_model_capability", lambda *args: True)
    assert cli._preflight_filter_attachments_for_capabilities(pending, provider="openrouter", model="m") == (pending, 0, False)
    monkeypatch.setattr(cli, "_model_capability", lambda p, m, feature, caps: feature != "image_input")
    assert cli._preflight_filter_attachments_for_capabilities(pending, provider="openrouter", model="m") == (pending, 3, True)
    monkeypatch.setattr(cli, "_model_capability", lambda *args: False)
    assert cli._preflight_filter_attachments_for_capabilities(pending, provider="openrouter", model="m") == (pending, len(pending), True)


def test_other_providers_unchanged_and_missing_mime_sniffed(monkeypatch):
    monkeypatch.setattr(cli, "_model_capability", lambda *args: True)
    output = Mock()
    monkeypatch.setattr(cli, "console", output)
    pending = [SimpleNamespace(mime_type=None, path="recording.wav")]
    assert cli._preflight_filter_attachments_for_capabilities(pending, provider="openai", model="m") == (pending, 0, False)
    output.print.assert_not_called()
    assert cli._preflight_filter_attachments_for_capabilities(pending, provider="openrouter", model="m") == (pending, 1, True)
