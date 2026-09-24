"""Pure codec tests; no provider, database, credentials or terminal access."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest

from openvegas.agent import native_handoff_document as handoff
from openvegas.contracts.errors import APIErrorCode, ContractError

FILE_ID = "11111111-1111-4111-8111-111111111111"


def read_observation():
    return {"tool_name": "Read", "arguments": {"path": "a.txt"}, "result": {
        "status": "succeeded", "payload": {"ok": True, "path": "a.txt", "content": "hello\n",
        "content_truncated": False, "truncated": False, "bytes_read": 6, "max_bytes": 100},
        "stdout": "hello\n", "stderr": ""}}


def tasks():
    return [{"user_text": "Read my file.\r\nKeep the newline.",
             "attachment_refs": [{"file_id": FILE_ID, "sha256": "a" * 64}],
             "generations": [{"assistant_text": "", "observations": [read_observation()]},
                             {"assistant_text": "It says hello.\n", "observations": []}]}]


def test_lossless_order_canonical_hash_and_detached_values():
    original = tasks()
    original.append({"user_text": "Now explain it.", "attachment_refs": [], "generations": [
        {"assistant_text": "It is a greeting.", "observations": []}]})
    expected = copy.deepcopy(original)
    document = handoff.PortableTaskDocument.from_tasks(original)
    original[0]["generations"].clear()
    assert document.values()["tasks"] == expected
    document.values()["tasks"].clear()
    assert document.values()["tasks"] == expected
    assert document.sha256 == hashlib.sha256(document.to_json().encode()).hexdigest()
    assert document.byte_bound == len(document.to_json().encode())
    assert handoff.PortableTaskDocument(json.dumps(document.values(), indent=2)) == document
    assert "hello" not in repr(document)
    with pytest.raises(FrozenInstanceError):
        document._json = "changed"


@pytest.mark.parametrize("level", ["root", "task", "generation", "observation", "arguments", "result", "payload"])
@pytest.mark.parametrize("field", ["reasoning", "signature", "execution_token", "source_snapshot", "extension"])
def test_positive_field_allowlists_reject_private_extensions(level, field):
    value = {"kind": handoff.KIND, "tasks": tasks()}
    task = value["tasks"][0]
    generation = task["generations"][0]
    obs = generation["observations"][0]
    target = {"root": value, "task": task, "generation": generation, "observation": obs,
              "arguments": obs["arguments"], "result": obs["result"],
              "payload": obs["result"]["payload"]}[level]
    target[field] = "synthetic-private-canary"
    with pytest.raises(ContractError) as error:
        handoff.PortableTaskDocument(json.dumps(value))
    assert error.value.code == APIErrorCode.HANDOFF_BLOCKED
    assert "canary" not in str(error.value) and field not in str(error.value)


@pytest.mark.parametrize("bad", [
    '{"kind":"first","kind":"second","tasks":[]}',
    '{"kind":"x","tasks":NaN}',
    '{"kind":"x","tasks":Infinity}',
    "[" * 1000 + "0" + "]" * 1000, "{}", "[]", "null", "\ud800",
])
def test_invalid_or_ambiguous_storage_rejected(bad):
    with pytest.raises(ContractError):
        handoff.PortableTaskDocument(bad)


@pytest.mark.parametrize("text", ["", " \n", "api_key=synthetic-secret", "a\x1bb", "a\x7fb",
                                  "a\u202eb", "\ud800", "x" * 64001, "\u754c" * 22000])
def test_user_text_bounds_secrets_controls_no_silent_cleanup(text):
    value = tasks()
    value[0]["user_text"] = text
    with pytest.raises(ContractError):
        handoff.PortableTaskDocument.from_tasks(value)


def test_configured_redaction_rejects_whole_document(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", "hello")
    with pytest.raises(ContractError):
        handoff.PortableTaskDocument.from_tasks(tasks())


@pytest.mark.parametrize("mutation", ["unfinished", "empty_final", "truncated", "payload_truncated",
                                     "timeout", "cancelled", "pending", "duplicate_file", "changed_file",
                                     "bool_size", "float_size", "extra_call", "no_tasks"])
def test_incomplete_unsupported_or_uncertain_input_rejected(mutation):
    value = tasks()
    task, obs = value[0], value[0]["generations"][0]["observations"][0]
    if mutation == "unfinished":
        task["generations"].pop()
    elif mutation == "empty_final":
        task["generations"][-1]["assistant_text"] = ""
    elif mutation in {"truncated", "payload_truncated"}:
        obs["result"]["payload"]["truncated" if mutation == "truncated" else "content_truncated"] = True
    elif mutation in {"timeout", "cancelled", "pending"}:
        obs["result"]["status"] = mutation
    elif mutation == "duplicate_file":
        task["attachment_refs"] *= 2
    elif mutation == "changed_file":
        value.append(copy.deepcopy(task))
        value[1]["attachment_refs"][0]["sha256"] = "b" * 64
    elif mutation in {"bool_size", "float_size"}:
        obs["result"]["payload"]["bytes_read"] = True if mutation == "bool_size" else 6.0
    elif mutation == "extra_call":
        task["generations"][0]["observations"] *= 17
    else:
        value = []
    with pytest.raises(ContractError):
        handoff.PortableTaskDocument.from_tasks(value)


def test_bounded_whole_document_and_task_count(monkeypatch):
    with pytest.raises(ContractError):
        handoff.PortableTaskDocument.from_tasks(tasks() * (handoff.MAX_TASKS + 1))
    monkeypatch.setattr(handoff, "MAX_DOCUMENT_BYTES", 100)
    with pytest.raises(ContractError):
        handoff.PortableTaskDocument.from_tasks(tasks())


def test_error_observation_is_retained_not_retried_or_rewritten():
    value = tasks()
    obs = value[0]["generations"][0]["observations"][0]
    obs["result"] = {"status": "failed", "payload": {"ok": False,
        "reason_code": "tool_execution_failed", "detail": "No file."}, "stdout": "", "stderr": ""}
    assert handoff.PortableTaskDocument.from_tasks(value).values()["tasks"] == value


def test_no_provider_or_system_roles_or_execution_authority_in_document():
    document = handoff.PortableTaskDocument.from_tasks(tasks()).to_json()
    for forbidden in ("execution_token", "provider_request_id", "provider_call_id", "role",
                      "system", "reasoning_details", "request_payload_json"):
        assert '"' + forbidden + '"' not in document


@pytest.mark.parametrize("name,args", [("Read", {"path": "a.txt"}),
                                       ("List", {"path": "."}),
                                       ("Search", {"pattern": "hello", "path": "."})])
@pytest.mark.parametrize("content", ["hello\n", "\ufeffhello\n"])
def test_real_readonly_runtime_output_fits_public_schema(tmp_path, name, args, content):
    from openvegas.agent import local_tools

    (tmp_path / "a.txt").write_text(content, encoding="utf-8")
    execute = {"Read": local_tools._exec_fs_read, "List": local_tools._exec_fs_list,
               "Search": local_tools._exec_fs_search}[name]
    result = execute(tmp_path, args)
    value = tasks()
    value[0]["generations"][0]["observations"] = [{"tool_name": name, "arguments": args,
        "result": {"status": result.result_status, "payload": result.result_payload,
                   "stdout": result.stdout, "stderr": result.stderr}}]
    assert handoff.PortableTaskDocument.from_tasks(value).values()["tasks"] == value


@pytest.mark.parametrize("name,args", [
    ("Write", {"filepath": "a.txt", "content": "\ufeffnew\n", "write_mode": "replace"}),
    ("InsertAtEnd", {"filepath": "a.txt", "content": "new\n"}),
    ("FindAndReplace", {"filepath": "a.txt", "old_string": "old", "new_string": "new",
                       "replace_all": True}),
])
def test_mutation_public_proof_without_private_source_snapshot(name, args):
    from openvegas.agent.native_mutation import build_mutation_plan
    from openvegas.agent.native_mutation_lifecycle import validated_proof

    plan = build_mutation_plan({"tool_name": name, "arguments": args},
                               {"exists": True, "content_utf8": "old\nprivate fixture\n"})
    proof = {"kind": "runtime_observed_file_v1", "contract_sha256": plan.contract_sha256,
             "relative_path": plan.relative_path,
             "observed_before": {"exists": plan.before_exists, "sha256": plan.before_sha256,
                                 "bytes": plan.before_bytes},
             "observed_after": {"exists": True, "sha256": plan.after_sha256, "bytes": plan.after_bytes},
             "outcome": "applied", "reason": None}
    assert validated_proof(plan, proof, "succeeded") == "committed"
    value = tasks()
    value[0]["generations"][0]["observations"] = [{"tool_name": name, "arguments": args,
        "result": {"status": "succeeded", "payload": {"native_mutation_proof": proof},
                   "stdout": "", "stderr": ""}}]
    document = handoff.PortableTaskDocument.from_tasks(value)
    assert document.values()["tasks"] == value
    assert "private fixture" not in document.to_json()


@pytest.mark.parametrize("exit_code", [0, 1])
def test_shell_completion_retains_public_result_exactly(exit_code):
    value = tasks()
    value[0]["generations"][0]["observations"] = [{"tool_name": "Bash", "arguments": {"command": "pwd"},
        "result": {"status": "succeeded" if exit_code == 0 else "failed", "payload": {
            "ok": exit_code == 0, "requested_command": "pwd", "effective_command": "pwd",
            "shell_wrapper": "/bin/bash -lc", "execution_cwd": "/synthetic/workspace",
            "exit_code": exit_code, "duration_ms": 1, "final_status_message": "Completed."},
            "stdout": "result\n", "stderr": ""}}]
    assert handoff.PortableTaskDocument.from_tasks(value).values()["tasks"] == value
    value[0]["generations"][0]["observations"][0]["result"]["payload"]["status"] = "running"
    with pytest.raises(ContractError):
        handoff.PortableTaskDocument.from_tasks(value)


@pytest.mark.parametrize("mutation", ["tuple", "key", "cycle", "huge_int", "nil_file", "bytes",
                                     "read_mismatch", "negative_size", "too_many_generations"])
def test_python_objects_cannot_coerce_or_bypass_document_schema(mutation):
    value = tasks()
    if mutation == "tuple":
        value = tuple(value)
    elif mutation == "key":
        value[0][1] = "unsupported"
    elif mutation == "cycle":
        value.append(value)
    elif mutation == "huge_int":
        value[0]["generations"][0]["observations"][0]["result"]["payload"]["bytes_read"] = 2**64
    elif mutation == "nil_file":
        value[0]["attachment_refs"][0]["file_id"] = "00000000-0000-0000-0000-000000000000"
    elif mutation == "bytes":
        value[0]["user_text"] = b"not text"
    elif mutation == "read_mismatch":
        value[0]["generations"][0]["observations"][0]["result"]["stdout"] = "different"
    elif mutation == "negative_size":
        value[0]["generations"][0]["observations"][0]["result"]["payload"]["bytes_read"] = -1
    else:
        value[0]["generations"] = [{"assistant_text": "Public", "observations": []}] * 129
    with pytest.raises(ContractError):
        handoff.PortableTaskDocument.from_tasks(value)


@pytest.mark.parametrize("aliases", [{"path": "a.txt"}, {"filepath": "a.txt"},
                                     {"path": "a.txt", "filepath": "a.txt"}])
def test_original_matching_read_aliases_are_lossless(aliases):
    value = tasks()
    value[0]["generations"][0]["observations"][0]["arguments"] = aliases
    assert handoff.PortableTaskDocument.from_tasks(value).values()["tasks"] == value
    value[0]["generations"][0]["observations"][0]["arguments"] = {"path": "a.txt", "filepath": "other.txt"}
    with pytest.raises(ContractError):
        handoff.PortableTaskDocument.from_tasks(value)


def test_canonical_exact_byte_limit_is_consistent_between_constructors(monkeypatch):
    document = handoff.PortableTaskDocument.from_tasks(tasks())
    monkeypatch.setattr(handoff, "MAX_DOCUMENT_BYTES", document.byte_bound)
    assert handoff.PortableTaskDocument.from_tasks(tasks()) == document
    assert handoff.PortableTaskDocument(document.to_json()) == document
    monkeypatch.setattr(handoff, "MAX_DOCUMENT_BYTES", document.byte_bound - 1)
    for build in (lambda: handoff.PortableTaskDocument.from_tasks(tasks()),
                  lambda: handoff.PortableTaskDocument(document.to_json())):
        with pytest.raises(ContractError):
            build()


def test_public_web_citations_are_preserved_in_order_without_native_annotations():
    value = tasks()
    value[0]["generations"][-1].update(web_search_used=True, web_search_sources=[
        "https://example.com/evidence?q=one#details", "https://example.org/next"])
    assert handoff.PortableTaskDocument.from_tasks(value).values()["tasks"] == value


@pytest.mark.parametrize("changes", [
    {"web_search_used": True}, {"web_search_sources": []},
    {"web_search_used": 1, "web_search_sources": []},
    {"web_search_used": False, "web_search_sources": ["https://example.com"]},
    *({"web_search_used": True, "web_search_sources": [url]} for url in (
        "javascript:alert(1)", "https://user:password@example.com", "http://127.0.0.1/a",
        "https://example.com/%0asecret", {"url": "https://example.com", "signature": "opaque"})),
    {"web_search_used": True, "web_search_sources": ["https://example.com"] * 65},
])
def test_invalid_web_metadata_rejects_whole_document(changes):
    value = tasks()
    value[0]["generations"][-1].update(changes)
    with pytest.raises(ContractError):
        handoff.PortableTaskDocument.from_tasks(value)
