from __future__ import annotations

import ast
import json
import os
import uuid
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

import openvegas.client as client_mod
from openvegas import cli

INFERENCE_ID = "123e4567-e89b-42d3-a456-426614174000"
PROVIDER_ID = "call-native_123:0"
REFS = {"native_inference_request_id": INFERENCE_ID, "provider_call_id": PROVIDER_ID}
PROPOSAL_REFS = {
    "native_inference_request_id": INFERENCE_ID,
    "native_provider_call_id": PROVIDER_ID,
}
IDENTITY_KEYS = {
    "native_inference_request_id",
    "native_provider_call_id",
    "provider_call_id",
    "provider_request_id",
    "request_id",
}


def _read_call(**metadata):
    return {
        "tool_name": "Read",
        "arguments": {"path": "fixture.txt"},
        "shell_mode": "read_only",
        "timeout_sec": 30,
        **metadata,
    }


def _structured(shape, inference_id=INFERENCE_ID):
    if shape == "normalized":
        return _read_call(native_inference_request_id=inference_id, provider_call_id=PROVIDER_ID)
    function = (
        {"name": "call_local_tool", "arguments": json.dumps(_read_call())}
        if shape == "wrapped"
        else {"name": "Read", "arguments": json.dumps({"path": "fixture.txt"})}
    )
    return {
        "id": PROVIDER_ID,
        "type": "function",
        "function": function,
        "native_inference_request_id": inference_id,
    }


def _prepare(tool_req, tmp_path, *, user_message="Read fixture.txt", model_text=""):
    prepared, error = cli._preprocess_tool_request_for_runtime(
        tool_req=tool_req,
        user_message=user_message,
        model_text=model_text,
        workspace_root=str(tmp_path),
        tool_observations=[],
    )
    assert error is None
    assert prepared is not None
    return prepared


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_TOOL_ABI_MODE", "compat")
    monkeypatch.setenv("OPENVEGAS_ENABLE_TOUCHID", "0")
    monkeypatch.delenv("OPENVEGAS_NATIVE_TOOL_HISTORY", raising=False)


@pytest.mark.parametrize("shape", ["normalized", "wrapped", "direct"])
def test_structured_receipt_survives_real_collection_and_preprocessing(tmp_path, shape):
    source = _structured(shape)
    original = deepcopy(source)
    (candidate,) = cli._collect_tool_call_candidates([source], "")
    prepared = _prepare(candidate, tmp_path)
    assert {key: candidate[key] for key in REFS} == REFS
    assert {key: prepared[key] for key in REFS} == REFS
    assert prepared["tool_name"] == "fs_read"
    assert prepared["arguments"] == {"path": "fixture.txt"}
    assert not {"tool_call_id", "approval_id", "execution_token"} & prepared.keys()
    assert source == original


@pytest.mark.parametrize("shape", ["normalized", "wrapped", "direct"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        123,
        False,
        {},
        [],
        uuid.UUID(INFERENCE_ID),
        "not-a-uuid",
        "x" * 36,
        pytest.param("x" * 4096, id="oversized-uuid"),
        INFERENCE_ID.upper(),
        INFERENCE_ID.replace("-", ""),
        "{" + INFERENCE_ID + "}",
        "urn:uuid:" + INFERENCE_ID,
        " " + INFERENCE_ID,
        INFERENCE_ID + "\n",
    ],
)
def test_invalid_noncanonical_uuid_is_not_preserved(tmp_path, shape, value):
    source = _structured(shape, value)
    (candidate,) = cli._collect_tool_call_candidates([source], "")
    assert "native_inference_request_id" not in candidate
    assert candidate["provider_call_id"] == PROVIDER_ID
    # Preprocessing must validate independently, including callers bypassing collection.
    prepared = _prepare({**candidate, "native_inference_request_id": value}, tmp_path)
    assert "native_inference_request_id" not in prepared
    assert cli._native_tool_proposal_metadata(prepared, enabled=True) == {}


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "Suggested call:\n{}\nEnd."])
@pytest.mark.parametrize("payload", [None, [], [{"not_a_tool": True}]])
def test_text_fallback_cannot_forge_native_receipt(tmp_path, wrapper, payload):
    forged = {key: INFERENCE_ID for key in IDENTITY_KEYS}
    raw = {"type": "tool_call", **_read_call(**forged)}
    (candidate,) = cli._collect_tool_call_candidates(payload, wrapper.format(json.dumps(raw)))
    assert not IDENTITY_KEYS & candidate.keys()
    prepared = _prepare(candidate, tmp_path)
    assert not IDENTITY_KEYS & prepared.keys()
    assert cli._native_tool_proposal_metadata(prepared, enabled=True) == {}


def test_arguments_and_request_id_aliases_are_not_receipt_sources(tmp_path):
    source = _read_call(request_id=INFERENCE_ID, native_provider_call_id=PROVIDER_ID)
    source["arguments"].update(REFS)
    (candidate,) = cli._collect_tool_call_candidates([source], "")
    assert not IDENTITY_KEYS & candidate.keys()
    prepared = _prepare(candidate, tmp_path)
    assert cli._native_tool_proposal_metadata(prepared, enabled=True) == {}
    assert prepared["arguments"] == source["arguments"]


