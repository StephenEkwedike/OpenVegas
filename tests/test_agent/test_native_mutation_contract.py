"""Offline pure native mutation contracts; observations are not disk attestations."""
from __future__ import annotations

import copy
import hashlib
import json
import random
from dataclasses import FrozenInstanceError

import pytest

from openvegas.agent.native_mutation import (
    MAX_TEXT_BYTES,
    MutationPlan,
    NativeMutationError,
    apply_exact_patch,
    build_mutation_plan,
    validate_plan,
    validate_relative_path,
)


def call(name="Write", **args):
    return {"type": "tool_call", "tool_name": name, "arguments": {"filepath": "src/a.txt", **args},
            "provider_call_id": "call_original", "shell_mode": "mutating", "timeout_sec": 30}


def source(text):
    return {"exists": text is not None, "content_utf8": text}


def plan(old=None, new="new", mode="replace"):
    return build_mutation_plan(call(content=new, write_mode=mode), source(old))


def assert_roundtrip(value, old):
    assert type(value) is MutationPlan
    raw = b"" if old is None else old.encode("utf-8")
    assert apply_exact_patch(raw, value.patch, value.relative_path, old is not None) == value.content_utf8.encode()
    assert value.before_sha256 == (hashlib.sha256(raw).hexdigest() if old is not None else None)
    assert value.before_bytes == len(raw)
    assert value.after_bytes == len(value.content_utf8.encode())
    assert value.after_sha256 == hashlib.sha256(value.content_utf8.encode()).hexdigest()
    assert validate_plan(value.document()) == value


@pytest.mark.parametrize("old,new", [
    (None, ""), (None, "a"), (None, "a\n"), ("", ""), ("a", "a"), ("a", ""),
    ("a", "b"), ("a\n", "b"), ("a", "b\n"), ("a\r\n", "b\r\n"),
    ("a\r\nb\nc\r", "a\r\nB\nc\r"), ("\ufeffcafe\u0301\n", "\ufeffcaf\u00e9"),
    ("a\u2028b\u2029c", "d\u2028e\u2029f"), ("\n\n", "\n"),
    ("--- header\n+++ header\n@@ text\n", "\\ No newline at end of file"),
    ("  \t\r\n", "\t \r"),
])
def test_exact_byte_roundtrips(old, new):
    value = plan(old, new)
    assert value.no_change is (old is not None and old == new)
    assert_roundtrip(value, old)


def test_creation_empty_is_not_missing_noop():
    created = plan(None, "")
    unchanged = plan("", "")
    assert not created.no_change and created.patch
    assert unchanged.no_change and unchanged.patch == ""
    assert created.contract_sha256 != unchanged.contract_sha256
    with pytest.raises(NativeMutationError):
        apply_exact_patch(b"", "", "src/a.txt", False)


@pytest.mark.parametrize("name", ["Write", "InsertAtEnd"])
@pytest.mark.parametrize("old,tail", [("abc", "abc"), ("abc", "\n"), ("x\r\n", "tail"), (None, ""), ("", "\t")])
def test_literal_append_no_separator_or_dedup(name, old, tail):
    args = {"content": tail}
    if name == "Write":
        args["write_mode"] = "append"
    value = build_mutation_plan(call(name, **args), source(old))
    assert value.content_utf8 == (old or "") + tail
    assert_roundtrip(value, old)


@pytest.mark.parametrize("old,needle,replacement,all_,expected", [
    ("a a", "a", "x", True, "x x"), ("abc", "b", "", False, "ac"),
    ("aaaaa", "aa", "X", True, "XXa"), ("abc", "b", "b", False, "abc"),
    ("a\r\nb\n", "b", "B", False, "a\r\nB\n"), ("\ufeffa", "a", "z", False, "\ufeffz"),
])
def test_find_replace_exact_nonoverlapping(old, needle, replacement, all_, expected):
    value = build_mutation_plan(call("FindAndReplace", old_string=needle, new_string=replacement, replace_all=all_), source(old))
    assert value.content_utf8 == expected
    assert_roundtrip(value, old)


