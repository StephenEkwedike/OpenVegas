"""Authenticated HTTP request preparation; no provider calls or production data."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_openrouter as upstream
import test_openrouter_attachments as files
import test_openrouter_route_preflight as route

from openvegas.capabilities import get_caps, resolve_capability
from openvegas.gateway.openrouter import build_payload, input_token_bound
from openvegas.gateway.providers import model_capabilities
from server.routes import inference as routes
from server.services.attachment_history import references
from server.services.file_uploads import FileUploadError


@pytest.fixture
def setup(monkeypatch):
    setup = route.setup_route.__wrapped__(monkeypatch)
    setup.state["row"] = files.config()
    upstream.install_review(monkeypatch, files.review())
    setup.uploads = SimpleNamespace(
        resolve_uploaded_for_inference=AsyncMock(return_value=[files.row()])
    )
    monkeypatch.setattr(routes, "get_file_upload_service", lambda: setup.uploads)
    monkeypatch.setenv("OPENVEGAS_ENABLE_FILES", "1")
    return setup


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
async def test_full_file_content_arrives_and_content_refs_persist(setup, endpoint):
    response = await route.post(setup, endpoint, attachments=[files.IDS[0]])
    assert response.status_code == 200 and "Fixture answer" in response.text
    req = setup.gateway.infer.call_args.args[0]
    assert req.max_tokens == 1000
    assert req.messages[-1]["content"][1]["text"].endswith("complete text")
    assert "Binary attachment" not in json.dumps(req.messages)
    assert "attachment_refs" not in json.dumps(req.messages)
    payload = build_payload(req, setup.state["row"], model_capabilities("openrouter", files.MODEL))
    assert payload["provider"]["only"] == ["fixture-provider"]
    assert payload["plugins"] == []
    assert input_token_bound(req) > len("complete text")
    assert setup.thread.append_exchange.call_args.kwargs["attachment_refs"] == references(
        [files.row()]
    )
    setup.uploads.resolve_uploaded_for_inference.assert_awaited_once_with(
        user_id=files.OWNER, file_ids=[files.IDS[0]]
    )


@pytest.mark.asyncio
async def test_followup_reauthorizes_retained_file_even_without_new_upload(setup):
    setup.thread.prepare_thread.return_value = SimpleNamespace(
        thread_id="fixture-thread", thread_status="active"
    )
    setup.thread.prepare_thread.side_effect = None
    setup.thread.get_recent_messages_with_stats = AsyncMock(
        return_value=(
            [
                {
                    "role": "user",
                    "content": "original",
                    "attachment_refs": references([files.row()]),
                },
                {"role": "assistant", "content": "Prior answer"},
            ],
            2,
            0,
        )
    )
    response = await route.post(setup, "ask", prompt="What else?")
    assert response.status_code == 200
    req = setup.gateway.infer.call_args.args[0]
    assert len(req.messages) == 3
    assert req.messages[0]["content"][1]["text"].endswith("complete text")
    assert req.messages[2] == {"role": "user", "content": "What else?"}
    setup.uploads.resolve_uploaded_for_inference.assert_awaited_once()
    assert "attachment_refs" not in setup.thread.append_exchange.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["owner", "content", "disabled", "oversize"])
async def test_invalid_upload_never_reaches_paid_gateway(setup, monkeypatch, failure):
    if failure == "owner":
        setup.uploads.resolve_uploaded_for_inference.side_effect = FileUploadError(
            "not_found", "secret filename", 404
        )
    elif failure == "content":
        setup.uploads.resolve_uploaded_for_inference.return_value = [
            files.row(b"not a png", "image/png", "image.png")
        ]
    elif failure == "disabled":
        monkeypatch.setenv("OPENVEGAS_ENABLE_FILES", "0")
    else:
        setup.uploads.resolve_uploaded_for_inference.return_value = [files.row(b"x" * 65_537)]
    response = await route.post(setup, "ask", attachments=[files.IDS[0]])
    assert response.status_code in {400, 404, 413}
    assert "secret filename" not in response.text
    setup.gateway.infer.assert_not_awaited()
    setup.thread.append_exchange.assert_not_awaited()


def test_caps_are_exact_review_not_env_wildcard(setup, monkeypatch):
    assert get_caps("openrouter", files.MODEL).image_input
    assert model_capabilities("openrouter", files.MODEL)["file_upload"]
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    monkeypatch.setenv("OPENVEGAS_ENABLE_FILE_UPLOAD", "1")
    assert not resolve_capability("openrouter", files.MODEL, "file_upload")


@pytest.mark.parametrize("policy", [None, [], "bad", False])
def test_malformed_review_cannot_crash_discovery(monkeypatch, policy):
    value = files.review()
    value["attachments"] = policy
    upstream.install_review(monkeypatch, value)
    assert not model_capabilities("openrouter", files.MODEL)["file_upload"]
