"""Exercise the actual chat prompt against the advertised native tool schemas."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from openvegas import cli
from openvegas.gateway.openrouter import _flat_tool, local_tool_definitions


def chat_prompt(provider, model):
    # Compile the actual nested prompt builder with its explicit closure inputs.
    tree = ast.parse(Path(cli.__file__).read_text())
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
             and n.name == "_tool_protocol_prompt"]
    assert len(nodes) == 1
    namespace = {
        "json": json, "workspace_root": "/workspace", "plan_mode": False,
        "approval_mode": "ask", "current_provider": provider, "current_model": model,
        "_local_tool_usage_prompt": cli._local_tool_usage_prompt,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), cli.__file__, "exec"), namespace)  # noqa: S102 - Trusted repository function only.
    return namespace["_tool_protocol_prompt"](
        "Read fixture.txt", [], None, web_search_effective=False, attachment_context="",
    )


@pytest.mark.parametrize("model", ["google/gemini-2.5-flash-lite", "google/schema-fixture"])
def test_google_prompt_matches_every_flat_function_field(model):
    prompt = chat_prompt("openrouter", model)
    usage = cli._local_tool_usage_prompt("openrouter", model)
    assert usage in prompt
    assert "Read({ path," in prompt
    assert "Read({ filepath" not in prompt
    definitions = local_tool_definitions(model)
    lines = usage.splitlines()
    assert len(lines) == len(definitions) == 7
    for line, tool in zip(lines, definitions, strict=True):
        function = tool["function"]
        schema = function["parameters"]
        name, fields = line.strip().removeprefix("- ").split("({ ")
        assert name == function["name"]
        fields = fields.removesuffix(" })").split(", ")
        assert {f.rstrip("?") for f in fields} == set(schema["properties"])
        assert {f for f in fields if not f.endswith("?")} == set(schema["required"])
        sample = {
            f: {"string": "fixture", "integer": 1, "boolean": False}[schema["properties"][f]["type"]]
            for f in fields if not f.endswith("?")
        }
        normalized = _flat_tool({"name": name, "arguments": json.dumps(sample)}, model)
        assert normalized["tool_name"] == name
        assert normalized["arguments"] == sample


def test_google_read_prompt_survives_real_local_preprocessing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENVEGAS_TOOL_ABI_MODE", "compat")
    call = _flat_tool({"name": "Read", "arguments": '{"path":"fixture.txt"}'}, "google/test")
    prepared, error = cli._preprocess_tool_request_for_runtime(
        tool_req=call, user_message="Read fixture.txt", model_text="",
        workspace_root=str(tmp_path), tool_observations=[],
    )
    assert error is None
    assert prepared["tool_name"] == "fs_read"
    assert prepared["arguments"] == {"path": "fixture.txt"}


@pytest.mark.parametrize("provider,model", [
    ("openai", "example"), ("anthropic", "example"), ("gemini", "example"),
    ("openrouter", "openai/example"), ("openrouter", "anthropic/example"),
    ("openrouter", "mistralai/example"),
])
def test_generic_dispatcher_instructions_remain_unchanged(provider, model):
    prompt = chat_prompt(provider, model)
    assert "Read({ filepath })" in prompt
    assert "Write({ filepath, content, write_mode? })" in prompt
    assert "Prior tool observations (JSON): []" in prompt
    assert "User request: Read fixture.txt" in prompt
