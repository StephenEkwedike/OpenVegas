"""Prompt hints and fresh provider schemas must advertise the same tools."""
import pytest

from openvegas.cli import _local_tool_usage_prompt
from openvegas.gateway.openrouter import local_tool_definitions


@pytest.mark.parametrize("family", ["openai", "anthropic", "google", "mistralai"])
def test_openrouter_hints_match_each_required_and_optional_field(family):
    model = family + "/synthetic-fixture"
    hints = _local_tool_usage_prompt("openrouter", model)
    expected = []
    for tool in local_tool_definitions(model):
        function = tool["function"]
        schema = function["parameters"]
        fields = ", ".join(
            name if name in schema["required"] else name + "?"
            for name in schema["properties"]
        )
        expected.append(f"  - {function['name']}({{ {fields} }})\n")
    assert hints == "".join(expected)
    assert "Read({ path," in hints
    assert "call_local_tool" not in hints


def test_direct_adapter_keeps_its_existing_hints():
    hints = _local_tool_usage_prompt("openai", "synthetic-fixture")
    assert "Read({ filepath })" in hints