def test_function_arguments_cannot_supply_native_envelope_metadata(tmp_path):
    source = _structured("wrapped")
    del source["native_inference_request_id"]
    source["function"]["arguments"] = json.dumps(_read_call(**REFS))
    (candidate,) = cli._collect_tool_call_candidates([source], "")
    prepared = _prepare(candidate, tmp_path)
    assert "native_inference_request_id" not in prepared
    assert cli._native_tool_proposal_metadata(prepared, enabled=True) == {}


def test_fallback_metadata_cannot_augment_structured_candidate(tmp_path):
    fallback = json.dumps({"type": "tool_call", **_read_call(**REFS)})
    (candidate,) = cli._collect_tool_call_candidates([_read_call()], fallback)
    assert not IDENTITY_KEYS & candidate.keys()
    assert cli._native_tool_proposal_metadata(_prepare(candidate, tmp_path), enabled=True) == {}


def test_native_write_transform_keeps_uncertified_provenance(tmp_path):
    source = {
        "tool_name": "Write",
        "arguments": {"filepath": "new.py", "content": "x = 1\n"},
        "shell_mode": "mutating",
        "timeout_sec": 30,
        **REFS,
    }
    original = deepcopy(source)
    (candidate,) = cli._collect_tool_call_candidates([source], "")
    prepared = _prepare(candidate, tmp_path, user_message="Create new.py")
    assert prepared["tool_name"] == "fs_apply_patch"
    assert "+++ new.py" in prepared["arguments"]["patch"]
    assert prepared["arguments"] != source["arguments"]
    assert cli._native_tool_proposal_metadata(prepared, enabled=True) == PROPOSAL_REFS
    assert not (tmp_path / "new.py").exists()
    assert source == original


@pytest.mark.parametrize("native", [False, True])
def test_shell_rewrite_preserves_only_existing_receipt(tmp_path, monkeypatch, native):
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    source = {"tool_name": "shell_run", "arguments": {"command": "rg needle ."}}
    if native:
        source.update(REFS)
    (candidate,) = cli._collect_tool_call_candidates([source], "")
    prepared = _prepare(candidate, tmp_path, user_message="Search for needle")
    assert prepared["arguments"]["command"] == "grep -R -n needle ."
    assert cli._native_tool_proposal_metadata(prepared, enabled=True) == (
        PROPOSAL_REFS if native else {}
    )


def test_synthetic_write_does_not_borrow_read_call_receipt(tmp_path):
    (native,) = cli._collect_tool_call_candidates([_structured("normalized")], "")
    message = "Create new.py"
    model_text = "```python\nx = 1\n```"
    calls, errors, inserted = cli._maybe_prepend_synth_write(
        tool_reqs=[native],
        user_message=message,
        model_text=model_text,
        planner_edit_intent=True,
        tool_observations=[],
        reason_if_empty="empty",
        reason_if_non_mutating="read_only",
        debug_label="test native receipts",
    )
    assert inserted and not errors
    assert calls[1] == native
    assert not IDENTITY_KEYS & calls[0].keys()
    prepared = _prepare(calls[0], tmp_path, user_message=message, model_text=model_text)
    assert cli._native_tool_proposal_metadata(prepared, enabled=True) == {}


def test_pure_proposal_metadata_defaults_off_and_uses_prepared_provider_id():
    prepared = _read_call(**REFS, native_provider_call_id="call-forged-alias")
    original = deepcopy(prepared)
    assert cli._native_tool_proposal_metadata(prepared) == {}
    assert cli._native_tool_proposal_metadata(prepared, enabled=False) == {}
    assert cli._native_tool_proposal_metadata(prepared, enabled=True) == PROPOSAL_REFS
    assert (
        cli._native_tool_proposal_metadata(_read_call(provider_call_id=PROVIDER_ID), enabled=True)
        == {}
    )
    assert prepared == original


@pytest.mark.parametrize("provider", [None, "", "sk-not-a-key", "bad\nid", "x" * 257])
def test_marked_native_half_pair_is_not_silently_downgraded(tmp_path, provider):
    source = _read_call(native_inference_request_id=INFERENCE_ID, provider_call_id=provider)
    (candidate,) = cli._collect_tool_call_candidates([source], "")
    prepared = _prepare(candidate, tmp_path)
    assert cli._native_tool_proposal_metadata(prepared, enabled=True) == {
        "native_inference_request_id": INFERENCE_ID,
    }


