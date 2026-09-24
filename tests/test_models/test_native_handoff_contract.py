"""Offline public handoff DTO checks; no routes, provider calls or authorization."""
from __future__ import annotations

import ast
import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from openvegas.contracts import native_handoff as contract
from openvegas.contracts.native_handoff import (
    ConfirmNativeHandoff,
    NativeHandoffRef,
    NativeHandoffResponse,
    NativeHandoffSelection,
    PrepareNativeHandoff,
)
from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope

RUN = "11111111-aaaa-4111-8111-111111111111"
SESSION = "22222222-bbbb-4222-8222-222222222222"
REQUEST = "33333333-cccc-4333-8333-333333333333"
HANDOFF = "44444444-dddd-4444-8444-444444444444"
SCOPE = {"run_id": RUN, "runtime_session_id": SESSION, "expected_run_version": 0,
         "expected_valid_actions_signature": "sha256:" + "a" * 64}
SOURCE_REF = {"previous_inference_request_id": REQUEST, "expected_history_revision": 0}
SELECTION = {"model": "fixture/exact-v1", "max_tokens": 4096}
REFERENCE = {"handoff_id": HANDOFF, "handoff_sha256": "b" * 64}
SENTINEL = "PRIVATE_PAYLOAD_MUST_NOT_APPEAR"
MODELS = [NativeHandoffSelection, PrepareNativeHandoff, NativeHandoffRef,
          ConfirmNativeHandoff, NativeHandoffResponse]


def incoming(model):
    values = {
        NativeHandoffSelection: SELECTION,
        PrepareNativeHandoff: {"source_scope": SCOPE, "source_ref": SOURCE_REF,
                              "selection": SELECTION, "idempotency_key": "prepare-1"},
        NativeHandoffRef: REFERENCE,
        ConfirmNativeHandoff: {**REFERENCE, "destination_scope": SCOPE,
                              "idempotency_key": "confirm-1"},
        NativeHandoffResponse: {**REFERENCE, "selection": SELECTION,
            "expires_at": "2026-09-24T12:30:00+00:00", "task_count": 2,
            "file_count": 3, "unique_file_count": 2, "observation_count": 4},
    }
    return deepcopy(values[model])


@pytest.mark.parametrize("model", MODELS)
def test_exact_roundtrip_and_frozen_snapshot(model):
    raw = incoming(model)
    parsed = model.model_validate(raw)
    dumped = parsed.model_dump(mode="json")
    assert model.model_validate_json(parsed.model_dump_json()) == parsed
    assert model.model_validate(dumped) == parsed
    assert TypeAdapter(model).validate_json(parsed.model_dump_json()) == parsed
    raw.clear()
    assert parsed.model_dump(mode="json") == dumped
    with pytest.raises(ValidationError):
        setattr(parsed, next(iter(model.model_fields)), "changed")


def test_prepare_confirm_and_response_are_exact_allowlists():
    prepare = PrepareNativeHandoff.model_validate(incoming(PrepareNativeHandoff))
    assert type(prepare.source_scope) is NativeInferenceScope
    assert type(prepare.source_ref) is NativeContinuationRef
    assert set(prepare.model_dump()) == {"source_scope", "source_ref", "selection", "idempotency_key"}
    assert prepare.selection.model_dump() == {
        "provider": "openrouter", "model": "fixture/exact-v1", "enable_tools": True,
        "enable_web_search": False, "reasoning_effort": None, "max_tokens": 4096,
    }
    assert prepare.idempotency_key not in repr(prepare)
    confirmed = ConfirmNativeHandoff.model_validate(incoming(ConfirmNativeHandoff))
    assert set(confirmed.model_dump()) == {
        "handoff_id", "handoff_sha256", "destination_scope", "idempotency_key",
    }
    assert confirmed.idempotency_key not in repr(confirmed)
    response = NativeHandoffResponse.model_validate(incoming(NativeHandoffResponse))
    assert response.destination_scope is None
    assert set(response.model_dump()) == {
        "handoff_id", "handoff_sha256", "selection", "expires_at", "task_count",
        "file_count", "unique_file_count", "observation_count", "destination_scope",
    }
    response = NativeHandoffResponse.model_validate({**incoming(NativeHandoffResponse),
                                                    "destination_scope": SCOPE})
    assert response.destination_scope.model_dump() == SCOPE


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("extra", [
    "user_id", "document", "messages", "history", "system", "tool_calls", "tool_results",
    "reasoning_details", "signature", "binding", "owner_token", "first_dispatch_json",
    "review_fingerprint", "attachment_refs", "_native_handoff_binding",
])
def test_private_and_history_fields_are_rejected(model, extra):
    with pytest.raises(ValidationError):
        model.model_validate({**incoming(model), extra: SENTINEL})


