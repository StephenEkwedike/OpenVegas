"""Historical handoff proof recognizes only one authenticated output cap."""
import pytest

from openvegas.contracts.errors import ContractError
from server.services.native_handoff_provenance import _output_budget


@pytest.mark.parametrize("key", ["max_tokens", "max_completion_tokens"])
def test_openrouter_wire_spelling_preserves_exact_budget(key):
    assert _output_budget({key: 128}, "openrouter") == 128


@pytest.mark.parametrize("payload", [
    {}, {"max_tokens": 128, "max_completion_tokens": 128},
    {"max_tokens": 128, "max_completion_tokens": 256},
    {"max_completion_tokens": None}, {"max_completion_tokens": True},
    {"max_completion_tokens": 0}, {"max_completion_tokens": "128"},
])
def test_missing_ambiguous_or_invalid_budget_rejected(payload):
    with pytest.raises(ContractError):
        _output_budget(payload, "openrouter")


def test_alias_does_not_expand_other_provider_contracts():
    with pytest.raises(ContractError):
        _output_budget({"max_completion_tokens": 128}, "anthropic")
    assert _output_budget({"max_tokens": 128}, "anthropic") == 128
