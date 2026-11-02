"""Offline helper checks, not certification of model access or native UI."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import ROUND_UP, Decimal, localcontext

import pytest

from openvegas.integrations.billing_choices import MAX_CEILING_V, model_choices, wallet_ceiling
from openvegas.integrations.credentials import IntegrationError


@pytest.mark.parametrize(("wallet", "expected"), [
    (0, "0"), ("0.0000009", "0"), ("-0", "0"),
    ("0.000001", "0.000001"), ("12.3456789", "12.345678"),
    (Decimal("123.456789"), "123.456789"), (52, "52"), (12.5, "12.5"),
    ("99999.9999999", "99999.999999"), ("100000", "100000"),
    ("100000.000001", "100000"), ("1e100", "100000"),
])
def test_ceiling_uses_wallet_not_an_arbitrary_session_default(wallet, expected):
    ceiling = wallet_ceiling(wallet)
    assert isinstance(ceiling, Decimal)
    assert ceiling == Decimal(expected)
    assert Decimal(0) <= ceiling <= min(Decimal(str(wallet)), MAX_CEILING_V)
    assert ceiling.as_tuple().exponent == -6
    assert "e" not in format(ceiling, "f").lower()
    assert not ceiling.is_signed()


@pytest.mark.parametrize(("wallet", "maximum", "expected"), [
    ("80", "60", "60"), ("50", "60", "50"), ("50", "49.0000009", "49"),
    ("100001", "100001", "100000"), ("10", "0.0000009", "0"),
])
def test_ceiling_honors_lower_backend_maximum(wallet, maximum, expected):
    assert wallet_ceiling(wallet, maximum) == Decimal(expected)


@pytest.mark.parametrize("invalid", [
    None, True, False, [], {}, object(), "", "garbage", "-0.1", -1,
    "NaN", "sNaN", "Infinity", "-Infinity", float("nan"), float("inf"),
    Decimal("sNaN"), "1e101", "1e-101", "1" * 65,
])
def test_invalid_wallet_and_maximum_fail_closed(invalid):
    with pytest.raises(IntegrationError, match="Invalid wallet amount"):
        wallet_ceiling(invalid)
    with pytest.raises(IntegrationError, match="Invalid wallet amount"):
        wallet_ceiling("10", invalid)


@pytest.mark.parametrize("zero", [0, "0", Decimal(0)])
def test_backend_maximum_must_be_positive(zero):
    with pytest.raises(IntegrationError, match="Invalid backend spending maximum"):
        wallet_ceiling("10", zero)


def test_ceiling_is_independent_of_caller_decimal_context():
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        assert wallet_ceiling("123.4567899") == Decimal("123.456789")
        assert context.prec == 2 and context.rounding == ROUND_UP


def reviewed(model, host="codex", **changes):
    return {"model": model, "host": host,
            "provider": "openai" if host == "codex" else "anthropic",
            "max_tokens": 2048, **changes}


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_preserves_current_reviewed_model_not_first_option(host):
    rows = [reviewed("reviewed-first", host), reviewed("reviewed-current", host)]
    before = deepcopy(rows)
    result = model_choices(host, rows, "reviewed-current")
    assert result.options == ("reviewed-first", "reviewed-current")
    assert result.selected_model == "reviewed-current"
    assert result.unavailable_model is None
    assert rows == before


@pytest.mark.parametrize("models", [[], ["reviewed-only"], ["reviewed-first", "reviewed-second"]])
def test_unavailable_original_is_honest_and_never_silently_replaced(models):
    result = model_choices("codex", [reviewed(model) for model in models], "subscription-model")
    assert result.options == tuple(models)
    assert result.selected_model is None
    assert result.unavailable_model == "subscription-model"
    assert "subscription-model" not in result.options


def test_unknown_current_model_needs_explicit_selection():
    result = model_choices("claude", [reviewed("reviewed-only", "claude")])
    assert result.options == ("reviewed-only",)
    assert result.selected_model is result.unavailable_model is None


@pytest.mark.parametrize(("current", "alternative"), [
    ("gpt-example", "openai/gpt-example"),
    ("openai/gpt-example", "gpt-example"),
    ("gpt-example", "gpt-example-20261101"),
    ("gpt-example-high", "gpt-example"),
    ("gpt-example", "GPT-example"),
    ("sonnet", "claude-sonnet-example"),
    ("o3", "reviewed-other-model"),
    ("native:model@tag+variant", "reviewed-other-model"),
])
def test_does_not_guess_alias_prefix_snapshot_case_or_effort(current, alternative):
    result = model_choices("codex", [reviewed(alternative)], current)
    assert result.selected_model is None
    assert result.unavailable_model == current


def test_minimal_server_rows_are_supported_without_mutation():
    rows = ({"model": "reviewed-only", "max_tokens": 1},)
    assert model_choices("codex", rows, "reviewed-only").selected_model == "reviewed-only"
    assert rows == ({"model": "reviewed-only", "max_tokens": 1},)


@pytest.mark.parametrize("rows", [
    None, {}, "model", [None], ["model"], [{}],
    [reviewed("bad\x1b[31m")], [reviewed("bad\nmodel")], [reviewed("x" * 161)],
    [reviewed(3)], [reviewed("ok-model", max_tokens=True)],
    [reviewed("ok-model", max_tokens=0)], [reviewed("ok-model", max_tokens=128001)],
    [reviewed("ok-model", max_tokens=2.0)], [reviewed("ok-model", max_tokens="2048")],
    [reviewed("ok-model", host="claude")], [reviewed("ok-model", provider="openrouter")],
    [reviewed("ok-model"), reviewed("ok-model", max_tokens=128)],
    [reviewed(f"model-{i}") for i in range(129)],
])
def test_invalid_catalog_fails_closed_not_partial_fallback(rows):
    with pytest.raises(IntegrationError, match="Invalid native model options"):
        model_choices("codex", rows)


@pytest.mark.parametrize("current", [False, 2, [], "", "unsafe\nmodel", "x" * 201])
def test_invalid_current_identity_is_not_rendered(current):
    with pytest.raises(IntegrationError, match="Invalid native model options"):
        model_choices("codex", [reviewed("ok-model")], current)


@pytest.mark.parametrize("host", ["other", None, []])
def test_unsupported_host_fails_closed(host):
    with pytest.raises(IntegrationError, match="Unsupported native host"):
        model_choices(host, [])


def test_choices_are_immutable_and_do_not_advertise_effort_support():
    result = model_choices("codex", [reviewed("ok-model", reasoning_efforts=["high"])])
    assert not hasattr(result, "reasoning_efforts")
    with pytest.raises(FrozenInstanceError):
        result.selected_model = "unreviewed-model"
