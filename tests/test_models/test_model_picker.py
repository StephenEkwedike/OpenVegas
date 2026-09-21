from __future__ import annotations  # noqa: I001 - Partial staging trees change Ruff's first-party import discovery.

from unittest.mock import AsyncMock

import pytest
from openvegas.gateway.providers import model_switch_enabled
from openvegas.tui.model_picker import (
    ModelSelectionError,
    format_options,
    model_options,
    plan_switch,
    select_model,
    validate_selection,
)


def model(provider="openai", **extra):
    return {
        "provider": provider,
        "model_id": "catalog-model",
        "enabled": True,
        "available": True,
        "capabilities": {"reviewed": False},
        **extra,
    }


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
@pytest.mark.asyncio
async def test_ordinary_legacy_selection_needs_no_new_manual_review(provider):
    target = model(provider)
    client = AsyncMock()
    client.list_models.return_value = {"models": [target]}
    client._request.return_value = {
        "model": target,
        "selection_valid": True,
        "state_changed": False,
    }
    validated = await validate_selection(client, provider, "catalog-model")
    client._request.assert_awaited_once_with(
        "POST", "/models/validate", json={"provider": provider, "model": "catalog-model"}
    )
    plan = plan_switch(
        validated, current_provider=provider, current_model="old", thread_id="existing"
    )
    assert plan.status == "ready" and plan.thread_id == "existing"
    assert not plan.reset_context


@pytest.mark.parametrize("extra", [{"available": False}, {"enabled": False}, {"selectable": False}])
@pytest.mark.asyncio
async def test_unavailable_models_cannot_bypass_server_catalog(extra):
    client = AsyncMock()
    client.list_models.return_value = {"models": [model("mistral", **extra)]}
    with pytest.raises(ModelSelectionError):
        await validate_selection(client, "mistral", "catalog-model")
    client._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_catalog_missing_availability_is_not_silently_accepted():
    old = model()
    old.pop("available")
    client = AsyncMock()
    client.list_models.return_value = {"models": [old]}
    with pytest.raises(ModelSelectionError):
        await validate_selection(client, "openai", "catalog-model")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"selection_valid": False},
        {"selection_valid": True, "state_changed": False, "model": model("other")},
        {"selection_valid": True, "state_changed": True, "model": model()},
    ],
)
async def test_failed_or_mismatched_preflight_never_returns_a_selection(response):
    client = AsyncMock()
    client.list_models.return_value = {"models": [model()]}
    client._request.return_value = response
    with pytest.raises(ModelSelectionError):
        await validate_selection(client, "openai", "catalog-model")


def test_list_search_and_display_do_not_execute_terminal_controls():
    payload = {
        "models": [
            model("gemini"),
            model("mistral", available=False, unavailable_reason="missing\x1b[31mkey"),
            model("openai"),
        ]
    }
    assert [r["provider"] for r in model_options(payload)] == ["gemini", "mistral", "openai"]
    assert len(model_options(payload, search="MISTRAL")) == 1
    assert "\x1b" not in format_options(model_options(payload))
    assert "unavailable" in format_options(model_options(payload))
    with pytest.raises(ModelSelectionError):
        select_model(payload, "openai", "invented-model")
    with pytest.raises(ModelSelectionError):
        select_model({"models": [model(), model()]}, "openai", "catalog-model")


@pytest.mark.parametrize("guard", ["turn_active", "tools_pending", "pending_attachments"])
def test_switch_waits_for_safe_boundary(guard):
    plan = plan_switch(
        model(),
        current_provider="openai",
        current_model="old",
        thread_id="existing",
        **{guard: True},
    )
    assert plan.status == "blocked" and plan.model == "old" and plan.thread_id == "existing"


def test_cross_provider_requires_explicit_fresh_context_and_never_replays():
    kwargs = {"current_provider": "openai", "current_model": "old", "thread_id": "existing"}
    plan = plan_switch(model("mistral"), **kwargs)
    assert plan.status == "confirm_fresh" and plan.thread_id == "existing"
    confirmed = plan_switch(model("mistral"), **kwargs, confirm_fresh=True)
    assert confirmed.status == "ready" and confirmed.thread_id is None
    assert confirmed.reset_context
    assert not hasattr(confirmed, "messages")


def test_enabled_default_and_explicit_server_rollback(monkeypatch):
    monkeypatch.delenv("OPENVEGAS_MODEL_SWITCH_ENABLED", raising=False)
    assert model_switch_enabled()
    monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "0")
    assert not model_switch_enabled()
    with pytest.raises(ModelSelectionError):
        select_model({"models": [model()], "switching_enabled": False}, "openai", "catalog-model")


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["same", "fresh", "cancel", "unavailable", "background"])
async def test_actual_cli_switch_branch(scenario):
    import ast
    import asyncio
    from pathlib import Path
    from types import SimpleNamespace

    source = (Path(__file__).parents[2] / "openvegas/cli.py").read_text()
    branch = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name) and node.test.left.id == "cmd"
        and isinstance(node.test.comparators[0], ast.Set)
        and {value.value for value in node.test.comparators[0].elts} == {"/models", "/provider", "/model"}
    )
    block = ast.unparse(branch)
    wrapper = "async def run(client, Confirm, jobs):\n"
    wrapper += (
        "    current_provider, current_model, current_thread_id = 'openai', 'old', 'thread'\n"
    )
    wrapper += "    allow_model_switch, startup_bootstrap_task = True, None\n"
    wrapper += "    pending_attachments, chat_transcript = [], [{'role': 'user'}]\n"
    wrapper += "    model_switch_local_tools = SimpleNamespace(_BACKGROUND_JOBS=jobs)\n"
    wrapper += "    for cmd, parts in commands:\n"
    wrapper += "\n".join("        " + line for line in block.splitlines())
    wrapper += "\n    return current_provider, current_model, current_thread_id\n"
    command = ("/model", ["/model", "catalog-model"])
    target = model()
    if scenario in {"fresh", "cancel"}:
        command = ("/provider", ["/provider", "mistral", "catalog-model"])
        target = model("mistral")
    if scenario == "unavailable":
        target = model(available=False)
    client = AsyncMock()
    client.list_models.return_value = {"models": [target]}
    client._request.return_value = {
        "model": target,
        "selection_valid": True,
        "state_changed": False,
    }
    namespace = {
        "commands": [command],
        "asyncio": asyncio,
        "SimpleNamespace": SimpleNamespace,
        "console": SimpleNamespace(print=lambda *a, **k: None),
        "APIError": RuntimeError,
        "ModelSelectionError": ModelSelectionError,
        "model_options": model_options,
        "format_options": format_options,
        "plan_switch": plan_switch,
        "validate_selection": validate_selection,
        "_env_flag": lambda name, default: default == "1",
        "_chat_modal": AsyncMock(side_effect=lambda callback: callback()),
    }
    exec(compile(wrapper, "documented_cli_branch", "exec"), namespace)  # noqa: S102 - Trusted repository AST only; exercises the actual CLI branch.
    confirm = SimpleNamespace(ask=lambda *a, **k: scenario != "cancel")
    jobs = (
        {"running": SimpleNamespace(process=SimpleNamespace(returncode=None))}
        if scenario == "background"
        else {}
    )
    result = await namespace["run"](client, confirm, jobs)
    if scenario == "same":
        assert result == ("openai", "catalog-model", "thread")
    elif scenario == "fresh":
        assert result == ("mistral", "catalog-model", None)
        assert client._request.await_count == 2
    else:
        assert result == ("openai", "old", "thread")
