"""Customer selection never installs or requests an OpenRouter credential."""

import pytest
from click.testing import CliRunner

from openvegas import cli as commands
from openvegas import config


@pytest.mark.parametrize("provider", ["openrouter", "mistral"])
def test_no_invented_default_model(monkeypatch, provider):
    monkeypatch.setattr(config, "load_config", dict)
    assert config.get_default_model(provider) == ""
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {
            "default_model_by_provider": {provider: "vendor/reviewed-model"},
        },
    )
    assert config.get_default_model(provider) == "vendor/reviewed-model"


@pytest.mark.parametrize("command", [["chat"], ["ask", "hello"]])
def test_missing_model_fails_before_auth_or_provider(monkeypatch, command):
    monkeypatch.setattr(commands, "_load_openvegas_env_defaults_from_dotenv", lambda: None)
    monkeypatch.setattr(config, "load_config", dict)
    monkeypatch.setattr(commands, "run_async", lambda *_: pytest.fail("No request expected"))
    result = CliRunner().invoke(commands.cli, [*command, "--provider", "openrouter"])
    assert result.exit_code == 1
    assert "openvegas models --provider openrouter" in result.output
    assert "--model" in result.output
    assert "API_KEY" not in result.output
