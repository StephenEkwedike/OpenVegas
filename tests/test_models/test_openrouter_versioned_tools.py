"""Offline flat-v2 and exact retained-v1 contracts. No provider/credential access."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from openvegas.agent.native_envelope import NativeHistoryInputs
from openvegas.agent.native_generation import NativeGenerationClaim
from openvegas.contracts.errors import ContractError
from openvegas.contracts.native_scope import NativeInferenceScope
from openvegas.contracts.native_tool_schema import FLAT_V2, GENERIC_V1, GOOGLE_FLAT_V1
from openvegas.gateway import openrouter
from openvegas.gateway.inference import AIGateway, InferenceRequest

CONFIG = {"max_tokens": 512, "cost_input_per_1m": "1", "cost_output_per_1m": "2"}
CAPS = {"tools": True, "context_window_tokens": 64000}
PRIVATE = "PRIVATECANARY-opaque-original-signature"
KEY = "synthetic-not-a-live-credential"
FAMILIES = ("openai", "anthropic", "google", "mistralai")
OPERATIONS = ("Read", "Search", "Write", "FindAndReplace", "InsertAtEnd", "Bash", "List")


def request(model="openai/schema-fixture", *, native=False):
    req = InferenceRequest("user:" + str(uuid4()), "openrouter", model,
                           [{"role": "user", "content": "Read fixture.txt"}],
                           max_tokens=128, enable_tools=True, idempotency_key=str(uuid4()))
    req._native_history_required = native
    req._native_history_inputs = NativeHistoryInputs(json.dumps({
        "attachment_refs": [], "settings": {"prompt": "Read fixture.txt"}}))
    return req


def claim(req, payload):
    scope = NativeInferenceScope(run_id=str(uuid4()), runtime_session_id=str(uuid4()),
        expected_run_version=1, expected_valid_actions_signature="sha256:" + "a" * 64)
    req._native_history_required = True
    req._native_generation_claim = NativeGenerationClaim(
        req.account_id.removeprefix("user:"), str(uuid4()), scope, scope.model_dump_json(),
        "synthetic-owner", "a" * 64, req.idempotency_key, history_revision=1,
        previous_request_id=str(uuid4()), continuation_payload_json=json.dumps(payload))


def assistant(version, name="Read", args=None):
    args = {"path": "fixture.txt"} if args is None else args
    raw = json.dumps({"tool_name": name, "arguments": args} if version == GENERIC_V1 else args,
                     ensure_ascii=False, indent=2)
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-original", "type": "function",
        "function": {"name": "call_local_tool" if version == GENERIC_V1 else name, "arguments": raw},
        "extra_content": {"provider": {"signature": PRIVATE}},
    }], "reasoning_details": [{"type": "reasoning.encrypted", "data": PRIVATE, "index": 0}]}


def continuation(version, model="openai/schema-fixture", *, req=None, config=None, caps=None):
    req = request(model, native=True) if req is None else req
    payload = copy.deepcopy(openrouter.build_payload(req, config or CONFIG, caps or CAPS))
    local = openrouter._versioned_tool_definitions(req.model, version)
    payload["tools"] = ([payload["tools"][0]] if req.enable_web_search else []) + local
    payload["messages"] += [assistant(version), {"role": "tool", "tool_call_id": "call-original",
                                                "content": "original accepted result"}]
    req.messages = copy.deepcopy(payload["messages"])
    req._managed_web_context = None
    claim(req, payload)
    openrouter.build_payload(req, config or CONFIG, caps or CAPS)
    return req, payload


def response(req, message):
    return {"id": "gen-synthetic-receipt", "model": req.model,
            "choices": [{"finish_reason": "tool_calls", "message": message}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20, "cost": "0"}}


async def complete(req, message, mutate=None):
    sent = []
    def supplier(wire):
        sent.append(json.loads(wire.content))
        if mutate is not None:
            mutate()
        return httpx.Response(200, json=response(req, message))
    async with httpx.AsyncClient(transport=httpx.MockTransport(supplier), trust_env=False) as client:
        result = await openrouter.complete(req, KEY, model_config=CONFIG, capabilities=CAPS,
                                          parse_tool=AIGateway._parse_local_tool_call, client=client)
    assert len(sent) == 1
    return result, sent[0]


@pytest.mark.parametrize(("version", "model", "digest"), [
    (GENERIC_V1, "openai/schema-fixture", "ccd5367605c09be97aeedf2b61633c3531789ce7eecdae1d94362faf217f77da"),
    (GOOGLE_FLAT_V1, "google/schema-fixture", "ea913b596cfd880453e2e496284480ce367e1f2496949f1ef05608330aaf2bdb"),
])
def test_historical_schema_snapshots_are_pinned(version, model, digest):
    encoded = openrouter._schema_json(openrouter._versioned_tool_definitions(model, version)).encode()
    assert hashlib.sha256(encoded).hexdigest() == digest


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("native", [False, True])
def test_all_fresh_requests_advertise_flat_v2_with_semantic_bounds(family, native):
    req = request(family + "/schema-fixture", native=native)
    payload = openrouter.build_payload(req, CONFIG, CAPS)
    assert [tool["function"]["name"] for tool in payload["tools"]] == list(OPERATIONS)
    assert payload["tools"] == openrouter.local_tool_definitions(req.model)
    read = payload["tools"][0]["function"]["parameters"]
    assert read["required"] == ["path"] and read["additionalProperties"] is False
    assert set(read["properties"]) == {"path", "max_bytes", "result_content_max_chars", "timeout_sec"}
    assert read["properties"]["max_bytes"]["minimum"] == 1
    assert read["properties"]["result_content_max_chars"]["minimum"] == 1
    assert read["properties"]["path"]["minLength"] == 1
    assert req._managed_openrouter_dispatch.input_tokens == openrouter.input_token_bound(req)
    assert openrouter.input_token_bound(req) >= len(json.dumps(payload["tools"]).encode())
    payload["tools"][0]["function"]["parameters"]["properties"]["path"]["type"] = "integer"
    assert openrouter.local_tool_definitions(req.model)[0]["function"]["parameters"]["properties"]["path"]["type"] == "string"


@pytest.mark.asyncio
@pytest.mark.parametrize("family", FAMILIES)
async def test_flat_v2_original_arguments_callid_and_private_state_preserved(family):
    req = request(family + "/schema-fixture", native=True)
    message = assistant(FLAT_V2, args={"path": "fixture.txt", "max_bytes": 17})
    result, sent = await complete(req, message)
    assert result["tool_calls"][0]["arguments"] == {"path": "fixture.txt", "max_bytes": 17}
    assert result["tool_calls"][0]["provider_call_id"] == "call-original"
    captured = req._native_envelope_capture
    assert captured.assistant_message() == message and captured.request_payload() == sent
    assert captured.continuation_safe and PRIVATE not in json.dumps(result, default=str)


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "args", "mode"), [
    ("Read", {"path": "fixture.txt"}, "read_only"),
    ("Search", {"pattern": "needle", "max_files": 1, "max_matches": 1}, "read_only"),
    ("Write", {"filepath": "fixture.txt", "content": "", "write_mode": "replace"}, "mutating"),
    ("FindAndReplace", {"filepath": "fixture.txt", "old_string": "old", "new_string": "", "replace_all": False}, "mutating"),
    ("FindAndReplace", {"filepath": "fixture.txt", "old_string": " \t", "new_string": ""}, "mutating"),
    ("InsertAtEnd", {"filepath": "fixture.txt", "content": "tail"}, "mutating"),
    ("Bash", {"command": "printf synthetic", "shell_mode": "mutating", "timeout_sec": 1}, "mutating"),
    ("List", {"recursive": False, "max_entries": 1}, "read_only"),
])
async def test_every_operation_accepts_only_its_own_contract(name, args, mode):
    req = request()
    original = copy.deepcopy(args)
    result, _ = await complete(req, assistant(FLAT_V2, name, args))
    call = result["tool_calls"][0]
    assert call["tool_name"] == name and call["shell_mode"] == mode
    assert call["arguments"] == {k: v for k, v in args.items() if k not in {"shell_mode", "timeout_sec"}}
    assert args == original


@pytest.mark.asyncio
@pytest.mark.parametrize(("version", "model"), [
    (GENERIC_V1, "openai/schema-fixture"), (GENERIC_V1, "anthropic/schema-fixture"),
    (GENERIC_V1, "mistralai/schema-fixture"), (GOOGLE_FLAT_V1, "google/schema-fixture"),
    *[(FLAT_V2, family + "/schema-fixture") for family in FAMILIES],
])
async def test_retained_schema_payload_and_response_parser_remain_bound(version, model):
    req, retained = continuation(version, model)
    expected = copy.deepcopy(retained)
    args = {"filepath": "fixture.txt"} if version == GENERIC_V1 else {"path": "fixture.txt"}
    message = assistant(version, args=args)
    result, sent = await complete(req, message)
    assert sent == expected and retained == expected
    assert result["tool_calls"][0]["arguments"] == args
    assert req._native_envelope_capture.assistant_message() == message
    assert openrouter.build_payload(req, CONFIG, CAPS) == expected


@pytest.mark.parametrize("change", ["missing", "empty", "extra", "order", "description", "bool_integer", "float_integer", "required"])
def test_unknown_or_modified_retained_schemas_reject_without_reflection(change):
    req, retained = continuation(FLAT_V2)
    if change == "missing":
        del retained["tools"]
    elif change == "empty":
        retained["tools"] = []
    elif change == "extra":
        retained["tools"].append({"type": "function", "function": {"name": PRIVATE}})
    elif change == "order":
        retained["tools"].reverse()
    elif change == "description":
        retained["tools"][0]["function"]["description"] = PRIVATE
    elif change == "required":
        retained["tools"][0]["function"]["parameters"]["required"] = []
    else:
        retained["tools"][0]["function"]["parameters"]["properties"]["max_bytes"]["minimum"] = (
            True if change == "bool_integer" else 1.0)
    claim(req, retained)
    with pytest.raises(ContractError) as caught:
        openrouter.build_payload(req, CONFIG, CAPS)
    assert PRIVATE not in str(caught.value)


def test_untyped_override_cannot_select_generic_for_a_fresh_request():
    req = request(native=True)
    req._native_generation_claim = SimpleNamespace(previous_request_id=str(uuid4()),
        history_revision=1, continuation_payload_json=json.dumps({"tools": [openrouter.local_tool_definition()]}))
    req._tool_schema_version = GENERIC_V1
    req._tool_definitions = [openrouter.local_tool_definition()]
    with pytest.raises(ContractError, match="private native generation claim"):
        openrouter.build_payload(req, CONFIG, CAPS)
    with pytest.raises(ContractError, match="private native generation claim"):
        openrouter.parse_response(response(req, assistant(GENERIC_V1)), req, CONFIG, AIGateway._parse_local_tool_call)


@pytest.mark.parametrize("field", ["_tool_schema_version", "_tool_definitions", "_native_generation_claim"])
def test_caller_cannot_construct_a_private_schema_override(field):
    with pytest.raises(TypeError):
        InferenceRequest("synthetic", "openrouter", "openai/schema-fixture", [], **{field: GENERIC_V1})


def test_unknown_version_is_not_a_registry_override():
    with pytest.raises(ContractError, match="Unrecognized retained"):
        openrouter._versioned_tool_definitions("openai/schema-fixture", PRIVATE)


@pytest.mark.parametrize(("name", "args"), [
    ("Read", {}), ("Read", {"path": "a", "content": "unused"}),
    ("Read", {"path": "a", "filepath": "b"}), ("Read", {"path": "a", "filepath": "a"}),
    ("Read", {"filepath": "a"}), ("Read", {"path": " "}),
    ("Read", {"path": "a", "max_bytes": 0}), ("Read", {"path": "a", "max_bytes": -1}),
    ("Read", {"path": "a", "max_bytes": True}), ("Read", {"path": "a", "max_bytes": 1.0}),
    ("Read", {"path": "a", "result_content_max_chars": 0}),
    ("Read", {"path": "a", "timeout_sec": 0}), ("Read", {"path": "a", "timeout_sec": 301}),
    ("Read", {"path": "a", "shell_mode": "mutating"}),
    ("Search", {}), ("Search", {"pattern": "x", "recursive": False}),
    ("Search", {"pattern": "x", "max_files": 0}),
    ("Bash", {}), ("Bash", {"command": "printf synthetic", "foreground_job_id": "job-1"}),
    ("Write", {"filepath": "a"}), ("Write", {"filepath": "a", "content": "x", "write_mode": "invalid"}),
    ("FindAndReplace", {"filepath": "a", "old_string": "old"}),
    ("FindAndReplace", {"filepath": "a", "old_string": "", "new_string": "x"}),
    ("InsertAtEnd", {"filepath": "a", "content": "tail", "pattern": "unused"}),
    ("List", {"recursive": 1}), ("List", {"max_entries": -1}),
])
def test_fresh_flat_rejects_irrelevant_required_type_bound_alias_failures(name, args):
    req = request()
    with pytest.raises(ValueError):
        openrouter.parse_response(response(req, assistant(FLAT_V2, name, args)), req, CONFIG, AIGateway._parse_local_tool_call)


@pytest.mark.parametrize("args", [
    {}, {"path": "a", "filepath": "b"}, {"path": "a", "max_bytes": 0},
    {"path": "a", "content": "unused"},
])
def test_legacy_generic_wire_does_not_authorize_invalid_runtime_shapes(args):
    req, _ = continuation(GENERIC_V1)
    with pytest.raises(ValueError):
        openrouter.parse_response(response(req, assistant(GENERIC_V1, args=args)), req, CONFIG, AIGateway._parse_local_tool_call)


@pytest.mark.parametrize("version", [GENERIC_V1, FLAT_V2])
def test_recognized_schema_cannot_bypass_other_payload_or_latest_gates(version):
    for change in ("price", "reasoning", "plugins", "max_tokens", "tools_gate", "context_bound"):
        req, retained = continuation(version)
        config, caps = dict(CONFIG), dict(CAPS)
        if change == "price":
            config["cost_input_per_1m"] = "3"
        elif change == "tools_gate":
            caps["tools"] = False
        elif change == "context_bound":
            caps["context_window_tokens"] = 512
        else:
            retained[change] = {"effort": "high"} if change == "reasoning" else [] if change == "plugins" else 64
            claim(req, retained)
        with pytest.raises(ContractError):
            openrouter.build_payload(req, config, caps)


@pytest.mark.asyncio
async def test_version_is_latched_across_provider_await_not_response_selected():
    req = request(native=True)
    replacement = copy.deepcopy(openrouter.build_payload(req, CONFIG, CAPS))
    replacement["tools"] = [openrouter.local_tool_definition()]
    with pytest.raises(openrouter.OpenRouterFailure) as caught:
        await complete(req, assistant(GENERIC_V1), mutate=lambda: claim(req, replacement))
    assert caught.value.diagnostic_reason == "invalid_tool_schema"


@pytest.mark.parametrize("version", [GENERIC_V1, FLAT_V2])
def test_web_retains_schema_and_rechecks_full_reviewed_prefix(monkeypatch, version):
    from openvegas.gateway import providers
    from tests.test_models.test_openrouter_web_gateway import catalog_row, review
    from tests.test_models.test_openrouter_web_gateway import request as web_request

    frozen_review = review()
    monkeypatch.setattr(providers, "get_model_review", lambda *_: copy.deepcopy(frozen_review))
    req = web_request(enable_tools=True)
    config = catalog_row()
    caps = {"tools": True, "web_search": True, "context_window_tokens": 8192}
    req, retained = continuation(version, req=req, config=config, caps=caps)
    assert openrouter.build_payload(req, config, caps) == retained
    context = req._managed_web_context
    assert openrouter.build_payload(req, config, caps) == retained and req._managed_web_context is context
    retained["tools"][0]["parameters"]["max_uses"] = 2
    claim(req, retained)
    req._managed_web_context = None
    with pytest.raises(ContractError):
        openrouter.build_payload(req, config, caps)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [GENERIC_V1, FLAT_V2])
async def test_owned_media_remains_authoritative_for_each_schema(monkeypatch, version):
    from tests.test_models.test_openrouter_attachment_request import (
        Setup,
        UploadDB,
        catalog_config,
        model_review,
    )

    setup = Setup(UploadDB(), catalog_config(), model_review(), monkeypatch)
    setup.install_review()
    req, _ = await setup.request(tools=True)
    caps = {"tools": True, "context_window_tokens": 500_000}
    req, retained = continuation(version, req=req, config=setup.config, caps=caps)
    assert openrouter.build_payload(req, setup.config, caps) == retained
    assert openrouter.input_token_bound(req) > req._managed_attachment_context.prepared.media_tokens
    retained["messages"][0]["content"][1]["image_url"]["url"] = "data:image/png;base64,AA=="
    req.messages = copy.deepcopy(retained["messages"])
    claim(req, retained)
    from server.services.openrouter_attachments import AttachmentError
    with pytest.raises((AttachmentError, ContractError)):
        openrouter.build_payload(req, setup.config, caps)


@pytest.mark.parametrize(("version", "model", "response_version"), [
    (GENERIC_V1, "openai/schema-fixture", FLAT_V2),
    (FLAT_V2, "openai/schema-fixture", GENERIC_V1),
    (GOOGLE_FLAT_V1, "google/schema-fixture", GENERIC_V1),
])
def test_response_name_cannot_select_an_undispatched_schema(version, model, response_version):
    req, _ = continuation(version, model)
    with pytest.raises(ValueError, match="Unapproved tool"):
        openrouter.parse_response(response(req, assistant(response_version)), req, CONFIG, AIGateway._parse_local_tool_call)


@pytest.mark.parametrize(("model", "tools"), [
    ("google/schema-fixture", [openrouter.local_tool_definition()]),
    ("openai/schema-fixture", openrouter._google_flat_v1_definitions()),
])
def test_retained_legacy_schema_must_match_its_original_family_contract(model, tools):
    req, retained = continuation(FLAT_V2, model)
    retained["tools"] = tools
    claim(req, retained)
    with pytest.raises(ContractError, match="Unrecognized retained"):
        openrouter.build_payload(req, CONFIG, CAPS)


def test_legacy_equal_aliases_and_positive_read_options_remain_valid():
    req, _ = continuation(GENERIC_V1)
    args = {"path": "fixture.txt", "filepath": "fixture.txt", "max_bytes": 100, "result_content_max_chars": 256}
    result = openrouter.parse_response(response(req, assistant(GENERIC_V1, args=args)), req, CONFIG, AIGateway._parse_local_tool_call)
    assert result["tool_calls"][0]["arguments"] == args


def test_legacy_google_schema_preserved_but_nonpositive_limits_still_reject():
    req, _ = continuation(GOOGLE_FLAT_V1, "google/schema-fixture")
    with pytest.raises(ValueError):
        openrouter.parse_response(response(req, assistant(GOOGLE_FLAT_V1, args={"path": "a", "max_bytes": 0})),
                                  req, CONFIG, AIGateway._parse_local_tool_call)


@pytest.mark.parametrize("web", [False, True])
def test_fresh_requests_without_local_tools_unchanged(monkeypatch, web):
    from openvegas.gateway import providers
    from tests.test_models.test_openrouter_web_gateway import catalog_row, review
    from tests.test_models.test_openrouter_web_gateway import request as web_request

    frozen_review = review()
    monkeypatch.setattr(providers, "get_model_review", lambda *_: copy.deepcopy(frozen_review))
    req = web_request() if web else request()
    req.enable_tools = False
    config = catalog_row() if web else CONFIG
    caps = {"tools": False, "web_search": web, "context_window_tokens": 64000 if not web else 8192}
    payload = openrouter.build_payload(req, config, caps)
    assert not any(t.get("type") == "function" for t in payload.get("tools", []))
    assert ("tools" in payload) is web
    # Existing typed native continuation requires local tools. Do not widen it
    # into unsupported text/web-only continuation while changing the tool schema.
    claim(req, payload)
    with pytest.raises(ContractError):
        openrouter.build_payload(req, config, caps)


@pytest.mark.parametrize(("version", "model"), [
    (FLAT_V2, "openai/schema-fixture"),
    (GOOGLE_FLAT_V1, "google/schema-fixture"),
    (GENERIC_V1, "openai/schema-fixture"),
])
@pytest.mark.parametrize("name", ["Write", "InsertAtEnd"])
@pytest.mark.parametrize("content", ["字" * 6000, "x" * 31800])
def test_original_wire_argument_limit_preserves_unicode_and_near_limit_text(version, model, name, content):
    req = request(model) if version == FLAT_V2 else continuation(version, model)[0]
    args = {"filepath": "fixture.txt", "content": content}
    message = assistant(version, name, args)
    wire = {"tool_name": name, "arguments": args} if version == GENERIC_V1 else args
    function = message["tool_calls"][0]["function"]
    function["arguments"] = json.dumps(wire, ensure_ascii=False)
    assert len(function["arguments"].encode("utf-8")) < 32000
    result = openrouter.parse_response(response(req, message), req, CONFIG, AIGateway._parse_local_tool_call)
    assert result["tool_calls"][0]["arguments"] == args


@pytest.mark.parametrize("size", [32000, 32001])
def test_flat_wire_byte_boundary_excludes_internal_envelope_overhead(size):
    req = request()
    args = {"filepath": "fixture.txt", "content": ""}
    args["content"] = "x" * (size - len(json.dumps(args).encode("utf-8")))
    message = assistant(FLAT_V2, "Write", args)
    message["tool_calls"][0]["function"]["arguments"] = json.dumps(args)
    if size > 32000:
        with pytest.raises(ValueError):
            openrouter.parse_response(response(req, message), req, CONFIG, AIGateway._parse_local_tool_call)
    else:
        result = openrouter.parse_response(response(req, message), req, CONFIG, AIGateway._parse_local_tool_call)
        assert result["tool_calls"][0]["arguments"] == args
