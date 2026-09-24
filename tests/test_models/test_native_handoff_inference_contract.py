"""Explicit handoff identity and budget, without changing legacy replay keys."""
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from openvegas.agent.native_continuation import (
    apply_request_history,
    check_options,
    frozen_settings,
)
from openvegas.agent.native_envelope import history_inputs
from openvegas.contracts.errors import ContractError
from openvegas.gateway.inference import InferenceRequest
from server.routes.inference import AskRequest
from server.services.inference_replay import command_fingerprint
from tests.test_models.test_native_generation_ownership import rows
from tests.test_models.test_native_user_input import initial


def command():
    return {**initial(), "native_handoff": {"handoff_id": str(uuid4()), "handoff_sha256": "b" * 64},
            "max_tokens": 100}


def fingerprint(value):
    return command_fingerprint({k: v for k, v in value.items() if k != "idempotency_key"})


def test_legacy_null_fields_do_not_change_original_replay_identity():
    original = initial()
    assert fingerprint(original) == fingerprint({**original, "native_handoff": None, "max_tokens": None})


def test_handoff_identity_and_reviewed_budget_are_exact_fingerprint_inputs():
    value = command()
    req = AskRequest(**value)
    assert fingerprint(value) == fingerprint(req.model_dump(mode="json"))
    for change in ({"max_tokens": 101}, {"native_handoff": {**value["native_handoff"], "handoff_sha256": "c" * 64}},
                   {"native_handoff": {**value["native_handoff"], "handoff_id": str(uuid4())}}):
        assert fingerprint(value) != fingerprint({**value, **change})


@pytest.mark.parametrize("budget", [None, 0, -1, True, 1.5, "100", 1_000_001])
def test_invalid_handoff_budget_is_rejected_at_both_boundaries(budget):
    value = {**command(), "max_tokens": budget}
    with pytest.raises(ValidationError):
        AskRequest(**value)
    with pytest.raises(ContractError):
        fingerprint(value)


@pytest.mark.parametrize("change", [{"native_handoff": None}, {"native_history": False}, {"native_scope": None}])
def test_handoff_is_not_a_legacy_or_unscoped_option(change):
    value = {**command(), **change}
    with pytest.raises(ValidationError):
        AskRequest(**value)
    with pytest.raises(ContractError):
        fingerprint(value)


def test_first_handoff_requires_original_user_input():
    with pytest.raises(ValidationError):
        AskRequest(**{**command(), "native_user_text": None})


@pytest.mark.parametrize("field", ["enable_tools", "enable_web_search", "persist_context"])
@pytest.mark.parametrize("value", [0, 1, "false", "true"])
def test_handoff_flags_require_literal_booleans_before_model_coercion(field, value):
    with pytest.raises(ValidationError):
        AskRequest(**{**command(), field: value})
    with pytest.raises(ContractError):
        fingerprint({**command(), field: value})


@pytest.mark.parametrize("change", [{"max_tokens": 101}, {"native_handoff": None}])
def test_continuation_cannot_change_handoff_or_budget(change):
    value = command()
    settings = frozen_settings(AskRequest(**value), max_tokens=100)
    check_options(value, {"settings": settings})
    with pytest.raises(ContractError):
        check_options({**value, **change}, {"settings": settings})


@pytest.mark.asyncio
async def test_route_preparation_retains_exact_incoming_private_input():
    value = command()
    req = AskRequest(**value)
    settings = frozen_settings(req, max_tokens=100)
    incoming = {**value["native_handoff"], "document_sha256": "d" * 64}
    inputs = history_inputs(attachment_refs=[], settings=settings, incoming_handoff=incoming)
    _, claim, _ = rows()
    payload = {"messages": [{"role": "user", "content": "Original owned history"}]}
    claim = replace(claim, history_revision=1, continuation_payload_json=json.dumps(payload),
                    history_inputs_json=inputs._json)
    request = InferenceRequest("user:" + claim.user_id, req.provider, req.model, [], max_tokens=100)
    prepared = SimpleNamespace(req=req, inference_request=request, attachment_refs=[])
    await apply_request_history(prepared, claim)
    assert request.messages == payload["messages"]
    assert request._native_history_inputs._json == inputs._json
    prepared.req = req.model_copy(update={"native_handoff": None})
    with pytest.raises(ContractError):
        await apply_request_history(prepared, claim)
