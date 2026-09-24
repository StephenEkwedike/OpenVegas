"""Original input provenance across wire, replay and private native settings."""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from openvegas.agent.native_continuation import frozen_settings, original_user_text, restore_request
from openvegas.agent.native_envelope import history_inputs
from openvegas.agent.native_scope_client import NativeGenerationSession
from openvegas.client import OpenVegasClient
from openvegas.contracts.errors import ContractError
from openvegas.contracts.native_scope import validate_native_user_text
from server.routes.inference import AskRequest
from server.services.inference_replay import command_fingerprint
from tests.test_models.test_native_continuation_client import reply
from tests.test_models.test_native_continuation_contract import command
from tests.test_models.test_native_generation_ownership import rows

TEXT = "Read the file.\r\nKeep exact text."


def initial():
    return {**command(), "idempotency_key": "initial", "native_history": True,
            "native_user_text": TEXT, "prompt": "RUNTIME POLICY: source tool protocol and current user input"}


def fingerprint(value):
    return command_fingerprint({k: v for k, v in value.items() if k != "idempotency_key"})


def test_new_input_is_in_replay_identity_legacy_defaults_unchanged():
    first = initial()
    old = {k: v for k, v in first.items() if k != "native_user_text"}
    assert fingerprint(old) == fingerprint({**old, "native_user_text": None})
    assert fingerprint(first) != fingerprint(old)
    assert fingerprint(first) != fingerprint({**first, "native_user_text": TEXT + " "})


@pytest.mark.parametrize("text", [True, 3, [], {"role": "assistant"}, "", " \n",
                                  "\ud800", "a\u202eb", "a\x1bb", "\u754c" * 22000,
                                  "password=synthetic_fixture", "x" * 64001])
def test_invalid_original_input_fails_before_replay_and_transport(text):
    with pytest.raises(ValueError):
        validate_native_user_text(text)
    with pytest.raises(ValidationError):
        AskRequest(**{**initial(), "native_user_text": text})
    with pytest.raises(ContractError):
        fingerprint({**initial(), "native_user_text": text})


@pytest.mark.parametrize("mutation", ["legacy", "unscoped", "continuation"])
def test_original_input_cannot_be_smuggled_into_unowned_or_later_generation(mutation):
    value = initial()
    if mutation == "legacy":
        value["native_history"] = False
    elif mutation == "unscoped":
        value["native_scope"] = None
    else:
        value["native_continuation"] = {"previous_inference_request_id": str(uuid4()),
                                         "expected_history_revision": 0}
    with pytest.raises(ValidationError):
        AskRequest(**value)
    with pytest.raises(ContractError):
        fingerprint(value)


def test_capture_preserves_original_input_and_continuation_restores_not_replaces():
    req = AskRequest(**initial())
    settings = frozen_settings(req, max_tokens=1024)
    inputs = history_inputs(attachment_refs=[], settings=settings)
    assert original_user_text(inputs.values()) == TEXT
    assert settings["prompt"] != TEXT
    _, claim, _ = rows()
    claim = replace(claim, history_inputs_json=inputs._json)
    followup = AskRequest(**{**initial(), "native_user_text": None, "prompt": "Not authoritative"})
    restored = restore_request(followup, claim)
    assert restored.native_user_text == TEXT
    assert frozen_settings(restored, max_tokens=1024) == settings
    assert "native_user_text" not in repr(req)
    assert TEXT not in repr(inputs)


@pytest.mark.parametrize("inputs", [{}, {"settings": {}}, {"settings": {"prompt": TEXT}},
                                     {"settings": {"native_user_text": None}}])
def test_old_or_incomplete_tasks_remain_ineligible_no_prompt_parsing(inputs):
    with pytest.raises(ContractError, match="no verified original user input"):
        original_user_text(inputs)


def test_original_input_is_frozen_for_same_key_and_successive_commands():
    request = initial()
    scope = request["native_scope"]
    options = {"model": request["model"]}
    session = NativeGenerationSession()
    def prepare(key, text):
        return session.prepare(key=key, scope=scope, options=options, history=True, user_text=text)
    first = prepare("first", TEXT)
    assert first["native_user_text"] == TEXT
    assert prepare("first", TEXT) == first
    with pytest.raises(ValueError, match="original user input"):
        prepare("first", "different")
    session.validate_result(reply(scope))
    with pytest.raises(ValueError, match="original user input"):
        prepare("next", None)
    following = prepare("next", TEXT)
    assert "native_user_text" not in following and "native_continuation" in following


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_actual_client_transports_text_separately_without_replacing_prompt(stream):
    client = object.__new__(OpenVegasClient)
    client.base_url, client.token = "https://synthetic.invalid", "fixture"
    client._request = AsyncMock(return_value={"text": "ok"})
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text='event: response.completed\ndata: {"text":"ok"}\n\n')

    request = initial()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client._http_client = http
        if stream:
            assert [e async for e in client.ask_stream(**request)]
            payload = captured[0]
        else:
            await client.ask(**request)
            payload = client._request.call_args.kwargs["json"]
    assert payload["prompt"] == request["prompt"]
    assert payload["native_user_text"] == TEXT


def test_settings_without_original_input_remain_byte_compatible():
    old = initial()
    old.pop("native_user_text")
    req = AskRequest(**old)
    attrs = {key: getattr(req, key) for key in ("provider", "model", "prompt", "enable_tools",
                                             "enable_web_search", "reasoning_effort", "attachments")}
    assert frozen_settings(req, max_tokens=1024) == frozen_settings(SimpleNamespace(**attrs), max_tokens=1024)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.parametrize("failure", ["secret", "large", "scope", "continuation", "malformed_json"])
async def test_inference_validation_never_reflects_private_input(monkeypatch, endpoint, failure):
    from server.middleware.auth import get_current_user
    from server.routes import inference as routes

    app = FastAPI()
    app.include_router(routes.router, prefix="/inference")
    app.dependency_overrides[get_current_user] = lambda: {"user_id": str(uuid4())}
    prepare = AsyncMock(side_effect=AssertionError("Invalid input reached inference preparation"))
    monkeypatch.setattr(routes, "_prepare_ask_context", prepare)
    request = initial()
    request["prompt"] = "PRIVATE_PROMPT_CANARY"
    if failure == "secret":
        request["native_user_text"] = "password=synthetic_private_canary"
    elif failure == "large":
        request["native_user_text"] = "PRIVATE_TEXT_CANARY" * 4000
    elif failure == "scope":
        request["native_scope"] = {"run_id": "PRIVATE_SCOPE_CANARY"}
    elif failure == "continuation":
        request["native_continuation"] = {"previous_inference_request_id": str(uuid4()),
                                          "expected_history_revision": 0}
    body = json.dumps(request).encode() if failure != "malformed_json" else b'{"prompt":"PRIVATE_JSON_CANARY"'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post("/inference/" + endpoint, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {"detail": "Invalid inference request. Check the selected options and input limits."}
    assert "canary" not in response.text.lower() and "password" not in response.text
    prepare.assert_not_awaited()