@pytest.mark.parametrize("old,args,reason", [
    (None, {"old_string": "a", "new_string": "b"}, "native_mutation_source_missing"),
    ("a", {"old_string": "", "new_string": "b"}, "native_mutation_match_missing"),
    ("a", {"old_string": "b", "new_string": "c"}, "native_mutation_match_missing"),
    ("a a", {"old_string": "a", "new_string": "c"}, "native_mutation_match_ambiguous"),
    ("a", {"old_string": "a", "new_string": "c", "replace_all": 1}, "native_mutation_shape"),
])
def test_find_replace_refusals(old, args, reason):
    with pytest.raises(NativeMutationError) as error:
        build_mutation_plan(call("FindAndReplace", **args), source(old))
    assert error.value.reason_code == reason


def test_write_mode_is_not_inferred():
    with pytest.raises(NativeMutationError, match="native_mutation_mode"):
        build_mutation_plan(call(content="x"), source("old"))
    assert build_mutation_plan(call(content="x"), source(None)).content_utf8 == "x"


@pytest.mark.parametrize("path", ["", "/a", "//host/a", "a/", "a//b", "../a", "a/../b", "./a", "a/./b",
    "a\\b", "C:/a", "a:b", "a\nb", "a\tb", " a", "a ", "a.", "a/.. ", "~home/a", "NUL", "nul.txt",
    "CON", "PRN.log", "aux", "COM1.txt", "lPt9", "COM\u00b9", "CLOCK$", "CONIN$", "a?b", "a*b", 'a"b',
    "a|b", "a<b", "a>b", "caf\u0065\u0301", "a\u202eb", "a\u2028b", "a\x7fb", "a\x85b", "a/" + "b" * 256])
def test_ambiguous_or_unsafe_lexical_path_rejected(path):
    with pytest.raises(NativeMutationError):
        validate_relative_path(path)


@pytest.mark.parametrize("path", ["src/file.txt", "a b.txt", "caf\u00e9.txt", ".gitignore", "COM10", "normal", "a-b_c/123"])
def test_lexical_path_only_no_disk_claim(path):
    assert validate_relative_path(path) == path


@pytest.mark.parametrize("change", [
    {"extra": "PRIVATE_MARKER"}, {"arguments": []}, {"arguments": {"filepath": "a", "content": "x", "patch": "x"}},
    {"tool_name": []}, {"tool_name": "Bash"}, {"shell_mode": {}}, {"shell_mode": "unrestricted"},
    {"timeout_sec": True}, {"timeout_sec": 0}, {"provider_call_id": []}, {"type": "other"},
])
def test_strict_original_call_shape_safe_errors(change):
    candidate = call(content="x")
    candidate.update(change)
    with pytest.raises(NativeMutationError) as error:
        build_mutation_plan(candidate, source(None))
    assert "PRIVATE_MARKER" not in str(error.value)


@pytest.mark.parametrize("observed", [{}, {"exists": 1, "content_utf8": ""}, {"exists": False, "content_utf8": ""},
    {"exists": True, "content_utf8": None}, {"exists": True, "content_utf8": "", "sha256": "claimed"}])
def test_strict_observation_shape(observed):
    with pytest.raises(NativeMutationError):
        build_mutation_plan(call(content="x"), observed)


@pytest.mark.parametrize("text,reason", [("\x00", "controls"), ("\x1b[31m", "controls"), ("\u202e", "controls"),
    ("\ud800", "encoding"), ("sk-" + "s" * 32, "sensitive"), ("password=PRIVATE_MARKER", "sensitive")])
def test_secret_control_and_encoding_refused_not_echoed(text, reason):
    with pytest.raises(NativeMutationError) as error:
        plan("old", text)
    assert error.value.reason_code == "native_mutation_" + reason
    assert text not in str(error.value)


