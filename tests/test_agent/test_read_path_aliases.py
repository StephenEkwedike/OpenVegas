"""Conflicting aliases cannot silently choose which local file is read."""
import pytest

from openvegas.agent.orchestration_service import AgentOrchestrationService
from openvegas.contracts.errors import ContractError


@pytest.mark.parametrize("tool", ["fs_read", "editor_open"])
@pytest.mark.parametrize("alias", ["filepath", "file_path", "target_path", "file"])
@pytest.mark.parametrize("other", ["private-canary-b.txt", "", None, 7])
def test_conflicting_read_paths_reject_before_normalization_and_validation(tool, alias, other):
    args = {"path": "private-canary-a.txt", alias: other}
    for operation in (
        AgentOrchestrationService._normalize_tool_arguments,
        AgentOrchestrationService._validate_tool_arguments,
    ):
        with pytest.raises(ContractError, match="Conflicting file path aliases") as exc:
            operation(tool_name=tool, arguments=args)
        assert "private-canary" not in str(exc.value)
        assert args == {"path": "private-canary-a.txt", alias: other}


@pytest.mark.parametrize("tool", ["fs_read", "editor_open"])
@pytest.mark.parametrize("args", [
    {"path": "fixture.txt"},
    {"filepath": "fixture.txt"},
    {"file_path": "fixture.txt"},
    {"target_path": "fixture.txt"},
    {"file": "fixture.txt"},
    {"file": {"path": "fixture.txt"}},
    {"path": "fixture.txt", "filepath": "fixture.txt"},
])
def test_single_or_equal_aliases_still_normalize_and_validate(tool, args):
    result = AgentOrchestrationService._normalize_tool_arguments(tool_name=tool, arguments=args)
    assert result["path"] == "fixture.txt"
    AgentOrchestrationService._validate_tool_arguments(tool_name=tool, arguments=result)


@pytest.mark.parametrize("tool", ["fs_read", "editor_open"])
def test_lifted_nested_path_cannot_override_conflicting_explicit_filepath(tool):
    args = {"file": {"path": "a.txt"}, "filepath": "b.txt"}
    with pytest.raises(ContractError, match="Conflicting file path aliases"):
        AgentOrchestrationService._normalize_tool_arguments(tool_name=tool, arguments=args)
    assert "path" not in args


@pytest.mark.parametrize("tool", ["fs_read", "editor_open"])
@pytest.mark.parametrize("args", [
    {"path": "a.txt", "file": {"path": "b.txt"}},
    {"file_path": "a.txt", "target_path": "b.txt"},
])
def test_conflicting_nested_or_noncanonical_aliases_reject(tool, args):
    for operation in (
        AgentOrchestrationService._normalize_tool_arguments,
        AgentOrchestrationService._validate_tool_arguments,
    ):
        with pytest.raises(ContractError, match="Conflicting file path aliases"):
            operation(tool_name=tool, arguments=args)
