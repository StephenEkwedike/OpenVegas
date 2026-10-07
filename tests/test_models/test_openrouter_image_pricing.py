"""Bounded image fees are opt-in supplier costs, never invented customer tokens."""

import copy
from decimal import Decimal

import pytest
import test_openrouter_attachment_catalog as catalog
import test_openrouter_attachment_request as request
import test_native_handoff_context as handoff
from test_openrouter_attachment_request import setup  # noqa: F401
from test_native_handoff_context import case as handoff_case  # noqa: F401

from openvegas.contracts.errors import ContractError
from openvegas.gateway import openrouter
from openvegas.gateway.inference import AIGateway
from openvegas.gateway.openrouter_catalog import ReviewError
from openvegas.gateway.providers import model_capabilities
from openvegas.gateway.openrouter_web import WebValidationError
from server.services.openrouter_attachments import AttachmentError, validate_attachment_review


def upgrade(review, *, fee="0.0002"):
    policy = review["attachments"]
    policy.update(
        schema_version=2, pricing_policy="bounded_image_fee_v2",
        input_modalities=["text", "image"], pdf_page_tokens=0, pdf_file_overhead_tokens=0,
        cache_policy="implicit_free_only_no_cache_control",
        fee_bound_basis="Synthetic per-input-image USD quote; no automatic paid caching or other fees.",
    )
    policy["non_token_fees"]["image"] = fee
    review["observed_pricing"].update(
        image=fee, input_cache_write="0.000001", audio="0.003", web_search="0.01"
    )


def body(cost):
    return {
        "id": "gen-synthetic-image-fee", "model": request.MODEL,
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Answer"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105, "cost": cost},
    }


@pytest.mark.asyncio
async def test_reviewed_fee_is_frozen_at_dispatch_and_counted_for_each_history_occurrence(setup):
    upgrade(setup.review)
    setup.install_review()
    first, refs = await setup.request()
    req, _ = await setup.request(history=request.retained_history(refs))
    payload = setup.build(req)
    assert payload["provider"]["max_price"]["image"] == 0.0002
    assert payload["provider"]["zdr"] is True
    assert payload["provider"]["allow_fallbacks"] is False
    assert req._managed_openrouter_dispatch.supplier_image_cost_usd == Decimal("0.0004")
    assert first._managed_attachment_context.prepared.supplier_image_cost_usd == Decimal("0.0002")
    result = openrouter.parse_response(body("0.000510"), req, setup.config, AIGateway._parse_local_tool_call)
    assert result["actual_cost_usd"] == Decimal("0.000510")
    assert (result["input_tokens"], result["output_tokens"]) == (100, 5)
    assert AIGateway._calculate_v_cost(None, setup.config, 100, 5) == Decimal("0.001100")
    with pytest.raises(ValueError, match="approved token prices"):
        openrouter.parse_response(body("0.000512"), req, setup.config, AIGateway._parse_local_tool_call)
    setup.review["attachments"]["non_token_fees"]["image"] = "999"
    setup.install_review()
    # Settlement uses the original bound, not a changed operator review.
    assert openrouter.parse_response(body("0.000510"), req, setup.config, AIGateway._parse_local_tool_call)
    with pytest.raises(ContractError):
        setup.build(req)


@pytest.mark.asyncio
async def test_text_upload_does_not_incur_an_image_fee(setup):
    upgrade(setup.review)
    setup.install_review()
    req, _ = await setup.request(file_ids=[request.TEXT_ID])
    setup.build(req)
    assert req._managed_openrouter_dispatch.supplier_image_cost_usd == 0
    with pytest.raises(ValueError, match="approved token prices"):
        openrouter.parse_response(body("0.000310"), req, setup.config, AIGateway._parse_local_tool_call)


@pytest.mark.asyncio
async def test_preflight_bound_includes_exact_image_fee_and_discovery_honors_schema_two(setup):
    upgrade(setup.review)
    setup.install_review()
    caps = model_capabilities("openrouter", request.MODEL)
    assert caps["image_input"] is True and caps["file_upload"] is True
    req, _ = await setup.request()
    bound = openrouter.supplier_cost_bound(req, setup.config, caps)
    dispatch = req._managed_openrouter_dispatch
    expected = (Decimal(dispatch.input_tokens) + Decimal(req.max_tokens) * 2) / 1_000_000
    assert bound == expected + Decimal("0.000201")
    # Paid multi-pass search needs a separate combined fee review, not a silent
    # override back to zero-image pricing or an underestimated pass count.
    with pytest.raises(WebValidationError, match="web_attachment_image_fee_unreviewed"):
        openrouter._web_attachment_options(req, setup.config, None)