@pytest.mark.parametrize("path", ["source_scope", "source_ref", "selection"])
def test_nested_private_fields_are_rejected(path):
    raw = incoming(PrepareNativeHandoff)
    raw[path]["binding"] = SENTINEL
    with pytest.raises(ValidationError):
        PrepareNativeHandoff.model_validate(raw)


@pytest.mark.parametrize("max_tokens", [0, -1, 1_000_001, True, False, 1.0, "1", None])
def test_output_budget_requires_exact_bounded_positive_integer(max_tokens):
    with pytest.raises(ValidationError):
        NativeHandoffSelection.model_validate({**SELECTION, "max_tokens": max_tokens})


def test_output_budget_is_required_and_endpoints_are_preserved():
    with pytest.raises(ValidationError):
        NativeHandoffSelection(model="fixture/exact-v1")
    for count in (1, 1_000_000):
        assert NativeHandoffSelection(**{**SELECTION, "max_tokens": count}).max_tokens == count


@pytest.mark.parametrize("name", ["enable_tools", "enable_web_search"])
@pytest.mark.parametrize("value", [0, 1, "true", "false", None, [], {}])
def test_booleans_are_not_coerced(name, value):
    with pytest.raises(ValidationError):
        NativeHandoffSelection.model_validate({**SELECTION, name: value})


def test_tools_cannot_be_disabled_but_web_is_optional():
    with pytest.raises(ValidationError):
        NativeHandoffSelection(**SELECTION, enable_tools=False)
    for enabled in (False, True):
        assert NativeHandoffSelection(**SELECTION, enable_web_search=enabled).enable_web_search is enabled


@pytest.mark.parametrize("provider", ["openai", "OpenRouter", "openrouter ", "", True, 1, None])
def test_only_known_provider_is_accepted(provider):
    with pytest.raises(ValidationError):
        NativeHandoffSelection(**SELECTION, provider=provider)


@pytest.mark.parametrize("model", [
    "fixture/exact-v1", "vendor/Exact-V2.1", "a" * 64 + "/" + "B" * 128,
    "fixture/latest", "fixture/v1-auto", "fixture/ROUTER_v2", "openrouter/exact-v1",
    "fixture/model:online", "fixture/model:free", "fixture/model\n", " fixture/model",
    "Vendor/model", "fixture/a/b", "a" * 65 + "/model", "fixture/" + "m" * 129,
    "fixture/m\u00f6del", "fixture/", "model", "", None, 1,
])
def test_exact_model_syntax_matches_existing_managed_adapter(model):
    from openvegas.gateway.openrouter import valid_model

    if valid_model(model):
        assert NativeHandoffSelection(**{**SELECTION, "model": model}).model == model
    else:
        with pytest.raises(ValidationError):
            NativeHandoffSelection(**{**SELECTION, "model": model})


def test_reasoning_allowlist_matches_existing_contract():
    from openvegas.capabilities import REASONING_EFFORTS

    assert get_args(contract._ReasoningEffort) == REASONING_EFFORTS
    for effort in (*REASONING_EFFORTS, None):
        assert NativeHandoffSelection(**SELECTION, reasoning_effort=effort).reasoning_effort == effort


@pytest.mark.parametrize("effort", ["", "HIGH", "automatic", True, 1, [], {}])
def test_reasoning_rejects_unknown_or_coerced_values(effort):
    with pytest.raises(ValidationError):
        NativeHandoffSelection(**SELECTION, reasoning_effort=effort)


