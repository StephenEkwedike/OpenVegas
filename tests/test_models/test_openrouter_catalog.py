"""Offline fixtures only; no managed account, API key, paid completion or DB."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import httpx
import pytest

from openvegas.gateway import openrouter_catalog as c

NOW = datetime(2026, 9, 21, 12, tzinfo=UTC)
OBSERVED = (NOW - timedelta(hours=1)).isoformat()


def model(**changes):
    return {
        "id": "vendor/model-v2",
        "canonical_slug": "vendor/model-v2-20260901",
        "name": "Synthetic Reviewed Model",
        "context_length": 128000,
        "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
        "supported_parameters": ["max_tokens", "tools", "tool_choice", "temperature"],
        "top_provider": {"context_length": 64000, "max_completion_tokens": 8192},
        "pricing": {"prompt": "0.0000025", "completion": "0.00001", "request": "0", "image": "0"},
        "expiration_date": None,
        **changes,
    }


def payload(value=None):
    return json.dumps({"data": [model() if value is None else value]}).encode()


def plan(data=None, now=NOW):
    data = payload() if data is None else data
    return {
        "schema_version": 1,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "models": [
            {
                "model_id": "vendor/model-v2",
                "account_access": True,
                "pricing_scope_acknowledged": True,
                "completion_chat": True,
                "context_window_tokens": 32000,
                "max_tokens": 2048,
                "cost_input_per_1m": "2.5",
                "cost_output_per_1m": "10",
                "v_price_input_per_1m": "321.00",
                "v_price_output_per_1m": "876.50",
                "capabilities": {"tools": True, "image_input": False, "web_search": False},
                "reviewed_at": now.isoformat(),
                "expires_at": (now + timedelta(days=1)).isoformat(),
            }
        ],
    }


def build(data=None, review=None, **kwargs):
    data = payload() if data is None else data
    review = plan(data) if review is None else review
    options = {
        "now": NOW,
        "observed_at": OBSERVED,
        "ack_account_access": True,
        "ack_retail_prices": True,
    }
    return c.reviewed_bundle(data, review, **(options | kwargs))


def test_candidate_facts_do_not_grant_access_retail_rates_or_capability_review():
    result = c.candidate_report(payload(), observed_at=OBSERVED, now=NOW)
    assert result["account_access_inferred"] is False
    row = result["models"][0]
    assert row["account_access"] is None
    assert row["availability"] == "public_listing_not_account_access"
    assert row["eligible_for_review"] is True
    assert row["cost_input_per_1m"] == "2.5000000"
    assert row["cost_output_per_1m"] == "10.00000"
    assert row["context_window_tokens"] == 64000
    assert row["max_tokens"] == 8192
    assert "v_price_input_per_1m" not in row
    assert row["advertised_capabilities"] == {"tools": True, "image_input": True}
    assert "audio" in row["missing_optional_fee_fields"]


def test_reviewed_bundle_is_disabled_exact_price_matched_and_explicit_retail():
    result = build()
    row = result["provider_catalog"][0]
    review = result["model_reviews"]["openrouter:vendor/model-v2"]
    assert result["installed"] is False and result["live_verified"] is False
    assert row["enabled"] is False and row["provider"] == "openrouter"
    for field in (
        "cost_input_per_1m",
        "cost_output_per_1m",
        "v_price_input_per_1m",
        "v_price_output_per_1m",
        "max_tokens",
    ):
        assert row[field] == review[field]
    assert review["v_price_input_per_1m"] == "321.00"
    assert review["account_access_source"] == "operator_attestation"
    assert review["pricing_scope_acknowledged"] is True
    assert review["response_model_ids"] == [row["model_id"]]
    assert review["canonical_slug"] == "vendor/model-v2-20260901"
    assert review["capabilities"] == {"tools": True, "image_input": False, "web_search": False}
    assert review["pricing_policy"] == "text_tokens_only_zero_request_cap_no_plugins"


def test_generated_review_integrates_with_current_gateway_catalog(monkeypatch):
    from openvegas.contracts.errors import ContractError
    from openvegas.gateway.catalog import ModelDisabled, validate_catalog_entry

    now = datetime.now(UTC)
    review = plan(now=now)
    result = build(review=review, now=now, observed_at=(now - timedelta(minutes=1)).isoformat())
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps(result["model_reviews"]))
    row = result["provider_catalog"][0]
    with pytest.raises(ModelDisabled):
        validate_catalog_entry("openrouter", row["model_id"], row)
    # Test fixture only: installing/enabling production is not an importer side effect.
    row["enabled"] = True
    validate_catalog_entry("openrouter", row["model_id"], row)
    row["cost_output_per_1m"] = "10.1"
    with pytest.raises(ContractError):
        validate_catalog_entry("openrouter", row["model_id"], row)


@pytest.mark.parametrize(
    "model_id",
    [
        "openrouter/auto",
        "openrouter/free",
        "vendor/model-latest",
        "vendor/latest-v2",
        "vendor/auto",
        "vendor/model:online",
        "vendor/model:free",
        "vendor/model:nitro",
        "vendor/*",
        "vendor/~model",
        "~vendor/model",
        "vendor/model/extra",
        " vendor/model",
        "vendor/model\n",
        "https://vendor/model",
        "vendor/../model",
        "vendor/model?x=1",
        None,
    ],
)
def test_exact_only_no_aliases_variants_routers_or_wildcards(model_id):
    with pytest.raises(c.ReviewError):
        c.exact_model_id(model_id)
    result = c.candidate_report(payload(model(id=model_id)), now=NOW)
    assert not result["models"] and len(result["rejected_entries"]) == 1


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0", "0"),
        ("0.0000025", "2.5000000"),
        ("1e-6", "1.000000"),
        ("0.0000000125", "0.0125000000"),
    ],
)
def test_decimal_conversion_exact_and_independent_of_ambient_precision(value, expected):
    with localcontext() as context:
        context.prec = 2
        assert c.usd_per_million(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        0,
        0.000001,
        Decimal("0.000001"),
        "",
        "NaN",
        "Infinity",
        "-1",
        "+1",
        " 0 ",
        "1_000",
        "1e999999",
        "0.",
        "10",
    ],
)
def test_reject_invalid_or_unbounded_token_rates(value):
    with pytest.raises(c.ReviewError):
        c.usd_per_million(value)


def test_subprecision_usd_not_rounded_silently_to_free():
    value = model(pricing={"prompt": "0.000000000001", "completion": "0.00001"})
    report = c.candidate_report(payload(value), now=NOW)
    row = report["models"][0]
    assert Decimal(row["cost_input_per_1m"]) > 0
    assert row["eligible_for_review"] is False
    assert any("unrepresentable" in reason for reason in row["rejection_reasons"])


@pytest.mark.parametrize("key", sorted(c.UNIT_FEES - c.SCOPED_OUT_FEES))
def test_every_non_token_fee_is_surfaced_and_blocks_review(key):
    value = model()
    value["pricing"][key] = "0.001"
    data = payload(value)
    candidate = c.candidate_report(data, now=NOW)["models"][0]
    assert candidate["pricing_observed"][key] == "0.001"
    assert candidate["eligible_for_review"] is False
    assert any(key in reason for reason in candidate["rejection_reasons"])
    with pytest.raises(c.ReviewError, match="ineligible"):
        build(data)


@pytest.mark.parametrize(
    "extra",
    [
        {"unknown_charge": "0"},
        {"unknown_charge": "0.001"},
        {"request": None},
        {"image": "-1"},
        {"audio": "NaN"},
        {"overrides": [{"min_prompt_tokens": 200000, "prompt": "0.1"}]},
        {"overrides": [{"utc_start": 1200, "prompt": "0.1"}]},
        {"overrides": None},
        {"input_cache_read": "0.0001"},
    ],
)
def test_unknown_invalid_conditional_or_excess_cache_prices_never_ignored(extra):
    value = model()
    value["pricing"].update(extra)
    row = c.candidate_report(payload(value), now=NOW)["models"][0]
    assert row["eligible_for_review"] is False


def test_cache_discounts_and_zero_units_are_observed_not_added_to_base():
    value = model()
    value["pricing"].update(
        input_cache_read="0.000001", input_cache_write="0.0000025", audio="0", overrides=[]
    )
    row = c.candidate_report(payload(value), now=NOW)["models"][0]
    assert row["eligible_for_review"] is True
    assert row["pricing_observed"]["input_cache_read"] == "0.000001"
    assert Decimal(row["cost_input_per_1m"]) == Decimal("2.5")


def test_missing_optional_fees_stay_unknown_with_zero_request_cap_policy():
    value = model(pricing={"prompt": "0.0000025", "completion": "0.00001"})
    report = c.candidate_report(payload(value), now=NOW)
    row = report["models"][0]
    assert "request" in row["missing_optional_fee_fields"]
    assert "request" not in row["pricing_observed"]
    assert any("unknown" in warning for warning in row["warnings"])
    assert build(payload(value))["model_reviews"]["openrouter:vendor/model-v2"][
        "pricing_policy"
    ].endswith("zero_request_cap_no_plugins")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("pricing"),
        lambda value: value["pricing"].pop("prompt"),
        lambda value: value["pricing"].pop("completion"),
        lambda value: value.update(context_length=True),
        lambda value: value["top_provider"].update(context_length=None),
        lambda value: value["top_provider"].update(max_completion_tokens=None),
        lambda value: value["top_provider"].update(max_completion_tokens=0),
        lambda value: value.update(architecture=None),
        lambda value: value["architecture"].update(output_modalities=["image"]),
        lambda value: value["architecture"].update(input_modalities=["audio"]),
        lambda value: value.update(supported_parameters=["temperature"]),
        lambda value: value.update(expiration_date="2026-01-01"),
        lambda value: value.update(expiration_date="not-a-date"),
    ],
)
def test_incomplete_model_metadata_cannot_become_reviewed_by_guessing(mutate):
    value = model()
    mutate(value)
    row = c.candidate_report(payload(value), now=NOW)["models"][0]
    assert row["eligible_for_review"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"account_access": False},
        {"pricing_scope_acknowledged": False},
        {"pricing_scope_acknowledged": 1},
        {"pricing_scope_acknowledged": None},
        {"account_access": 1},
        {"completion_chat": False},
        {"max_tokens": True},
        {"max_tokens": 10000},
        {"context_window_tokens": 128000},
        {"context_window_tokens": 1000, "max_tokens": 2048},
        {"cost_input_per_1m": "2.51"},
        {"cost_output_per_1m": "9.99"},
        {"v_price_input_per_1m": None},
        {"v_price_output_per_1m": "0.001"},
        {"v_price_output_per_1m": "100000000"},
        {"capabilities": {"tools": True, "image_input": True, "web_search": False}},
        {"capabilities": {"tools": True, "image_input": False, "web_search": True}},
        {"capabilities": {"tools": 1, "image_input": False, "web_search": False}},
        {"extra": "ignored?"},
        {"model_id": "vendor/unknown"},
        {"reviewed_at": "2026-09-21T12:01:00Z"},
        {"reviewed_at": "2026-09-21T10:00:00Z"},
        {"reviewed_at": "2026-09-21T12:00:00"},
        {"expires_at": "2026-09-21T12:00:00Z"},
        {"expires_at": "2026-12-21T12:00:00Z"},
    ],
)
def test_operator_must_explicitly_review_exact_current_account_prices_and_bounds(change):
    review = plan()
    review["models"][0].update(change)
    with pytest.raises(c.ReviewError):
        build(review=review)


def test_no_missing_fields_duplicate_rows_hash_drift_or_acknowledgement_bypass():
    for change in (
        lambda p: p["models"][0].pop("cost_input_per_1m"),
        lambda p: p["models"].append(copy.deepcopy(p["models"][0])),
        lambda p: p.update(source_sha256="0" * 64),
        lambda p: p.update(schema_version=True),
    ):
        review = plan()
        change(review)
        with pytest.raises(c.ReviewError):
            build(review=review)
    for option in ("ack_account_access", "ack_retail_prices"):
        with pytest.raises(c.ReviewError, match="acknowledgements"):
            build(**{option: False})
    with pytest.raises(c.ReviewError, match="stale"):
        build(observed_at=(NOW - timedelta(hours=25)).isoformat())
    with pytest.raises(c.ReviewError, match="future"):
        build(observed_at=(NOW + timedelta(seconds=1)).isoformat())


def test_tools_need_both_tools_and_tool_choice_metadata():
    value = model(supported_parameters=["max_tokens", "tools"])
    with pytest.raises(c.ReviewError, match="tool_choice"):
        build(payload(value))
    review = plan(payload(value))
    review["models"][0]["capabilities"]["tools"] = False
    assert build(payload(value), review)["provider_catalog"]


def test_canonical_response_alias_requires_explicit_exact_metadata_match():
    review = plan()
    review["models"][0]["response_model_ids"] = ["vendor/model-v2", "vendor/model-v2-20260901"]
    result = build(review=review)
    assert (
        result["model_reviews"]["openrouter:vendor/model-v2"]["response_model_ids"]
        == review["models"][0]["response_model_ids"]
    )
    for values in (
        ["vendor/model-v2", "vendor/other"],
        ["vendor/model-v2:online"],
        [],
        ["vendor/model-v2"] * 2,
        ["vendor/model-v2", {}],
    ):
        review["models"][0]["response_model_ids"] = values
        with pytest.raises(c.ReviewError):
            build(review=review)


def test_review_cannot_outlive_model_deprecation():
    data = payload(model(expiration_date="2026-09-22"))
    with pytest.raises(c.ReviewError, match="expiration"):
        build(data)


def test_template_requires_operator_access_retail_and_scope_decisions():
    report = c.candidate_report(payload(), now=NOW)
    template = c.review_template(report, ["vendor/model-v2"])
    row = template["models"][0]
    assert row["account_access"] is False
    assert row["pricing_scope_acknowledged"] is False
    assert row["completion_chat"] is False
    assert row["v_price_input_per_1m"] is None
    assert row["context_window_tokens"] is None
    assert row["reviewed_at"] is None
    with pytest.raises(c.ReviewError):
        build(review=template)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"[]",
        b"{",
        b'{"data":[],"data":[]}',
        b'{"data":NaN}',
        b'{"data":[]}',
        b'{"data":[[[[]]]]}',
    ],
)
def test_malformed_catalog_is_rejected_or_nonobject_entries_are_surfaced(data):
    if data == b'{"data":[[[[]]]]}':
        assert c.candidate_report(data)["rejected_entries"]
    else:
        with pytest.raises(c.ReviewError):
            c.candidate_report(data)


def test_bounded_input_duplicates_and_display_control_sanitization():
    with pytest.raises(c.ReviewError):
        c.candidate_report(b" " * (c.MAX_BYTES + 1))
    with pytest.raises(c.ReviewError, match="Duplicate"):
        c.candidate_report(json.dumps({"data": [model(), model()]}).encode())
    result = c.candidate_report(payload(model(name="bad\x1b[2Jname")), now=NOW)
    assert result["models"][0]["display_name"] == "vendor/model-v2"


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.asyncio
async def test_public_fetch_is_one_fixed_unauthenticated_get(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "MUST_NOT_BE_READ_OR_SENT")
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid")
    calls = []

    async def handler(request):
        calls.append(request)
        assert request.method == "GET" and str(request.url) == c.MODELS_URL
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=Body([payload()])
        )

    assert await c.fetch_public_models(transport=httpx.MockTransport(handler)) == payload()
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,headers,body",
    [
        (302, {"location": "http://other.invalid"}, b""),
        (401, {}, b"secret body"),
        (500, {}, b"secret body"),
        (200, {"content-type": "text/html"}, b"<html>secret</html>"),
        (200, {"content-type": "application/json", "content-encoding": "gzip"}, b"bad"),
        (200, {"content-type": "application/json", "content-length": str(c.MAX_BYTES + 1)}, b""),
        (200, {"content-type": "application/json", "content-length": "invalid"}, b""),
        (200, {"content-type": "application/json"}, b"NaN"),
    ],
)
async def test_fetch_redirect_errors_encoding_lengths_fail_closed_no_retry(status, headers, body):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(status, headers=headers, stream=Body([body]))

    with pytest.raises(c.ReviewError) as failure:
        await c.fetch_public_models(transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    assert "secret body" not in str(failure.value)


@pytest.mark.asyncio
async def test_chunked_body_and_timeout_are_bounded(monkeypatch):
    monkeypatch.setattr(c, "MAX_BYTES", 16)

    async def oversized(request):
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=Body([b" " * 10] * 2)
        )

    with pytest.raises(c.ReviewError, match="size limit"):
        await c.fetch_public_models(transport=httpx.MockTransport(oversized))

    async def timed_out(request):
        raise httpx.ReadTimeout("do not echo sensitive configuration")

    with pytest.raises(c.ReviewError, match="timed out") as failure:
        await c.fetch_public_models(transport=httpx.MockTransport(timed_out))
    assert "sensitive" not in str(failure.value)


def test_cli_offline_default_no_credentials_config_or_network(tmp_path):
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "models.json"
    source.write_bytes(payload())
    output = tmp_path / "candidates.json"
    home = tmp_path / "home"
    home.mkdir()
    bootstrap = r"""
