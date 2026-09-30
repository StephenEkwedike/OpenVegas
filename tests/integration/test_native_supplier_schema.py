"""Offline checks for synthetic supplier fixtures; no PostgreSQL connection."""
import copy
import json

import pytest

from openvegas.contracts.native_tool_schema import FLAT_V2, GENERIC_V1, GOOGLE_FLAT_V1
from openvegas.gateway import openrouter
from openvegas.gateway.inference import AIGateway
from tests.integration.native_supplier_schema import tool_function
from tests.test_models.test_openrouter_versioned_tools import (
    CONFIG,
    assistant,
    continuation,
    request,
    response,
)


@pytest.mark.parametrize(("version", "model"), [
    (FLAT_V2, "openai/schema-fixture"),
    (GENERIC_V1, "openai/schema-fixture"),
    (GOOGLE_FLAT_V1, "google/schema-fixture"),
])
@pytest.mark.parametrize(("name", "args", "mode"), [
    ("Read", {"path": "notes.txt"}, "read_only"),
    ("Search", {"pattern": "needle"}, "read_only"),
    ("List", {}, "read_only"),
    ("Write", {"filepath": "notes.txt", "content": "after\n"}, "mutating"),
    ("InsertAtEnd", {"filepath": "notes.txt", "content": "tail"}, "mutating"),
    ("FindAndReplace", {"filepath": "notes.txt", "old_string": "before", "new_string": "after"}, "mutating"),
    ("Bash", {"command": "printf synthetic"}, "mutating"),
])
def test_supplier_uses_dispatched_version_without_changing_public_call(version, model, name, args, mode):
    req = request(model) if version == FLAT_V2 else continuation(version, model)[0]
    payload = {"tools": openrouter._versioned_tool_definitions(model, version)}
    before = copy.deepcopy(payload)
    call = {"tool_name": name, "arguments": args, "shell_mode": mode, "timeout_sec": 30}
    message = assistant(version)
    function = tool_function(payload, call)
    message["tool_calls"][0]["function"] = function
    parsed = openrouter.parse_response(response(req, message), req, CONFIG, AIGateway._parse_local_tool_call)
    assert parsed["tool_calls"] == [{**call, "provider_call_id": "call-original"}]
    assert payload == before
    if version == GENERIC_V1:
        assert function == {"name": "call_local_tool", "arguments": json.dumps(call)}
    else:
        assert function["name"] == name
        assert "tool_name" not in json.loads(function["arguments"])


def test_supplier_ignores_web_prefix_but_never_invents_missing_local_tool():
    call = {"tool_name": "Read", "arguments": {"path": "notes.txt"}}
    web = {"type": "openrouter:web_search"}
    payload = {"tools": [web, *openrouter.local_tool_definitions("openai/schema-fixture")]}
    assert tool_function(payload, call)["name"] == "Read"
    with pytest.raises(AssertionError, match="dispatched function"):
        tool_function({"tools": [web]}, call)
