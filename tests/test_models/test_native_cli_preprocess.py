"""Native requests must not enter legacy inference/rewrite/synthesis paths."""
from copy import deepcopy
from uuid import uuid4

import pytest

from openvegas import cli


def prepare(call, tmp_path):
    return cli._preprocess_tool_request_for_runtime(
        tool_req=call, user_message="Overwrite a different file with inferred code",
        model_text="```python\nprint('not the requested tool')\n```", workspace_root=str(tmp_path),
        tool_observations=[], force_patch_intent=True, native_exact=True)


@pytest.mark.parametrize("name,args,expected", [
    ("Read", {"filepath": "notes.txt"}, {"filepath": "notes.txt", "path": "notes.txt"}),
    ("List", {}, {"path": "."}),
    ("Search", {"pattern": "needle"}, {"pattern": "needle", "path": ".", "max_files": 250, "max_matches": 120}),
    ("Bash", {"command": "python example.py"}, {"command": "python example.py"}),
])
def test_exact_requests_are_not_promoted_rewritten_or_inferred(tmp_path, monkeypatch, name, args, expected):
    def forbidden(*a, **k):
        raise AssertionError("Native call reached a legacy rewrite")
    monkeypatch.setattr(cli, "_promote_tool_call_for_patch_intent", forbidden)
    monkeypatch.setattr(cli, "_rewrite_shell_command_for_env", forbidden)
    call = {"type": "tool_call", "tool_name": name, "arguments": args, "shell_mode": "read_only",
            "timeout_sec": 30, "provider_call_id": "call-original", "native_inference_request_id": str(uuid4())}
    original = deepcopy(call)
    result, error = prepare(call, tmp_path)
    assert error is None and result["arguments"] == expected
    assert result["native_inference_request_id"] == call["native_inference_request_id"]
    assert result["provider_call_id"] == "call-original"
    assert call == original


@pytest.mark.parametrize("name,args", [
    ("Read", {}), ("Search", {}), ("Bash", {}),
    ("Write", {"filepath": "notes.txt", "content": "must not convert into a guessed patch"}),
    ("FindAndReplace", {"filepath": "notes.txt", "find": "a", "replace": "b"}),
    ("InsertAtEnd", {"filepath": "notes.txt", "content": "extra"}),
])
def test_missing_arguments_or_unverified_patch_transform_stays_blocked(tmp_path, name, args):
    result, error = prepare({"tool_name": name, "arguments": args, "shell_mode": "read_only"}, tmp_path)
    assert result is None and error["status"] == "blocked"
