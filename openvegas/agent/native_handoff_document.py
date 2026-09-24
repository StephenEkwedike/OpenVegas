"""Bounded, server-assembled public task history; never execution authority.

This codec does not prove ownership, settlement, complete receipts, or file
access. The transactional assembler must establish those before constructing a
document, and reauthorize files at dispatch. No HTTP history-import API uses it.
Source provider envelopes and private mutation preparations are not inputs.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.conversation import _SECRET

KIND = "openvegas.native-task-public.v1"
MAX_DOCUMENT_BYTES = 1_000_000
MAX_TEXT_BYTES = 64_000
MAX_TASKS = 32
MAX_GENERATIONS = 128
MAX_OBSERVATIONS = 128
MAX_DEPTH = 24
MAX_NODES = 20_000


def _fail() -> None:
    raise ContractError(
        APIErrorCode.HANDOFF_BLOCKED,
        "Task history is unsupported, incomplete, sensitive or over its transfer bound; "
        "nothing was transferred or truncated.",
    ) from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _constant(_value):
    _fail()


def _text(value, *, empty=False, allow_bom=False):
    if type(value) is not str or (not empty and not value.strip()):
        _fail()
    if len(value) > MAX_TEXT_BYTES or len(value.encode("utf-8")) > MAX_TEXT_BYTES or _SECRET.search(value):
        _fail()
    if any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} and c not in "\n\r\t"
           and not (allow_bom and c == "\ufeff")
           for c in value):
        _fail()


def _shape(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() <= set(required) | set(optional):
        _fail()


def _fields(value, required, optional=None):
    optional = optional or {}
    _shape(value, required, optional)
    for key, expected in (required | optional).items():
        if key not in value:
            continue
        item = value[key]
        if type(item) is not expected:
            _fail()
        if expected is int and not -(2**63) <= item < 2**63:
            _fail()


def _digest(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        _fail()


def _file_observation(value):
    _shape(value, {"exists", "sha256", "bytes"})
    if type(value["exists"]) is not bool or type(value["bytes"]) is not int:
        _fail()
    if not 0 <= value["bytes"] <= 32768:
        _fail()
    if value["exists"]:
        _digest(value["sha256"])
    elif value["sha256"] is not None or value["bytes"] != 0:
        _fail()


def _arguments(tool, args):
    schemas = {
        "Read": ({}, {"path": str, "filepath": str, "max_bytes": int,
                       "result_content_max_chars": int}),
        "List": ({}, {"path": str, "recursive": bool, "max_entries": int}),
        "Search": ({"pattern": str}, {"path": str, "max_files": int, "max_matches": int}),
        "Bash": ({"command": str}, {}),
        "Write": ({"filepath": str, "content": str}, {"write_mode": str}),
        "FindAndReplace": ({"filepath": str, "old_string": str, "new_string": str},
                           {"replace_all": bool}),
        "InsertAtEnd": ({"filepath": str, "content": str}, {}),
    }
    if type(tool) is not str or tool not in schemas:
        _fail()
    _fields(args, *schemas[tool])
    if tool == "Read":
        aliases = {"path", "filepath"} & args.keys()
        if not aliases or (len(aliases) == 2 and args["path"] != args["filepath"]):
            _fail()
    if any(type(v) is int and v < 1 for v in args.values()):
        _fail()
    for key in {"path", "filepath", "command", "pattern"} & args.keys():
        _text(args[key])
    if tool in {"Write", "FindAndReplace", "InsertAtEnd"}:
        from openvegas.agent.native_mutation import _call

        _call({"tool_name": tool, "arguments": args})


def _payload(tool, result):
    status, payload = result["status"], result["payload"]
    if status not in {"succeeded", "failed", "blocked"}:
        _fail()
    # Error results are public observations, not permission to retry a tool.
    if status != "succeeded" and type(payload) is dict and "detail" in payload:
        _fields(payload, {"ok": bool, "reason_code": str, "detail": str})
        if payload["ok"] or tool in {"Write", "FindAndReplace", "InsertAtEnd"}:
            _fail()
        return
    if tool == "Read":
        _fields(payload, {"ok": bool, "path": str, "content": str,
                          "content_truncated": bool, "truncated": bool,
                          "bytes_read": int, "max_bytes": int})
        if payload["content_truncated"] or payload["truncated"]:
            _fail()
        if not 0 <= payload["bytes_read"] <= payload["max_bytes"]:
            _fail()
        if payload["content"] != result["stdout"]:
            _fail()
    elif tool == "List":
        _fields(payload, {"ok": bool, "path": str, "recursive": bool, "entries": list,
                          "truncated": bool, "max_entries": int})
        if payload["truncated"]:
            _fail()
        if not 0 <= len(payload["entries"]) <= payload["max_entries"]:
            _fail()
        for entry in payload["entries"]:
            _fields(entry, {"path": str, "kind": str, "size": int})
            if entry["kind"] not in {"dir", "file"} or entry["size"] < 0:
                _fail()
    elif tool == "Search":
        _fields(payload, {"ok": bool, "pattern": str, "regex": bool, "path": str,
                          "recursive": bool, "files_scanned": int, "max_files": int,
                          "matches": list, "max_matches": int, "truncated": bool})
        if payload["truncated"]:
            _fail()
        if (not 0 <= len(payload["matches"]) <= payload["max_matches"]
                or not 0 <= payload["files_scanned"] <= payload["max_files"]):
            _fail()
        for match in payload["matches"]:
            _fields(match, {"path": str, "line": int, "column": int, "text": str})
            if match["line"] < 1 or match["column"] < 1:
                _fail()
    elif tool == "Bash":
        _fields(payload, {"ok": bool, "requested_command": str, "effective_command": str,
                          "shell_wrapper": str, "execution_cwd": str, "exit_code": int,
                          "final_status_message": str},
                {"duration_ms": int, "reason_code": str, "status": str, "job_id": str})
        if payload.get("status") not in {None, "completed", "foreground_result"}:
            _fail()
        if (status == "succeeded") != (payload["exit_code"] == 0):
            _fail()
    else:
        # Only the accepted public runtime proof, never the private source/patch.
        _shape(payload, {"native_mutation_proof"})
        proof = payload["native_mutation_proof"]
        _shape(proof, {"kind", "contract_sha256", "relative_path", "observed_before",
                       "observed_after", "outcome", "reason"})
        if (status != "succeeded" or proof["kind"] != "runtime_observed_file_v1"
                or proof["outcome"] not in {"applied", "no_change"} or proof["reason"] is not None
                or result["stdout"] or result["stderr"]):
            _fail()
        _digest(proof["contract_sha256"])
        _text(proof["relative_path"])
        _file_observation(proof["observed_before"])
        _file_observation(proof["observed_after"])
        if proof["observed_after"]["exists"] is not True:
            _fail()
        if proof["outcome"] == "no_change" and proof["observed_before"] != proof["observed_after"]:
            _fail()
        return
    if payload["ok"] is not (status == "succeeded"):
        _fail()


def _observation(value):
    _fields(value, {"tool_name": str, "arguments": dict, "result": dict})
    _arguments(value["tool_name"], value["arguments"])
    result = value["result"]
    _fields(result, {"status": str, "payload": dict, "stdout": str, "stderr": str})
    _payload(value["tool_name"], result)
    if (value["tool_name"] in {"Write", "FindAndReplace", "InsertAtEnd"}
            and result["payload"]["native_mutation_proof"]["relative_path"] != value["arguments"]["filepath"]):
        _fail()
    if (value["tool_name"] == "Bash" and "requested_command" in result["payload"]
            and any(result["payload"][name] != value["arguments"]["command"]
                    for name in ("requested_command", "effective_command"))):
        _fail()


def _web_sources(generation):
    if not ({"web_search_used", "web_search_sources"} & generation.keys()):
        return
    from openvegas.gateway.openrouter_web import WebValidationError, _public_url

    if (type(generation.get("web_search_used")) is not bool
            or type(generation.get("web_search_sources")) is not list
            or len(generation["web_search_sources"]) > 64
            or (generation["web_search_sources"] and not generation["web_search_used"])):
        _fail()
    try:
        for source in generation["web_search_sources"]:
            _text(source)
            if _public_url(source) != source:
                _fail()
    except WebValidationError:
        _fail()


def _validate(value):
    from server.services.attachment_history import validate_refs

    _shape(value, {"kind", "tasks"})
    if value["kind"] != KIND or type(value["tasks"]) is not list or not 1 <= len(value["tasks"]) <= MAX_TASKS:
        _fail()
    generations = observations = 0
    files = {}
    for task in value["tasks"]:
        _fields(task, {"user_text": str, "attachment_refs": list, "generations": list})
        _text(task["user_text"])
        refs = validate_refs(task["attachment_refs"]) if task["attachment_refs"] else []
        for ref in refs:
            if ref["file_id"] == "00000000-0000-0000-0000-000000000000":
                _fail()
            if ref["file_id"] in files and files[ref["file_id"]] != ref["sha256"]:
                _fail()
            files[ref["file_id"]] = ref["sha256"]
        if len(files) > 8 or not task["generations"]:
            _fail()
        for generation in task["generations"]:
            _fields(generation, {"assistant_text": str, "observations": list},
                    {"web_search_used": bool, "web_search_sources": list})
            _web_sources(generation)
            generations += 1
            observations += len(generation["observations"])
            if (generations > MAX_GENERATIONS or observations > MAX_OBSERVATIONS
                    or len(generation["observations"]) > 16):
                _fail()
            _text(generation["assistant_text"], empty=bool(generation["observations"]))
            for observation in generation["observations"]:
                _observation(observation)
        if task["generations"][-1]["observations"]:
            _fail()


def _tree(value):
    pending, visited, size = [(value, 0)], 0, 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if visited > MAX_NODES or depth > MAX_DEPTH:
            _fail()
        if type(item) is dict:
            if len(item) > MAX_NODES or any(type(k) is not str for k in item):
                _fail()
            pending.extend((v, depth + 1) for v in item.values())
            pending.extend((k, depth + 1) for k in item)
        elif type(item) is list:
            if len(item) > MAX_NODES:
                _fail()
            pending.extend((v, depth + 1) for v in item)
        elif type(item) is str:
            _text(item, empty=True, allow_bom=True)
            size += len(item.encode("utf-8"))
            if size > MAX_DOCUMENT_BYTES:
                _fail()
        elif type(item) is int:
            if not -(2**63) <= item < 2**63:
                _fail()
        elif item is not None and type(item) is not bool:
            _fail()


def _canonical(raw):
    try:
        if (type(raw) is not str or len(raw) > MAX_DOCUMENT_BYTES
                or len(raw.encode("utf-8")) > MAX_DOCUMENT_BYTES):
            _fail()
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
        _tree(value)
        from openvegas.agent.tool_cas import redaction_required

        if redaction_required(value):
            _fail()
        _validate(value)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            _fail()
        return encoded
    except (ValueError, TypeError, UnicodeError, RecursionError, ContractError):
        _fail()


@dataclass(frozen=True, repr=False)
class PortableTaskDocument:
    _json: str = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "_json", _canonical(self._json))

    def __repr__(self):
        return "<PortableTaskDocument private>"

    @classmethod
    def from_tasks(cls, tasks: list[dict[str, Any]]) -> PortableTaskDocument:
        try:
            _tree(tasks)
            raw = json.dumps({"kind": KIND, "tasks": tasks}, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            _fail()
        return cls(raw)

    def values(self) -> dict:
        return json.loads(self._json)

    def to_json(self) -> str:
        return self._json

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self._json.encode("utf-8")).hexdigest()

    @property
    def byte_bound(self) -> int:
        return len(self._json.encode("utf-8"))
