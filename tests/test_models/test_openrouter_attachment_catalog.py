"""Generating an attachment review never activates a model or invents limits."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
import test_openrouter_attachments as media
import test_openrouter_catalog as catalog

from openvegas.gateway.openrouter_catalog import ReviewError, verify_attachment_endpoints


def plan():
    plan = catalog.plan()
    entry = plan["models"][0]
    entry["attachments"] = copy.deepcopy(media.review()["attachments"])
    entry["attachments"].update(
        model_id=entry["model_id"],
        input_modalities=["text", "image"],
        pdf_page_tokens=0,
        pdf_file_overhead_tokens=0,
    )
    entry["capabilities"]["image_input"] = True
    return plan


def test_review_keeps_disabled_status_and_real_attachment_policy():
    bundle = catalog.build(review=plan())
    assert bundle["provider_catalog"][0]["enabled"] is False
    value = bundle["model_reviews"]["openrouter:vendor/model-v2"]
    assert value["attachments"]["input_modalities"] == ["text", "image"]
    assert value["capabilities"]["image_input"]
    assert value["pricing_scope"]["input"] == "owned_reviewed_attachments"


@pytest.mark.parametrize("case", ["endpoint", "bounds", "fee", "unadvertised", "mismatch"])
def test_invalid_media_review_rejected_without_bundle(case):
    value = plan()
    entry = value["models"][0]
    if case == "endpoint":
        entry["attachments"]["provider"] = ""
    elif case == "bounds":
        entry["attachments"]["image_tokens"] = 0
    elif case == "fee":
        entry["attachments"]["non_token_fees"]["image"] = "0.01"
    elif case == "unadvertised":
        entry["attachments"]["input_modalities"].append("file")
        entry["attachments"].update(pdf_page_tokens=10000, pdf_file_overhead_tokens=1000)
    else:
        entry["capabilities"]["image_input"] = False
    with pytest.raises(ReviewError):
        catalog.build(review=value)


def endpoint_fixture():
    bundle = catalog.build(review=plan())
    review = bundle["model_reviews"]["openrouter:vendor/model-v2"]
    endpoint = {
        "model_id": "vendor/model-v2", "tag": review["attachments"]["provider"],
        "status": 0, "supported_parameters": ["max_tokens", "tools", "tool_choice"],
        "context_length": review["context_window_tokens"],
        "max_completion_tokens": review["max_tokens"], "pricing": review["observed_pricing"],
    }
    return bundle, {"data": {"id": endpoint["model_id"], "endpoints": [endpoint]}}, {"data": [copy.deepcopy(endpoint)]}


def verify(bundle, endpoints, zdr):
    return verify_attachment_endpoints(bundle, [json.dumps(endpoints).encode()], json.dumps(zdr).encode())


def test_pinned_endpoint_must_match_privacy_prices_and_parameters():
    bundle, endpoints, zdr = endpoint_fixture()
    verified = verify(bundle, endpoints, zdr)
    assert verified["vendor/model-v2"]["output_token_parameter"] == "max_tokens"
    assert bundle["provider_catalog"][0]["enabled"] is False
    assert "endpoint_verification" not in bundle


def test_completion_budget_alias_requires_exact_endpoint_support():
    bundle, endpoints, zdr = endpoint_fixture()
    review = bundle["model_reviews"]["openrouter:vendor/model-v2"]
    review["attachments"]["output_token_parameter"] = "max_completion_tokens"
    with pytest.raises(ReviewError, match="parameters"):
        verify(bundle, endpoints, zdr)
    for e in (endpoints["data"]["endpoints"][0], zdr["data"][0]):
        e["supported_parameters"][0] = "max_completion_tokens"
    assert verify(bundle, endpoints, zdr)["vendor/model-v2"]["output_token_parameter"] == "max_completion_tokens"


@pytest.mark.parametrize("change", ["zdr", "pin", "duplicate", "parameters", "context", "prompt", "output", "price", "image_fee", "request_fee", "cache_write", "reasoning", "overrides", "unknown_fee", "status"])
def test_ineligible_pin_is_rejected_before_any_paid_request(change):
    bundle, endpoints, zdr = endpoint_fixture()
    e = zdr["data"][0]
    if change == "zdr": e["tag"] = "other-private-endpoint"
    elif change == "pin": endpoints["data"]["endpoints"][0]["tag"] = "other-endpoint"
    elif change == "duplicate": zdr["data"].append(copy.deepcopy(e))
    elif change == "parameters": e["supported_parameters"].remove("max_tokens")
    elif change == "context": e["context_length"] = 1
    elif change == "prompt": e["max_prompt_tokens"] = 1
    elif change == "output": e["max_completion_tokens"] = 1
    elif change == "price": e["pricing"]["prompt"] = "999"
    elif change == "image_fee": e["pricing"]["image"] = "0.01"
    elif change == "request_fee": e["pricing"]["request"] = "0.01"
    elif change == "cache_write": e["pricing"]["input_cache_write"] = "0.001"
    elif change == "reasoning": e["pricing"]["internal_reasoning"] = "0.001"
    elif change == "overrides": e["pricing"]["overrides"] = [{"tier": "expensive"}]
    elif change == "unknown_fee": e["pricing"]["new_fee"] = "0.01"
    else: e["status"] = 1
    with pytest.raises(ReviewError):
        verify(bundle, endpoints, zdr)


def test_cli_requires_endpoint_evidence_before_writing_media_bundle(tmp_path, capsys):
    from scripts import review_openrouter_models as cli

    now = datetime.now(UTC)
    review = plan()
    review["models"][0].update(reviewed_at=now.isoformat(), expires_at=(now + timedelta(hours=1)).isoformat())
    _, endpoints, zdr = endpoint_fixture()
    source, policy, output = (tmp_path / n for n in ("models.json", "plan.json", "bundle.json"))
    source.write_bytes(catalog.payload())
    policy.write_text(json.dumps(review))
    args = ["--input", str(source), "--review", str(policy), "--observed-at",
            (now - timedelta(minutes=1)).isoformat(), "--ack-account-access",
            "--ack-retail-prices", "--output", str(output)]
    assert cli.main(args) == 2
    assert not output.exists()
    assert "--endpoint-input" in capsys.readouterr().err
    endpoint_path, zdr_path = tmp_path / "endpoints.json", tmp_path / "zdr.json"
    endpoint_path.write_text(json.dumps(endpoints))
    zdr_path.write_text(json.dumps(zdr))
    assert cli.main([*args, "--endpoint-input", str(endpoint_path), "--zdr-input", str(zdr_path)]) == 0
    bundle = json.loads(output.read_text())
    assert bundle["installed"] is False
    assert bundle["provider_catalog"][0]["enabled"] is False
    assert bundle["endpoint_verification"]["vendor/model-v2"]["provider"] == "fixture-provider"
