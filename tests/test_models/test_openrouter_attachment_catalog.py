"""Generating an attachment review never activates a model or invents limits."""

import copy

import pytest
import test_openrouter_attachments as media
import test_openrouter_catalog as catalog

from openvegas.gateway.openrouter_catalog import ReviewError


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