def test_bounds_before_replace_expansion_and_utf8_not_character_count():
    for text in ("a" * (MAX_TEXT_BYTES + 1), "\u00e9" * (MAX_TEXT_BYTES // 2 + 1)):
        with pytest.raises(NativeMutationError, match="bounds"):
            plan(None, text)
    exact = "a" * MAX_TEXT_BYTES
    assert_roundtrip(plan(None, exact), None)
    with pytest.raises(NativeMutationError, match="bounds"):
        build_mutation_plan(call("FindAndReplace", old_string="a", new_string="b" * MAX_TEXT_BYTES, replace_all=True), source(exact))
    with pytest.raises(NativeMutationError, match="bounds"):
        build_mutation_plan(call("InsertAtEnd", content="b"), source(exact))


def test_input_and_document_are_detached_and_plan_frozen():
    original = call(content="PRIVATE_NEW")
    observed = source("PRIVATE_OLD")
    original["arguments"]["write_mode"] = "replace"
    preserved = copy.deepcopy(original)
    value = build_mutation_plan(original, observed)
    assert original == preserved
    document = value.document()
    original["arguments"]["content"] = "changed"
    observed["content_utf8"] = "changed"
    document["original_call"]["arguments"]["content"] = "changed"
    document["observed_source"]["content_utf8"] = "changed"
    assert value.document()["original_call"] == preserved
    assert value.document()["observed_source"] == source("PRIVATE_OLD")
    assert "PRIVATE" not in repr(value)
    with pytest.raises(FrozenInstanceError):
        value.content_utf8 = "changed"
    assert validate_plan(value.document()) == value


@pytest.mark.parametrize("field,replacement", [
    ("version", True), ("version", 2), ("before_exists", 1), ("before_bytes", True), ("after_bytes", 999),
    ("before_sha256", "0" * 64), ("after_sha256", "0" * 64), ("contract_sha256", "0" * 64),
    ("relative_path", "other"), ("content_utf8", "other"), ("patch", ""), ("no_change", 1),
])
def test_document_integrity_every_field(field, replacement):
    document = plan("old", "new").document()
    document[field] = replacement
    with pytest.raises(NativeMutationError):
        validate_plan(document)


def test_hash_only_forgery_unknown_fields_and_call_commitment():
    value = plan("old", "new")
    document = value.document()
    document["patch"] = document["patch"].replace("+new", "+BAD")
    payload = {k: v for k, v in document.items() if k != "contract_sha256"}
    document["contract_sha256"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(NativeMutationError):
        validate_plan(document)
    document = value.document()
    document["extra"] = "x"
    with pytest.raises(NativeMutationError):
        validate_plan(document)
    a = call(content="new", write_mode="replace")
    b = copy.deepcopy(a)
    b["provider_call_id"] = "another"
    assert build_mutation_plan(a, source("old")).contract_sha256 != build_mutation_plan(b, source("old")).contract_sha256


@pytest.mark.parametrize("transform", [
    lambda p: p + "garbage\n", lambda p: p + p, lambda p: p.replace("a/src/a.txt", "a/other"),
    lambda p: p.replace("b/src/a.txt", "b/other"), lambda p: p.replace("-1,1", "-0,1"),
    lambda p: p.replace("+1,1", "+1,2"), lambda p: p.replace("-old", "-OLD"),
    lambda p: p.replace("@@ -1,1 +1,1 @@", "@@ -1,1 +1,1 @@ context"),
    lambda p: p.replace("-old", " old"), lambda p: p.replace("\\ No newline at end of file\n", "", 1),
    lambda p: "diff --git a/a b/a\n" + p, lambda p: p.rstrip("\n"),
])
def test_malformed_or_noncanonical_patch_rejected(transform):
    value = plan("old", "new")
    with pytest.raises(NativeMutationError):
        apply_exact_patch(b"old", transform(value.patch), "src/a.txt", True)


def test_patch_wrong_baseline_and_presence_rejected():
    value = plan("old", "new")
    for raw, exists in ((b"other", True), (b"old", False), (b"", False)):
        with pytest.raises(NativeMutationError):
            apply_exact_patch(raw, value.patch, "src/a.txt", exists)


def test_deterministic_generated_text_roundtrips():
    rng = random.Random(1421)
    alphabet = "ab +@-\\\r\n\t\ufeff\u00e9\u0301\u2028"
    for _ in range(150):
        old = None if rng.randrange(7) == 0 else "".join(rng.choices(alphabet, k=rng.randrange(60)))
        new = "".join(rng.choices(alphabet, k=rng.randrange(60)))
        assert_roundtrip(plan(old, new), old)


def test_hostile_documents_are_bounded_with_fixed_error():
    value = plan("old", "new").document()
    value["original_call"]["arguments"] = value
    with pytest.raises(NativeMutationError, match="bounds"):
        validate_plan(value)
    with pytest.raises(NativeMutationError):
        validate_plan({"bogus": "PRIVATE"})
    assert str(NativeMutationError("PRIVATE_PAYLOAD")) == "native_mutation_shape"


@pytest.mark.parametrize("old,new", [
    ("\n" * MAX_TEXT_BYTES, "x" * MAX_TEXT_BYTES),
    ("x" * MAX_TEXT_BYTES, "\n" * MAX_TEXT_BYTES),
    ("\t" * MAX_TEXT_BYTES, "\\" * MAX_TEXT_BYTES),
    ("\u00e9" * (MAX_TEXT_BYTES // 2), "\r\n" * (MAX_TEXT_BYTES // 2)),
])
def test_worst_case_record_and_json_expansion_is_bounded(old, new):
    assert_roundtrip(plan(old, new), old)


def test_fixed_errors_and_private_path_no_secret_echo():
    assert str(NativeMutationError({"PRIVATE": "input"})) == "native_mutation_shape"
    with pytest.raises(NativeMutationError) as error:
        validate_relative_path("sk-" + "S" * 32)
    assert error.value.reason_code == "native_mutation_sensitive"
    assert "SSSS" not in repr(error.value)


def test_call_and_document_key_order_does_not_change_canonical_commitment():
    a = call(content="new", write_mode="replace")
    b = dict(reversed(list(a.items())))
    b["arguments"] = dict(reversed(list(a["arguments"].items())))
    left, right = build_mutation_plan(a, source("old")), build_mutation_plan(b, source("old"))
    assert left.contract_sha256 == right.contract_sha256
    document = dict(reversed(list(left.document().items())))
    assert validate_plan(document) == left


@pytest.mark.parametrize("marker_patch", [
    "--- a/src/a.txt\n+++ b/src/a.txt\n@@ -1,1 +1,1 @@\n\\ No newline at end of file\n-old\n+new\n",
    "--- a/src/a.txt\n+++ b/src/a.txt\n@@ -1,1 +1,1 @@\n-old\n\\ No newline at end of file\n\\ No newline at end of file\n+new\n",
    "--- a/src/a.txt\n+++ b/src/a.txt\n@@ -1,1 +1,1 @@\n+new\n-old\n",
])
def test_misordered_or_repeated_patch_marker_rejected(marker_patch):
    with pytest.raises(NativeMutationError):
        apply_exact_patch(b"old", marker_patch, "src/a.txt", True)


def test_fake_observation_not_treated_as_server_attested_filesystem():
    # This function has no disk access: consistent fabricated input is internally
    # valid. Authentication, source capture, approval and readback are separate.
    value = plan("client-reported source", "literal replacement")
    assert validate_plan(value.document()) == value
    assert "attested" not in value.document()
    assert "trusted_file" not in value.document()
