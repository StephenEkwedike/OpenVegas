"""Owned-envelope projection contracts; no database or provider transport."""
import copy
import json

import pytest

from openvegas.agent.native_history import _original_call_projection
from openvegas.agent.native_mutation_service import _schema
from openvegas.contracts.errors import ContractError
from openvegas.contracts.native_tool_schema import FLAT_V2, GENERIC_V1, GOOGLE_FLAT_V1
from openvegas.gateway.openrouter import _versioned_tool_definitions


def payload(model, version):
    return {"model": model, "tools": _versioned_tool_definitions(model, version)}


def raw_call(name, args):
    return {"id": "original-call", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


@pytest.mark.parametrize(("model", "version"), [
    ("openai/schema-fixture", FLAT_V2),
    ("anthropic/schema-fixture", FLAT_V2),
    ("mistralai/schema-fixture", FLAT_V2),
    ("google/schema-fixture", FLAT_V2),
    ("google/schema-fixture", GOOGLE_FLAT_V1),
    ("openai/schema-fixture", GENERIC_V1),
])
@pytest.mark.parametrize(("name", "args", "mode"), [
    ("Read", {"path": "notes.txt"}, "read_only"),
    ("Write", {"filepath": "notes.txt", "content": "after\n"}, "mutating"),
])
def test_projection_uses_original_schema(model, version, name, args, mode):
    request = payload(model, version)
    expected = {"tool_name": name, "arguments": args, "shell_mode": mode, "timeout_sec": 30}
    raw = raw_call("call_local_tool", expected) if version == GENERIC_V1 else raw_call(name, args)
    before = copy.deepcopy((raw, request))
    assert _original_call_projection(raw, model, request_payload=request) == {
        **expected, "provider_call_id": "original-call"}
    assert (raw, request) == before
    definition = next(d["function"] for d in request["tools"]
                      if d["function"]["name"] == raw["function"]["name"])
    _schema(json.loads(raw["function"]["arguments"]), definition["parameters"])


@pytest.mark.parametrize("change", ["unknown", "model", "wire", "bool_bound", "float_bound", "extra", "missing"])
def test_projection_rejects_unknown_or_mismatched_original_schema(change):
    model = "openai/schema-fixture"
    request = payload(model, FLAT_V2)
    raw = raw_call("Read", {"path": "notes.txt"})
    if change == "unknown":
        request["tools"][0]["function"]["description"] = "changed"
    elif change == "model":
        request["model"] = "anthropic/schema-fixture"
    elif change == "wire":
        raw = raw_call("call_local_tool", {"tool_name": "Read", "arguments": {"path": "notes.txt"}})
    elif change in {"bool_bound", "float_bound"}:
        request["tools"][0]["function"]["parameters"]["properties"]["max_bytes"]["minimum"] = (
            True if change == "bool_bound" else 1.0)
    elif change == "extra":
        request["tools"].append({"type": "function", "function": {"name": "Unknown"}})
    else:
        del request["tools"]
    with pytest.raises(ContractError):
        _original_call_projection(raw, model, request_payload=request)


def test_flat_response_cannot_select_flat_schema_for_retained_generic():
    model = "openai/schema-fixture"
    with pytest.raises(ContractError):
        _original_call_projection(raw_call("Read", {"path": "notes.txt"}), model,
                                  request_payload=payload(model, GENERIC_V1))


@pytest.mark.parametrize(("model", "version"), [
    ("openai/schema-fixture", FLAT_V2), ("google/schema-fixture", GOOGLE_FLAT_V1),
    ("openai/schema-fixture", GENERIC_V1),
])
@pytest.mark.parametrize("size", [18000, 32000, 32001])
def test_original_wire_byte_bound_not_reserialized_bound(model, version, size):
    args = {"filepath": "notes.txt", "content": "\u754c" * 5000}
    wire = {"tool_name": "Write", "arguments": args, "shell_mode": "mutating"} if version == GENERIC_V1 else args
    raw = raw_call("call_local_tool" if version == GENERIC_V1 else "Write", wire)
    # Whitespace remains part of the original wire byte limit, even though a
    # compact JSON reconstruction would fall below it. Unicode is not escaped.
    raw["function"]["arguments"] += " " * (size - len(raw["function"]["arguments"].encode("utf-8")))
    assert len(raw["function"]["arguments"].encode("utf-8")) == size
    if size > 32000:
        with pytest.raises(ContractError):
            _original_call_projection(raw, model, request_payload=payload(model, version))
    else:
        result = _original_call_projection(raw, model, request_payload=payload(model, version))
        assert result["arguments"] == args


def test_mutation_schema_enforces_minimum_string_length():
    with pytest.raises(ContractError):
        _schema("", {"type": "string", "minLength": 1})
    _schema("", {"type": "string"})