@pytest.mark.parametrize("model", [PrepareNativeHandoff, ConfirmNativeHandoff])
@pytest.mark.parametrize("key", ["", " ", "two words", " leading", "trailing ", "line\n",
                                 "tab\t", "null\x00", "del\x7f", "\u00e9", "k" * 201, 1, None])
def test_keys_match_strict_storage_contract(model, key):
    with pytest.raises(ValidationError):
        model.model_validate({**incoming(model), "idempotency_key": key})


@pytest.mark.parametrize("model", [PrepareNativeHandoff, ConfirmNativeHandoff])
def test_key_bytes_are_preserved_at_both_bounds(model):
    for key in ("!", "k" * 200, "prepare:one/2~"):
        assert model.model_validate({**incoming(model), "idempotency_key": key}).idempotency_key == key


@pytest.mark.parametrize("model", [NativeHandoffRef, ConfirmNativeHandoff, NativeHandoffResponse])
@pytest.mark.parametrize("bad", ["00000000-0000-0000-0000-000000000000", HANDOFF.upper(),
    HANDOFF.replace("-", ""), "{" + HANDOFF + "}", HANDOFF + "\n", "urn:uuid:" + HANDOFF, 1, None])
def test_handoff_ids_are_canonical_and_nonzero(model, bad):
    with pytest.raises(ValidationError):
        model.model_validate({**incoming(model), "handoff_id": bad})


@pytest.mark.parametrize("bad", ["0" * 64, "A" * 64, "g" * 64, "b" * 63, "b" * 65,
                                "sha256:" + "b" * 64, "b" * 64 + "\n", "", True, None])
def test_handoff_digest_is_exact_lowercase_nonzero_sha256(bad):
    with pytest.raises(ValidationError):
        NativeHandoffRef(**{**REFERENCE, "handoff_sha256": bad})


@pytest.mark.parametrize("field,bad", [
    ("run_id", RUN.upper()), ("runtime_session_id", "00000000-0000-0000-0000-000000000000"),
    ("expected_run_version", True), ("expected_run_version", "0"),
    ("expected_run_version", -1), ("expected_run_version", 2**63),
    ("expected_valid_actions_signature", "sha256:" + "A" * 64),
])
@pytest.mark.parametrize("model,slot", [(PrepareNativeHandoff, "source_scope"),
                                       (ConfirmNativeHandoff, "destination_scope"),
                                       (NativeHandoffResponse, "destination_scope")])
def test_embedded_scopes_reuse_strict_native_validation(model, slot, field, bad):
    with pytest.raises(ValidationError):
        model.model_validate({**incoming(model), slot: {**SCOPE, field: bad}})


@pytest.mark.parametrize("field,bad", [
    ("previous_inference_request_id", REQUEST.upper()),
    ("expected_history_revision", -1), ("expected_history_revision", True),
    ("expected_history_revision", "0"), ("expected_history_revision", 2**63 - 1),
])
def test_source_ref_reuses_native_continuation_validation(field, bad):
    with pytest.raises(ValidationError):
        PrepareNativeHandoff.model_validate({**incoming(PrepareNativeHandoff),
                                            "source_ref": {**SOURCE_REF, field: bad}})


@pytest.mark.parametrize("field,forged", [
    ("source_scope", NativeInferenceScope.model_construct(**{**SCOPE, "expected_run_version": True})),
    ("source_ref", NativeContinuationRef.model_construct(**{**SOURCE_REF, "expected_history_revision": -1})),
    ("selection", NativeHandoffSelection.model_construct(**{**SELECTION, "max_tokens": True})),
])
def test_preconstructed_nested_models_are_revalidated(field, forged):
    with pytest.raises(ValidationError):
        PrepareNativeHandoff.model_validate({**incoming(PrepareNativeHandoff), field: forged})


@pytest.mark.parametrize("expiry", [datetime(2026, 9, 24), date(2026, 9, 24),  # noqa: DTZ001 - rejection fixture
    "2026-09-24T12:30:00", "2026-09-24", "1720000000", 1720000000, 1.5, True, None,
    "not-a-date", "2026-09-24T12:30:00+25:00", "x" * 41])
