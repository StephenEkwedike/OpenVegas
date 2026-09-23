"""Pure exact-byte plans over runtime observations, NOT filesystem attestations.

Documents contain private source/call material. They are not public receipts or
execution permission. The integrating service must enforce ownership, approval,
path/secret policy (including configured blockers), and runtime observations.
The v1 patch dialect is a single full-context hunk, avoiding fuzzy application and
quadratic diff matching. No filesystem or provider work occurs here.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, NoReturn

from openvegas.gateway.conversation import _SECRET

MAX_TEXT_BYTES = 32_768
MAX_PATCH_BYTES = 262_144
MAX_DOCUMENT_BYTES = 524_288
VERSION = 1
_NO_NEWLINE = "\\ No newline at end of file\n"
_CALL_FIELDS = frozenset({"tool_name", "arguments", "type", "shell_mode", "timeout_sec",
                          "provider_call_id", "native_inference_request_id"})
_PLAN_FIELDS = frozenset({"version", "relative_path", "before_exists", "before_sha256",
                         "before_bytes", "after_sha256", "after_bytes", "content_utf8",
                         "patch", "no_change", "contract_sha256", "original_call",
                         "observed_source"})
_DEVICE = re.compile(r"(?:con|prn|aux|nul|clock\$|conin\$|conout\$|com[1-9]|lpt[1-9])\Z", re.IGNORECASE)
_REASONS = frozenset({"native_mutation_shape", "native_mutation_bounds",
                      "native_mutation_encoding", "native_mutation_controls",
                      "native_mutation_sensitive", "native_mutation_path",
                      "native_mutation_mode", "native_mutation_source_missing",
                      "native_mutation_match_missing", "native_mutation_match_ambiguous",
                      "native_mutation_patch", "native_mutation_integrity"})


class NativeMutationError(ValueError):
    """Fixed safe reason only; submitted strings never enter exception messages."""

    def __init__(self, reason_code: str = "native_mutation_shape") -> None:
        self.reason_code = (
            reason_code if type(reason_code) is str and reason_code in _REASONS else "native_mutation_shape"
        )
        super().__init__(self.reason_code)


def _fail(reason: str = "native_mutation_shape") -> NoReturn:
    raise NativeMutationError(reason) from None


def _encoded(value: Any, limit: int) -> bytes:
    if type(value) is not str:
        _fail()
    if len(value) > limit:
        _fail("native_mutation_bounds")
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail("native_mutation_encoding")
    if len(raw) > limit:
        _fail("native_mutation_bounds")
    return raw


def _text(value: Any) -> bytes:
    raw = _encoded(value, MAX_TEXT_BYTES)
    if any((ord(c) < 32 and c not in "\r\n\t") or 127 <= ord(c) <= 159
           or unicodedata.category(c) == "Cf" and c != "\ufeff" for c in value):
        _fail("native_mutation_controls")
    if _SECRET.search(value):
        _fail("native_mutation_sensitive")
    return raw


def _canonical(value: Any) -> str:
    # Public inputs are shallow strict schemas. Bound arbitrary documents before
    # JSON encoding so cycles, deep nesting and huge scalars cannot exhaust it.
    pending = [(value, 0)]
    nodes = 0
    size = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 256 or depth > 5:
            _fail("native_mutation_bounds")
        if type(item) is dict:
            if len(item) > 32 or any(type(k) is not str for k in item):
                _fail()
            for k, v in item.items():
                size += len(_encoded(k, 256))
                pending.append((v, depth + 1))
        elif type(item) is str:
            size += len(_encoded(item, MAX_DOCUMENT_BYTES))
        elif type(item) is int:
            if abs(item) > 1_000_000:
                _fail("native_mutation_bounds")
        elif item is not None and type(item) is not bool:
            _fail()
        if size > MAX_DOCUMENT_BYTES:
            _fail("native_mutation_bounds")
    try:
        out = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()
    _encoded(out, MAX_DOCUMENT_BYTES)
    return out


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def validate_relative_path(value: str) -> str:
    """Lexical portable path check only; cannot detect symlinks or inspect disk."""
    _encoded(value, 1024)
    if _SECRET.search(value):
        _fail("native_mutation_sensitive")
    if (not value or value != unicodedata.normalize("NFC", value)
            or any(c in value for c in '\\:<>"|?*')
            or any(c.isspace() and c != " " or unicodedata.category(c)[0] == "C" for c in value)):
        _fail("native_mutation_path")
    for component in value.split("/"):
        lower = component.casefold()
        if (lower in {".git", ".ssh", ".aws", ".gnupg", ".openvegas", ".netrc", ".npmrc", ".pypirc",
                      "env.md", "test-accounts.md", "test-account.md"}
                or (lower == ".env" or lower.startswith(".env."))
                and lower not in {".env.example", ".env.sample", ".env.template"}):
            _fail("native_mutation_sensitive")
        if (not component or component in {".", ".."} or component != component.strip()
                or component.endswith(".") or component.startswith("~")
                or len(component.encode("utf-8")) > 255):
            _fail("native_mutation_path")
        stem = unicodedata.normalize("NFKC", component.split(".", 1)[0]).rstrip(" .")
        if _DEVICE.fullmatch(stem):
            _fail("native_mutation_path")
    return value


def _call(value: dict) -> dict:
    if (type(value) is not dict or not {"tool_name", "arguments"} <= value.keys()
            or not value.keys() <= _CALL_FIELDS):
        _fail()
    frozen = _canonical(value)
    if "type" in value and value["type"] != "tool_call":
        _fail()
    if "shell_mode" in value and (type(value["shell_mode"]) is not str or value["shell_mode"] not in {"read_only", "mutating"}):
        _fail()
    if "timeout_sec" in value and (type(value["timeout_sec"]) is not int or not 1 <= value["timeout_sec"] <= 300):
        _fail()
    for key in ("provider_call_id", "native_inference_request_id"):
        if key in value:
            token = value[key]
            if (type(token) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", token)
                    or _SECRET.search(token)):
                _fail()
    name = value["tool_name"]
    args = value["arguments"]
    if type(name) is not str or type(args) is not dict:
        _fail()
    schemas = {"Write": ({"filepath", "content"}, {"write_mode"}),
               "InsertAtEnd": ({"filepath", "content"}, set()),
               "FindAndReplace": ({"filepath", "old_string", "new_string"}, {"replace_all"})}
    if name not in schemas:
        _fail()
    required, optional = schemas[name]
    if not required <= args.keys() or not args.keys() <= required | optional:
        _fail()
    validate_relative_path(args["filepath"])
    for key in required - {"filepath"}:
        _text(args[key])
    if "replace_all" in args and type(args["replace_all"]) is not bool:
        _fail()
    if "write_mode" in args and (type(args["write_mode"]) is not str or args["write_mode"] not in {"replace", "append"}):
        _fail("native_mutation_mode")
    return json.loads(frozen)


def _source(value: dict) -> tuple[bool, bytes]:
    if type(value) is not dict or set(value) != {"exists", "content_utf8"} or type(value["exists"]) is not bool:
        _fail()
    if not value["exists"]:
        if value["content_utf8"] is not None:
            _fail()
        return False, b""
    return True, _text(value["content_utf8"])


def _lines(raw: bytes) -> list[str]:
    # Only LF separates patch records; CR and other Unicode line separators are
    # literal file content. This preserves mixed CRLF and final newline state.
    text = raw.decode("utf-8")
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _patch(source: bytes, target: bytes, path: str, exists: bool) -> str:
    if exists and source == target:
        return ""
    old, new = _lines(source), _lines(target)
    out = [f"--- {'a/' + path if exists else '/dev/null'}\n", f"+++ b/{path}\n",
           f"@@ -{1 if old else 0},{len(old)} +{1 if new else 0},{len(new)} @@\n"]
    for prefix, lines in (("-", old), ("+", new)):
        for line in lines:
            out.append(prefix + line)
            if not line.endswith("\n"):
                out.extend(("\n", _NO_NEWLINE))
    patch = "".join(out)
    _encoded(patch, MAX_PATCH_BYTES)
    return patch


def apply_exact_patch(source: bytes, patch: str, relative_path: str, exists: bool) -> bytes:
    """Apply only our canonical full-context single-file dialect, with no fuzz."""
    validate_relative_path(relative_path)
    if type(source) is not bytes or type(exists) is not bool:
        _fail()
    if len(source) > MAX_TEXT_BYTES:
        _fail("native_mutation_bounds")
    try:
        _text(source.decode("utf-8", errors="strict"))
    except UnicodeError:
        _fail("native_mutation_encoding")
    if not exists and source:
        _fail("native_mutation_patch")
    _encoded(patch, MAX_PATCH_BYTES)
    if patch == "":
        if not exists:
            _fail("native_mutation_patch")
        return source
    prefix = f"--- {'a/' + relative_path if exists else '/dev/null'}\n+++ b/{relative_path}\n"
    if not patch.startswith(prefix):
        _fail("native_mutation_patch")
    records = patch[len(prefix):].split("\n")
    if records[-1] != "" or not re.fullmatch(r"@@ -[01],[0-9]{1,5} \+[01],[0-9]{1,5} @@", records[0]):
        _fail("native_mutation_patch")
    old, new = [], []
    phase = "-"
    previous = None
    unterminated = set()
    for record in records[1:-1]:
        if record == _NO_NEWLINE[:-1]:
            if previous is None or previous in unterminated:
                _fail("native_mutation_patch")
            collection = old if previous == "-" else new
            collection[-1] = collection[-1][:-1]
            unterminated.add(previous)
            previous = None
            continue
        if not record or record[0] not in {"-", "+"}:
            _fail("native_mutation_patch")
        kind = record[0]
        if kind in unterminated or kind == "-" and phase == "+":
            _fail("native_mutation_patch")
        if kind == "+":
            phase = "+"
        (old if kind == "-" else new).append(record[1:] + "\n")
        previous = kind
    before = "".join(old).encode("utf-8")
    target_text = "".join(new)
    target = _text(target_text)
    if before != source or patch != _patch(source, target, relative_path, exists):
        _fail("native_mutation_patch")
    return target


@dataclass(frozen=True, repr=False)
class MutationPlan:
    relative_path: str
    before_exists: bool
    before_sha256: str | None
    before_bytes: int
    after_sha256: str
    after_bytes: int
    content_utf8: str
    patch: str
    no_change: bool
    contract_sha256: str
    version: int = VERSION
    _call_json: str = field(default="{}", repr=False)
    _observed_source_json: str = field(default="{}", repr=False)

    def __repr__(self) -> str:
        return f"MutationPlan(version={self.version}, before_bytes={self.before_bytes}, after_bytes={self.after_bytes})"

    def document(self) -> dict:
        """Fresh PRIVATE validation document; contains observed source and target."""
        return {"version": self.version, "relative_path": self.relative_path,
                "before_exists": self.before_exists, "before_sha256": self.before_sha256,
                "before_bytes": self.before_bytes, "after_sha256": self.after_sha256,
                "after_bytes": self.after_bytes, "content_utf8": self.content_utf8,
                "patch": self.patch, "no_change": self.no_change,
                "contract_sha256": self.contract_sha256,
                "original_call": json.loads(self._call_json),
                "observed_source": json.loads(self._observed_source_json)}


def build_mutation_plan(call: dict, observed_source: dict) -> MutationPlan:
    original = _call(call)
    exists, source = _source(observed_source)
    name, args = original["tool_name"], original["arguments"]
    if name == "Write":
        mode = args.get("write_mode")
        if exists and mode is None:
            _fail("native_mutation_mode")
        target = (source if mode == "append" else b"") + _text(args["content"])
    elif name == "InsertAtEnd":
        target = source + _text(args["content"])
    else:
        if not exists:
            _fail("native_mutation_source_missing")
        needle, replacement = _text(args["old_string"]), _text(args["new_string"])
        if not needle:
            _fail("native_mutation_match_missing")
        count = source.count(needle)
        if count == 0:
            _fail("native_mutation_match_missing")
        if count > 1 and not args.get("replace_all", False):
            _fail("native_mutation_match_ambiguous")
        # Bound expansion BEFORE allocating it, including replace-all worst cases.
        total = len(source) + count * (len(replacement) - len(needle))
        if total > MAX_TEXT_BYTES:
            _fail("native_mutation_bounds")
        target = source.replace(needle, replacement)
    content = target.decode("utf-8")
    _text(content)
    path = args["filepath"]
    patch = _patch(source, target, path, exists)
    if apply_exact_patch(source, patch, path, exists) != target:
        _fail("native_mutation_integrity")
    plan = MutationPlan(path, exists, _sha(source) if exists else None, len(source),
                        _sha(target), len(target), content, patch, exists and source == target, "",
                        _call_json=_canonical(original), _observed_source_json=_canonical(observed_source))
    document = plan.document()
    del document["contract_sha256"]
    digest = _sha(_canonical(document).encode("utf-8"))
    return replace(plan, contract_sha256=digest)


def validate_plan(document: dict) -> MutationPlan:
    """Rebuild all commitments. This checks claims, not ownership or disk truth."""
    if type(document) is not dict or set(document) != _PLAN_FIELDS:
        _fail()
    frozen = _canonical(document)
    # Rebuilding plus strict JSON comparison rejects bool/int substitution, stale
    # digests, extra fields and self-consistent hash-only patch tampering.
    original = json.loads(frozen)
    plan = build_mutation_plan(original["original_call"], original["observed_source"])
    if frozen != _canonical(plan.document()):
        _fail("native_mutation_integrity")
    return plan
