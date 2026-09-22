import json

import pytest

from openvegas.cli import _collect_tool_call_candidates, _preprocess_tool_request_for_runtime


def normalized(**changes):
    return {"tool_name": "Read", "arguments": {"path": "fixture.txt"},
            "shell_mode": "read_only", "timeout_sec": 30, **changes}


def test_normalized_call_identity_survives_without_changing_arguments():
    source = normalized(provider_call_id="call_abc-123:0")
    result = _collect_tool_call_candidates([source], "")
    assert result == [{"type": "tool_call", **source}]


def test_native_function_call_identity_survives_normalization():
    source = {"id": "call-native", "type": "function", "function": {
        "name": "call_local_tool", "arguments": json.dumps(normalized())}}
    result = _collect_tool_call_candidates([source], "")
    assert result == [{"type": "tool_call", **normalized(), "provider_call_id": "call-native"}]


@pytest.mark.parametrize("value", [None, "", 1, {}, "x" * 257, "call\nunsafe", "sk-test-secret"])
def test_untrusted_identity_is_not_promoted_to_provenance(value):
    result = _collect_tool_call_candidates([normalized(provider_call_id=value)], "")
    assert result == [{"type": "tool_call", **normalized()}]


def test_text_fallback_never_manufactures_provider_identity():
    result = _collect_tool_call_candidates(None, json.dumps({"type": "tool_call", **normalized()}))
    assert len(result) == 1
    assert "provider_call_id" not in result[0]


@pytest.mark.parametrize("identity", ["call-invented", "sk-not-a-real-key", "x" * 257, "call\nunsafe"])
def test_text_fallback_explicit_id_is_removed_before_preprocessing(tmp_path, identity):
    raw = {"type": "tool_call", **normalized(provider_call_id=identity, provider_request_id=identity)}
    result = _collect_tool_call_candidates(None, json.dumps(raw))
    assert len(result) == 1
    assert "provider_call_id" not in result[0] and "provider_request_id" not in result[0]
    prepared, error = _preprocess_tool_request_for_runtime(
        tool_req=result[0], tool_observations=[], workspace_root=str(tmp_path), user_message="Read fixture.txt", model_text="",
    )
    assert error is None
    assert "provider_call_id" not in prepared


@pytest.mark.parametrize("native", [False, True])
def test_call_identity_survives_actual_runtime_preprocessing(tmp_path, native):
    source = normalized(provider_call_id="call-native:1")
    if native:
        source = {"id": "call-native:1", "type": "function", "function": {
            "name": "call_local_tool", "arguments": json.dumps(normalized())}}
    candidate = _collect_tool_call_candidates([source], "")[0]
    prepared, error = _preprocess_tool_request_for_runtime(
        tool_req=candidate, tool_observations=[], workspace_root=str(tmp_path), user_message="Read fixture.txt", model_text="",
    )
    assert error is None
    assert prepared["provider_call_id"] == "call-native:1"
    assert prepared["tool_name"] == "fs_read"
    assert prepared["arguments"]["path"] == "fixture.txt"
    assert "provider_call_id" not in prepared["arguments"]
    assert "tool_call_id" not in prepared and "approval_id" not in prepared


def test_native_explicit_empty_arguments_do_not_nest_the_envelope():
    source = {"id": "call-list", "type": "function", "function": {
        "name": "call_local_tool", "arguments": json.dumps({"tool_name": "List", "arguments": {}})}}
    candidate = _collect_tool_call_candidates([source], "")[0]
    assert candidate["arguments"] == {}
    assert candidate["provider_call_id"] == "call-list"
