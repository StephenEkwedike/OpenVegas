"""Effective account gates apply to real routes before mocked, offline inference."""

from __future__ import annotations

import copy
import json
import os
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import test_continuity_route_integration as canonical_routes
import test_openrouter as upstream
import test_openrouter_attachment_routes as attachment_routes
import test_openrouter_attachments as files
import test_openrouter_route_preflight as routes
import test_provider_continuity_transactions as continuity

from openvegas.capabilities import resolve_capability
from openvegas.gateway.openrouter import build_payload
from openvegas.gateway.providers import model_capabilities
from server.services.attachment_history import references
from server.services.dependencies import current_flags


@pytest.fixture(autouse=True)
def isolated_offline_runtime(monkeypatch, tmp_path):
    def no_network(*args, **kwargs):
        pytest.fail("Effective capability tests must not access the network")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, no_network)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, no_network)
    for name in tuple(os.environ):
        if name.startswith("OPENVEGAS_"):
            monkeypatch.delenv(name)
    # Lazy config must never fall back to the checkout's .env or native credentials.
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    for name, value in {
        "OPENVEGAS_ENV_FILE": str(empty_env),
        "OPENVEGAS_ROOT": str(tmp_path),
        "OPENVEGAS_TEST_MODE": "1",
        "OPENVEGAS_RUNTIME_ENV": "test",
        "OPENVEGAS_DOTENV_OVERRIDE": "0",
        "OPENVEGAS_ENABLE_TOUCHID": "0",
        "OPENVEGAS_FORCE_CONFIG_REFRESH_STORAGE": "1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "OPENVEGAS_FEATURES_ENABLED": "1",
    }.items():
        monkeypatch.setenv(name, value)
    current_flags.cache_clear()
    yield
    current_flags.cache_clear()