@pytest.mark.asyncio
async def test_explicit_cache_metadata_is_refused_but_literal_text_is_not(setup):
    upgrade(setup.review)
    setup.install_review()
    req, _ = await setup.request(prompt='Explain the literal string "cache_control".')
    payload = setup.build(req)
    payload["messages"][0]["cache_control"] = {"type": "ephemeral"}
    with pytest.raises(ContractError, match="Paid prompt caching"):
        openrouter._dispatch_metering(req, setup.config, 10000, payload)


@pytest.mark.parametrize("change", ["legacy", "fee_mismatch", "unquoted", "nan", "pdf", "cache_policy", "basis", "request_fee", "unknown", "read_fee"])
def test_invalid_image_review_still_fails_closed(change):
    review = request.model_review()
    upgrade(review)
    policy = review["attachments"]
    if change == "legacy":
        policy.update(schema_version=1, pricing_policy="input_tokens_only")
        policy.pop("cache_policy")
        policy.pop("fee_bound_basis")
    elif change == "fee_mismatch": review["observed_pricing"]["image"] = "0.0003"
    elif change == "unquoted": review["observed_pricing"].pop("image")
    elif change == "nan": policy["non_token_fees"]["image"] = "NaN"
    elif change == "pdf": policy["input_modalities"].append("file")
    elif change == "cache_policy": policy["cache_policy"] = "automatic_paid"
    elif change == "basis": policy["fee_bound_basis"] = ""
    elif change == "request_fee": policy["non_token_fees"]["request"] = "0.01"
    elif change == "unknown": review["observed_pricing"]["new_fee"] = "0.01"
    else: review["observed_pricing"]["input_cache_read"] = "999"
    with pytest.raises(AttachmentError):
        validate_attachment_review(model_id=request.MODEL, model_config=request.catalog_config(), model_review=review)


def test_endpoint_checks_reviewed_fee_and_retains_private_pin():
    bundle, endpoints, zdr = catalog.endpoint_fixture()
    review = bundle["model_reviews"]["openrouter:vendor/model-v2"]
    upgrade(review)
    for endpoint in (endpoints["data"]["endpoints"][0], zdr["data"][0]):
        endpoint["pricing"] = copy.deepcopy(review["observed_pricing"])
    assert catalog.verify(bundle, endpoints, zdr)["vendor/model-v2"]["provider"] == "fixture-provider"
    zdr["data"][0]["pricing"]["image"] = "0.0003"
    with pytest.raises(ReviewError, match="media/request fee"):
        catalog.verify(bundle, endpoints, zdr)


def test_catalog_builder_can_generate_but_never_enable_new_pricing_policy():
    source = catalog.catalog.model()
    source["pricing"].update(image="0.0002", input_cache_write="0.000001", audio="0.003", web_search="0.01")
    data = catalog.catalog.payload(source)
    plan = catalog.plan()
    plan["source_sha256"] = catalog.catalog.plan(data)["source_sha256"]
    temporary = {"attachments": plan["models"][0]["attachments"], "observed_pricing": {}}
    upgrade(temporary)
    result = catalog.catalog.build(data=data, review=plan)
    assert result["provider_catalog"][0]["enabled"] is False
    assert result["model_reviews"]["openrouter:vendor/model-v2"]["pricing_policy"] == "owned_native_media_bounded_image_fee"


@pytest.mark.asyncio
async def test_handoff_composition_preserves_image_fee_and_owned_public_context(handoff_case):
    upgrade(handoff_case.review)
    current, _ = await handoff.add_current_files(handoff_case)
    destination = await handoff.prepare(handoff_case)
    wire = openrouter.build_payload(destination, handoff_case.config, handoff_case.capabilities)
    assert wire["provider"]["max_price"]["image"] == 0.0002
    assert destination._managed_openrouter_dispatch.supplier_image_cost_usd == Decimal("0.0002")
    assert current.blocks[-1] in destination.messages[-1]["content"]
    assert "complete text" in str(destination.messages)
    assert "Historical tool observations" in str(destination.messages)