def test_expiry_requires_aware_datetime_or_iso_timestamp(expiry):
    with pytest.raises(ValidationError):
        NativeHandoffResponse.model_validate({**incoming(NativeHandoffResponse), "expires_at": expiry})


def test_expiry_normalizes_utc_without_renewing_expired_replay():
    old = datetime(2000, 1, 1, 1, tzinfo=timezone(timedelta(hours=1)))
    for expiry in (old, old.isoformat(), "2000-01-01T00:00:00Z"):
        parsed = NativeHandoffResponse.model_validate({**incoming(NativeHandoffResponse), "expires_at": expiry})
        assert parsed.expires_at == datetime(2000, 1, 1, tzinfo=UTC)
        assert parsed.expires_at.tzinfo is UTC


@pytest.mark.parametrize("field,maximum", [("task_count", 32), ("file_count", 12),
                                         ("unique_file_count", 8), ("observation_count", 128)])
@pytest.mark.parametrize("kind", ["negative", "too_large", "boolean", "string", "float"])
def test_counts_are_bounded_strict_integers(field, maximum, kind):
    bad = {"negative": -1, "too_large": maximum + 1, "boolean": True, "string": "1", "float": 1.0}[kind]
    with pytest.raises(ValidationError):
        NativeHandoffResponse.model_validate({**incoming(NativeHandoffResponse), field: bad})


@pytest.mark.parametrize("changes", [{"task_count": 0}, {"unique_file_count": 4},
    {"file_count": 0}, {"unique_file_count": 0}, {"task_count": 1}])
def test_impossible_file_and_task_counts_are_rejected(changes):
    with pytest.raises(ValidationError):
        NativeHandoffResponse.model_validate({**incoming(NativeHandoffResponse), **changes})


def test_empty_files_and_exact_aggregate_bounds_are_valid():
    for counts in ({"task_count": 1, "file_count": 0, "unique_file_count": 0, "observation_count": 0},
                   {"task_count": 32, "file_count": 12, "unique_file_count": 8, "observation_count": 128}):
        parsed = NativeHandoffResponse.model_validate({**incoming(NativeHandoffResponse), **counts})
        assert all(getattr(parsed, key) == value for key, value in counts.items())


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("method", ["constructor", "python", "json", "adapter"])
def test_validation_errors_do_not_echo_values_or_unknown_field_names(model, method):
    raw = {**incoming(model), SENTINEL: {"system": SENTINEL}}
    calls = {
        "constructor": lambda: model(**raw), "python": lambda: model.model_validate(raw),
        "json": lambda: model.model_validate_json(json.dumps(raw)),
        "adapter": lambda: TypeAdapter(model).validate_python(raw),
    }
    with pytest.raises(ValidationError) as caught:
        calls[method]()
    for rendered in (str(caught.value), repr(caught.value), repr(caught.value.errors()), caught.value.json()):
        assert SENTINEL not in rendered
    assert caught.value.errors()[0]["input"] is None
    assert caught.value.errors()[0]["loc"] == ()


@pytest.mark.parametrize("location", ["source_scope", "source_ref", "selection"])
def test_nested_validation_errors_are_sanitized(location):
    raw = incoming(PrepareNativeHandoff)
    raw[location] = {SENTINEL: SENTINEL}
    with pytest.raises(ValidationError) as caught:
        PrepareNativeHandoff.model_validate(raw)
    assert SENTINEL not in caught.value.json()
    assert caught.value.errors()[0]["input"] is None


@pytest.mark.parametrize("model", MODELS)
def test_json_syntax_errors_do_not_echo_input(model):
    with pytest.raises(ValidationError) as caught:
        model.model_validate_json('{"' + SENTINEL + '":')
    assert SENTINEL not in caught.value.json()
    assert caught.value.errors()[0]["input"] is None


def test_contract_has_no_server_storage_or_provider_imports():
    tree = ast.parse(Path(contract.__file__).read_text())
    modules = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    modules += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    assert not any(name.startswith(("server", "openvegas.agent", "openvegas.gateway")) for name in modules)