def test_fixture_keeps_lazy_configuration_and_credentials_isolated(tmp_path):
    assert os.environ["OPENVEGAS_ENV_FILE"] == str(tmp_path / "empty.env")
    assert (tmp_path / "empty.env").read_text(encoding="utf-8") == ""
    assert os.environ["OPENVEGAS_ROOT"] == str(tmp_path)
    assert os.environ["OPENVEGAS_TEST_MODE"] == "1"
    assert os.environ["OPENVEGAS_RUNTIME_ENV"] == "test"
    assert os.environ["OPENVEGAS_DOTENV_OVERRIDE"] == "0"
    assert os.environ["OPENVEGAS_ENABLE_TOUCHID"] == "0"
    assert os.environ["OPENVEGAS_FORCE_CONFIG_REFRESH_STORAGE"] == "1"
    assert os.environ["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"


def assert_blocked(response, endpoint, detail):
    if endpoint == "stream":
        assert response.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        errors = [event["payload"] for event in events if event["type"] == "response.error"]
        assert len(errors) == 1 and errors[0]["error"] == "invalid_transition"
        completed = [event["payload"] for event in events if event["type"] == "response.completed"]
        assert len(completed) == 1
        assert completed[0]["status"] == "error" and completed[0]["v_cost"] == "0"
        assert not any(event["type"] in {"response.delta", "tool.call"} for event in events)
    elif endpoint == "canonical":
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "invalid_transition"
    else:
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_transition"
    assert detail in response.text


def set_gate(monkeypatch, feature, configuration):
    if configuration == "env_disabled":
        monkeypatch.setenv(f"OPENVEGAS_ENABLE_{feature.upper()}", "0")
    elif configuration == "rollout_zero":
        monkeypatch.setenv(f"OPENVEGAS_ROLLOUT_{feature.upper()}_PCT", "0")
    elif configuration == "vision_disabled":
        monkeypatch.setenv("OPENVEGAS_ENABLE_VISION", "0")


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.parametrize("retained", [False, True], ids=["current", "retained"])
@pytest.mark.parametrize("kind", ["image", "text"])
@pytest.mark.parametrize(
    "configuration", ["enabled", "env_disabled", "vision_disabled", "rollout_zero"]
)
async def test_owned_attachment_image_gate(
    monkeypatch, endpoint, retained, kind, configuration,
):
    setup = attachment_routes.setup.__wrapped__(monkeypatch)
    image = kind == "image"
    row = files.row(files.picture(), "image/png", "image.png") if image else files.row()
    setup.uploads.resolve_uploaded_for_inference.return_value = [row]
    set_gate(monkeypatch, "image_input", configuration)
    allowed = configuration == "enabled"
    assert resolve_capability(
        "openrouter", files.MODEL, "image_input", user_id=files.OWNER,
    ) is allowed

    if retained:
        setup.thread.prepare_thread.side_effect = None
        setup.thread.prepare_thread.return_value = SimpleNamespace(
            thread_id="fixture-thread", thread_status="active",
        )
        setup.thread.get_recent_messages_with_stats = AsyncMock(return_value=(
            [
                {"role": "user", "content": "Original upload", "attachment_refs": references([row])},
                {"role": "assistant", "content": "Prior answer"},
            ], 2, 0,
        ))
    response = await routes.post(
        setup, endpoint, attachments=[] if retained else [row["file_id"]],
    )
    setup.uploads.resolve_uploaded_for_inference.assert_awaited_once_with(
        user_id=files.OWNER, file_ids=[row["file_id"]],
    )
    if image and not allowed:
        assert_blocked(response, endpoint, "Image input is disabled")
        setup.gateway.infer.assert_not_awaited()
        setup.thread.append_exchange.assert_not_awaited()
    else:
        assert response.status_code == 200 and "Fixture answer" in response.text
        assert "event: response.error" not in response.text
        setup.gateway.infer.assert_awaited_once()
        setup.thread.append_exchange.assert_awaited_once()
        request = setup.gateway.infer.call_args.args[0]
        message = request.messages[0] if retained else request.messages[-1]
        block = message["content"][1]
        if image:
            assert block["type"] == "image_url"
        else:
            assert block["type"] == "text" and block["text"].endswith("complete text")


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
async def test_disabled_image_never_dispatches_text_subset(monkeypatch, endpoint):
    setup = attachment_routes.setup.__wrapped__(monkeypatch)
    rows = [files.row(), files.row(files.picture(), "image/png", "image.png", index=1)]
    setup.uploads.resolve_uploaded_for_inference.return_value = rows
    monkeypatch.setenv("OPENVEGAS_ENABLE_IMAGE_INPUT", "0")
    response = await routes.post(setup, endpoint, attachments=[row["file_id"] for row in rows])
    assert_blocked(response, endpoint, "Image input is disabled")
    setup.gateway.infer.assert_not_awaited()
    setup.thread.append_exchange.assert_not_awaited()


def install_reasoning_review(monkeypatch):
    upstream.install_review(monkeypatch, upstream.review(
        capabilities={"tools": True, "reasoning_efforts": ["high", "none"]},
        supported_parameters=["reasoning"],
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.parametrize("configuration", ["enabled", "env_disabled", "rollout_zero"])
@pytest.mark.parametrize("effort", ["high", "none", None], ids=["high", "none", "default"])
async def test_explicit_reasoning_uses_effective_account_gate(
    monkeypatch, endpoint, configuration, effort,
):
    setup = routes.setup_route.__wrapped__(monkeypatch)
    install_reasoning_review(monkeypatch)
    set_gate(monkeypatch, "reasoning_controls", configuration)
    assert resolve_capability(
        "openrouter", upstream.MODEL, "reasoning_controls", user_id=files.OWNER,
    ) is (configuration == "enabled")
    response = await routes.post(
        setup, endpoint, **({"reasoning_effort": effort} if effort is not None else {}),
    )
    if effort is not None and configuration != "enabled":
        assert_blocked(response, endpoint, "Reasoning controls are disabled")
        setup.gateway.infer.assert_not_awaited()
        setup.thread.prepare_thread.assert_not_awaited()
        setup.thread.append_exchange.assert_not_awaited()
        assert setup.events == []
    else:
        assert response.status_code == 200 and "Fixture answer" in response.text
        assert "event: response.error" not in response.text
        setup.gateway.infer.assert_awaited_once()
        request = setup.gateway.infer.call_args.args[0]
        assert request.reasoning_effort == effort
        payload = build_payload(
            request, setup.state["row"], model_capabilities("openrouter", upstream.MODEL),
        )
        assert payload.get("reasoning") == (
            {"effort": effort, "exclude": True} if effort is not None else None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration", ["enabled", "env_disabled", "rollout_zero"])
@pytest.mark.parametrize("effort", ["high", "none", None], ids=["high", "none", "default"])
async def test_canonical_reasoning_gate_precedes_pending_state(
    monkeypatch, configuration, effort,
):
    state = continuity.setup.__wrapped__(monkeypatch)
    db, service, catalog = state
    app, _, gateway, _, _ = canonical_routes.app_setup.__wrapped__(state, monkeypatch)
    install_reasoning_review(monkeypatch)
    db.models[("openrouter", upstream.MODEL)] = upstream.catalog_row()
    created = await service.create_canonical_thread(
        user_id=continuity.USER, provider="openrouter", model_id=upstream.MODEL, catalog=catalog,
    )
    before = copy.deepcopy(db.messages)
    writes = len(db.writes)
    validate = AsyncMock(wraps=catalog.validate_selection)
    monkeypatch.setattr(catalog, "validate_selection", validate)
    set_gate(monkeypatch, "reasoning_controls", configuration)
    assert resolve_capability(
        "openrouter", upstream.MODEL, "reasoning_controls", user_id=continuity.USER,
    ) is (configuration == "enabled")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture",
    ) as client:
        response = await client.post("/models/conversations/ask", json={
            "provider": "openrouter", "model": upstream.MODEL,
            "thread_id": created.thread_id, "expected_revision": created.revision,
            "prompt": "hello", "idempotency_key": continuity.KEY,
            **({"reasoning_effort": effort} if effort is not None else {}),
        })
    if effort is not None and configuration != "enabled":
        assert_blocked(response, "canonical", "Reasoning controls are disabled")
        gateway.infer.assert_not_awaited()
        validate.assert_not_awaited()
        assert db.messages == before and len(db.writes) == writes
    else:
        assert response.status_code == 200, response.text
        assert not response.json()["continuity_blocked"]
        gateway.infer.assert_awaited_once()
        assert gateway.infer.call_args.args[0].reasoning_effort == effort