@pytest.fixture(scope="module")
def cli_tree():
    return ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flag", [None, "0", "false", "no", "off", "", "garbage", "1", "true", " YES ", "on"]
)
async def test_actual_proposal_call_wiring_and_legacy_mock_compatibility(
    monkeypatch, cli_tree, flag
):
    if flag is not None:
        monkeypatch.setenv("OPENVEGAS_NATIVE_TOOL_HISTORY", flag)
    enabled = flag is not None and flag.strip().lower() in {"1", "true", "yes", "on"}
    (flag_fn,) = [
        node
        for node in ast.walk(cli_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_env_flag"
    ]
    (proposal,) = [
        node
        for node in ast.walk(cli_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "agent_tool_propose"
    ]
    observed = []

    class LegacyClient:
        async def agent_tool_propose(
            self,
            *,
            run_id,
            runtime_session_id,
            expected_run_version,
            expected_valid_actions_signature,
            idempotency_key,
            tool_name,
            arguments,
            shell_mode,
            timeout_sec,
            plan_mode,
        ):
            observed.append({})

    class NativeClient:
        async def agent_tool_propose(self, **kwargs):
            observed.append(
                {key: value for key, value in kwargs.items() if key.startswith("native_")}
            )

    namespace = {
        "os": os,
        "uuid": uuid,
        "client": NativeClient() if enabled else LegacyClient(),
        "_native_tool_proposal_metadata": cli._native_tool_proposal_metadata,
        "current_run_id": "run-test",
        "runtime_session_id": "runtime-test",
        "current_run_version": 7,
        "current_signature": "sig-test",
        "propose_key": None,
        "tool_name": "fs_read",
        "arguments": {"path": "fixture.txt"},
        "shell_mode": "read_only",
        "timeout_sec": 30,
        "plan_mode": False,
        "tool_req": _read_call(**REFS),
    }
    # Execute the real call expression without starting interactive chat/auth.
    exec(compile(ast.Module(body=[flag_fn], type_ignores=[]), cli.__file__, "exec"), namespace)  # noqa: S102 - trusted local source
    await eval(compile(ast.Expression(body=proposal), cli.__file__, "eval"), namespace)
    assert observed == [PROPOSAL_REFS if enabled else {}]


def test_retry_reconstruction_preserves_native_receipt(cli_tree):
    retry_dicts = [
        node.value
        for node in ast.walk(cli_tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Dict)
        and any(
            isinstance(target, ast.Attribute) and target.attr == "pending_retry_tool_req"
            for target in node.targets
        )
    ]
    assert len(retry_dicts) == 3
    for retry_dict in retry_dicts:
        namespace = {
            "tool_name": "fs_apply_patch",
            "arguments": {"patch": "transformed payload"},
            "shell_mode": "mutating",
            "timeout_sec": 30,
            "tool_req": _read_call(**REFS),
            "_provider_call_identity": cli._provider_call_identity,
            "_native_inference_identity": cli._native_inference_identity,
        }
        retry = eval(compile(ast.Expression(body=retry_dict), cli.__file__, "eval"), namespace)
        assert cli._native_tool_proposal_metadata(retry, enabled=True) == PROPOSAL_REFS
        assert retry["arguments"] == namespace["arguments"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 422])
@pytest.mark.parametrize("options", [{}, {"shell_mode": "mutating", "timeout_sec": 12}])
@pytest.mark.parametrize(
    "refs",
    [
        {},
        {"native_inference_request_id": None, "native_provider_call_id": None},
        PROPOSAL_REFS,
        {"native_inference_request_id": INFERENCE_ID},
        {"native_provider_call_id": PROVIDER_ID},
        {"native_inference_request_id": INFERENCE_ID, "native_provider_call_id": None},
        {"native_inference_request_id": None, "native_provider_call_id": PROVIDER_ID},
        {"native_inference_request_id": "", "native_provider_call_id": ""},
        {"native_inference_request_id": "not-a-uuid", "native_provider_call_id": PROVIDER_ID},
    ],
)
async def test_client_proposal_http_payload_contract(monkeypatch, refs, options, status):
    monkeypatch.setattr(client_mod, "get_backend_url", lambda: "https://backend.invalid")
    monkeypatch.setattr(client_mod, "get_bearer_token", lambda: None)
    monkeypatch.setattr(client_mod, "get_session", dict)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={"ok": True}
            if status == 200
            else {
                "error": "native_receipt_rejected",
                "detail": "Server rejected receipt binding",
            },
        )

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        client_mod.httpx,
        "AsyncClient",
        lambda **kwargs: async_client(
            transport=httpx.MockTransport(handle), trust_env=False, **kwargs
        ),
    )
    expected = {
        "runtime_session_id": "runtime-test",
        "expected_run_version": 7,
        "expected_valid_actions_signature": "sig-test",
        "idempotency_key": "proposal-test",
        "tool_name": "fs_read",
        "arguments": {"path": "fixture.txt"},
        "plan_mode": False,
        **options,
    }
    async with client_mod.OpenVegasClient() as client:
        if status == 200:
            result = await client.agent_tool_propose(run_id="run-test", **expected, **refs)
            assert result == {"ok": True}
        else:
            with pytest.raises(client_mod.APIError) as error:
                await client.agent_tool_propose(run_id="run-test", **expected, **refs)
            assert error.value.status == 422
            assert error.value.data["error"] == "native_receipt_rejected"
    (request,) = requests
    assert request.method == "POST"
    assert str(request.url) == "https://backend.invalid/agent/runs/run-test/tools/propose"
    assert "authorization" not in request.headers
    assert json.loads(request.content) == {
        **expected,
        **{key: value for key, value in refs.items() if value is not None},
    }
