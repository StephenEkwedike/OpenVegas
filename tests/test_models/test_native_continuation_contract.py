"""Wire validation and old idempotency bytes; no provider/network calls."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from openvegas.contracts.errors import ContractError
from openvegas.contracts.native_scope import NativeContinuationRef
from server.routes.inference import AskRequest
from server.services.inference_replay import command_fingerprint


def command():
    return {"prompt": "Read notes", "provider": "openrouter", "model": "fixture/native-v1",
            "enable_tools": True, "persist_context": False, "conversation_mode": "ephemeral",
            "native_scope": {"run_id": str(uuid4()), "runtime_session_id": str(uuid4()),
                             "expected_run_version": 1,
                             "expected_valid_actions_signature": "sha256:" + "a" * 64}}


def test_optional_fields_do_not_change_legacy_or_ownership_fingerprints():
    for incoming in ({"prompt": "Hi", "provider": "openrouter", "model": "fixture/native-v1"}, command()):
        expected = command_fingerprint(incoming)
        assert command_fingerprint({**incoming, "native_history": False, "native_continuation": None}) == expected


def test_history_and_revision_are_part_of_idempotency_fingerprint():
    incoming = command()
    old = command_fingerprint(incoming)
    first = {**incoming, "native_history": True}
    assert command_fingerprint(first) != old
    ref = {"previous_inference_request_id": str(uuid4()), "expected_history_revision": 0}
    second = {**first, "native_continuation": ref}
    assert command_fingerprint(second) != command_fingerprint(first)
    assert command_fingerprint({**second, "native_continuation": {**ref, "expected_history_revision": 1}}) != command_fingerprint(second)


@pytest.mark.parametrize("revision", [-1, True, 0.0, "0", 2**63 - 1])
def test_continuation_revision_is_strict_and_bounded(revision):
    with pytest.raises(ValidationError):
        NativeContinuationRef(previous_inference_request_id=str(uuid4()), expected_history_revision=revision)


@pytest.mark.parametrize("extra", ["messages", "reasoning_details", "tool_results"])
def test_private_state_not_accepted_in_dto(extra):
    with pytest.raises(ValidationError):
        NativeContinuationRef.model_validate({"previous_inference_request_id": str(uuid4()),
                                             "expected_history_revision": 0, extra: []})


def test_scope_and_history_required_before_continuation():
    incoming = {**command(), "idempotency_key": "fresh"}
    ref = {"previous_inference_request_id": str(uuid4()), "expected_history_revision": 0}
    with pytest.raises(ValidationError):
        AskRequest(**incoming, native_continuation=ref)
    with pytest.raises(ContractError):
        command_fingerprint({**command(), "native_continuation": ref})
    with pytest.raises(ValidationError):
        AskRequest(**{**incoming, "native_scope": None}, native_history=True)
    assert AskRequest(**incoming, native_history=True, native_continuation=ref).native_continuation.expected_history_revision == 0


@pytest.mark.parametrize("bad", ["true", 1, None])
def test_native_history_is_a_strict_explicit_boolean(bad):
    with pytest.raises(ValidationError):
        AskRequest(**command(), idempotency_key="fresh", native_history=bad)