import os, runpy, socket, sys
def no_network(*args, **kwargs): raise RuntimeError("offline command attempted network")
socket.socket.connect = no_network
socket.getaddrinfo = no_network
socket.create_connection = no_network
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        text = os.fsdecode(args[0])
        if os.path.basename(text).startswith(".env") or "/.openvegas/" in text:
            raise RuntimeError("config access attempted")
    if event == "import" and args[0] in {"dotenv", "openvegas.config", "openvegas.cli", "asyncpg", "stripe"}:
        raise RuntimeError("application bootstrap attempted")
sys.addaudithook(audit)
root = sys.argv.pop(1)
sys.path.insert(0, root)
runpy.run_path(root + "/scripts/review_openrouter_models.py", run_name="__main__")
"""
    env = {
        "HOME": str(home),
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    base = [sys.executable, "-B", "-c", bootstrap, str(root), "--input", str(source)]
    result = subprocess.run(
        [*base, "--output", str(output)],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_bytes())["account_access_inferred"] is False
    assert result.stdout == ""
    result = subprocess.run(
        [*base, "--template", "--model", "vendor/model-v2"],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["models"][0]["account_access"] is False
    result = subprocess.run(
        [*base, "--output", str(output)],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 2
    assert "no overwrite" in result.stderr


def test_cli_review_generates_only_disabled_bundle_and_does_not_install(tmp_path, capsys):
    from scripts import review_openrouter_models as cli

    now = datetime.now(UTC)
    source, review = tmp_path / "models.json", tmp_path / "review.json"
    source.write_bytes(payload())
    review.write_text(json.dumps(plan(now=now)))
    result = cli.main(
        [
            "--input",
            str(source),
            "--observed-at",
            (now - timedelta(minutes=1)).isoformat(),
            "--review",
            str(review),
            "--ack-account-access",
            "--ack-retail-prices",
        ]
    )
    assert result == 0
    value = json.loads(capsys.readouterr().out)
    assert value["provider_catalog"][0]["enabled"] is False
    assert value["installed"] is False


@pytest.mark.skipif(os.name != "posix", reason="Operator CLI file writes require POSIX")
def test_cli_no_symlink_reads_or_writes_and_no_overwrites(tmp_path):
    from scripts import review_openrouter_models as cli

    source = tmp_path / "source.json"
    source.write_bytes(payload())
    linked = tmp_path / "linked.json"
    linked.symlink_to(source)
    for operation in (lambda: cli._read(str(linked)), lambda: cli._write(str(linked), b"{}")):
        with pytest.raises(OSError):
            operation()
    parent = tmp_path / "parent-link"
    parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        cli._write(str(parent / "output.json"), b"{}")
    assert source.read_bytes() == payload()


@pytest.mark.parametrize("key", sorted(c.SCOPED_OUT_FEES | c.CACHE_WRITE_FEES))
def test_unrequested_modality_plugin_and_opt_in_cache_fees_are_scoped_not_free(key):
    value = model()
    value["pricing"][key] = "0.01"
    data = payload(value)
    row = c.candidate_report(data, now=NOW)["models"][0]
    assert row["eligible_for_review"] is True
    assert row["pricing_observed"][key] == "0.01"
    assert key in row["pricing_scope"]["scoped_out_nonzero_fees"]
    assert row["pricing_scope"]["explicit_prompt_cache_write"] is False
    review = build(data)["model_reviews"]["openrouter:vendor/model-v2"]
    assert review["pricing_scope"] == row["pricing_scope"]
    assert review["observed_pricing"][key] == "0.01"


@pytest.mark.parametrize("price", ["0.000001", "0.00001"])
def test_reasoning_rate_within_completion_cap_uses_output_usage_not_a_free_feature(price):
    value = model()
    value["pricing"]["internal_reasoning"] = price
    row = c.candidate_report(payload(value), now=NOW)["models"][0]
    assert row["eligible_for_review"] is True
    assert row["pricing_observed"]["internal_reasoning"] == price
    assert row["pricing_scope"]["reasoning_in_output_token_budget"] is True
    assert Decimal(row["cost_output_per_1m"]) == Decimal(10)


@pytest.mark.parametrize(
    "canonical", [None, "vendor/model:online", "vendor/*", "openrouter/auto", "vendor/model-latest"]
)
def test_unreviewable_canonical_slug_is_not_carried_into_runtime_review(canonical):
    result = build(payload(model(canonical_slug=canonical)))
    review = result["model_reviews"]["openrouter:vendor/model-v2"]
    assert "canonical_slug" not in review
    assert review["response_model_ids"] == ["vendor/model-v2"]
