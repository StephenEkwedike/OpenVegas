"""Source policy blocks before upload and never rewrites private observations."""
from pathlib import Path

import pytest

from openvegas.agent import native_mutation_client as bridge
from openvegas.agent.native_mutation import NativeMutationError, validate_relative_path
from openvegas.agent.runtime_write import capture_source
from openvegas.contracts.errors import ContractError
from tests.test_agent.test_native_mutation_client import Client, prepare
from tests.test_agent.test_native_mutation_client import (
    scope as scope,  # noqa: PLC0414 - pytest fixture export
)


@pytest.mark.parametrize("path", [".env", ".env.production", "sub/.ENV.local", ".git/config",
                                 ".ssh/id_rsa", ".aws/credentials", ".openvegas/config.json", ".netrc",
                                 "env.md", "test-accounts.md", ".npmrc", ".pypirc"])
def test_sensitive_path_denied_before_disk_or_request(scope, monkeypatch, path):
    monkeypatch.setenv("OPENVEGAS_CHAT_NATIVE_MUTATIONS", "1")
    scope["call"]["arguments"]["filepath"] = path
    reads = []
    monkeypatch.setattr(bridge, "capture_source", lambda *_: reads.append(True))
    client = Client(scope["call"])
    with pytest.raises(ContractError):
        prepare(scope, client)
    assert not reads and not client.calls
    with pytest.raises(NativeMutationError):
        capture_source(scope["workspace_root"], path)


@pytest.mark.parametrize("name", [".env.example", ".env.sample", ".env.template", "src/config.py"])
def test_plain_templates_remain_lexically_supported(name):
    assert validate_relative_path(name) == name


@pytest.mark.parametrize("where", ["source", "target"])
def test_configured_sensitive_content_blocks_before_upload(scope, monkeypatch, where):
    monkeypatch.setenv("OPENVEGAS_CHAT_NATIVE_MUTATIONS", "1")
    monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", "fixture_private_marker")
    path = Path(scope["workspace_root"]) / "a.txt"
    if where == "source":
        path.write_text("fixture_private_marker")
    else:
        scope["call"]["arguments"]["content"] = "fixture_private_marker"
    before = path.read_bytes()
    client = Client(scope["call"])
    with pytest.raises(ContractError) as error:
        prepare(scope, client)
    assert "fixture_private_marker" not in str(error.value)
    assert not client.calls and path.read_bytes() == before
