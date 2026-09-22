"""Offline bounded-web contracts. Fixture prices are NOT approved retail prices."""

from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest

from openvegas.gateway import openrouter_web as web

NOW = datetime(2026, 9, 21, tzinfo=UTC)
D = Decimal


def prices(**changes):
    return web.ReviewedPrices(
        **(
            {
                "review_id": "synthetic-test-review",
                "expires_at": NOW + timedelta(hours=1),
                "supplier_input_usd_per_million": D("1"),
                "supplier_output_usd_per_million": D("2"),
                "supplier_search_usd_per_call": D("0.007"),
                "supplier_cap_usd": D("1"),
                "retail_input_v_per_million": D("10"),
                "retail_output_v_per_million": D("20"),
                "retail_search_v_per_call": D("0.125"),
                "retail_cap_v": D("10"),
            }
            | changes
        )
    )


def execution(**changes):
    return web.ReviewedExecution(
        **(
            {
                "review_id": "synthetic-execution-review",
                "evidence_ref": "fixture-review-record",
                "price_review_id": "synthetic-test-review",
                "expires_at": NOW + timedelta(hours=1),
                "model": "fixture/exact-model-20260901",
                "provider_slug": "fixture-endpoint",
                "context_window_tokens": 8192,
                "max_output_tokens": 1024,
                "account_plugins_reviewed": True,
                "combined_output_budget_reviewed": True,
                "aggregate_usage_reviewed": True,
                "token_prices_cover_all_fees": True,
            }
            | changes
        )
    )


def usage(searches=1, **changes):
    return {
        "prompt_tokens": 200,
        "completion_tokens": 100,
        "total_tokens": 300,
        "server_tool_use": {"web_search_requests": searches},
        "cost": "0.0074" if searches else "0.0004",
        **changes,
    }


def annotation(**changes):
    return {
        "type": "url_citation",
        "url_citation": {
            "url": "https://news.example.com/article",
            "title": "Example",
            "content": "Bounded excerpt",
            "start_index": 0,
            "end_index": 6,
            **changes,
        },
    }


def message(annotations=None):
    return {
        "role": "assistant",
        "content": "Source says something.",
        "annotations": [annotation()] if annotations is None else annotations,
    }


def receipt(u=None, m=None, **changes):
    return web.parse_receipt(
        usage() if u is None else u,
        message() if m is None else m,
        context_window_tokens=8192,
        max_tokens=64,
        prices=prices(),
        **changes,
    )


def test_exact_exa_wire_limits_and_no_silent_paid_surfaces():
    fields = web.candidate_request_fields()
    assert fields["tools"] == [
        {
            "type": "openrouter:web_search",
            "parameters": {
                "engine": "exa",
                "mode": "fast",
                "max_uses": 1,
                "max_results": 3,
                "max_total_results": 3,
                "max_characters": 2000,
            },
        }
    ]
    assert fields["max_tool_calls"] == 1
    assert fields["parallel_tool_calls"] is False
    assert fields["stream"] is False
    assert "transforms" not in fields
    assert fields["tool_choice"] == "auto"  # May legitimately perform ZERO searches.
    assert fields["plugins"] == [
        {"id": "web", "enabled": False},
        {"id": "file-parser", "enabled": False},
        {"id": "response-healing", "enabled": False},
        {"id": "context-compression", "enabled": False},
    ]
    assert "stop_server_tools_when" not in fields  # Overrides the step budget.
    assert "provider" not in fields  # Never claim request max_price caps server fees.
    fields["tools"][0]["parameters"]["engine"] = "native"
    assert web.candidate_request_fields()["tools"][0]["parameters"]["engine"] == "exa"


@pytest.mark.parametrize(
    "field,bad",
    [
        ("max_results", 0),
        ("max_results", 11),
        ("max_results", True),
        ("max_results", "3"),
        ("max_results", 2.0),
        ("max_characters", False),
        ("max_characters", -1),
        ("max_characters", 10001),
        ("max_characters", None),
    ],
)
def test_invalid_limits(field, bad):
    with pytest.raises(web.WebValidationError):
        web.WebLimits(**{field: bad})


def test_boundary_limits():
    fields = web.candidate_request_fields(web.WebLimits(10, 10_000))
    assert fields["tools"][0]["parameters"]["max_total_results"] == 10


def test_budget_covers_both_full_contexts_both_outputs_and_one_search():
    bound = web.candidate_budget(context_window_tokens=8192, max_tokens=64, prices=prices())
    assert (bound.input_tokens, bound.output_tokens) == (16384, 128)
    assert bound.supplier_tokens_usd == D("0.016640")
    assert bound.supplier_search_usd == D("0.007")
    assert bound.supplier_total_usd == D("0.023640")
    assert bound.retail_tokens_v == D("0.166400")
    assert bound.retail_search_v == D("0.125")
    assert bound.retail_reservation_v == D("0.291400")
    assert bound.enforceable is False


def test_rounding_is_conservative_and_independent_of_global_decimal_context():
    p = prices(
        retail_input_v_per_million="0.000001",
        retail_search_v_per_call="0",
        retail_output_v_per_million="0",
        supplier_input_usd_per_million="0.000001",
        supplier_output_usd_per_million="0",
    )
    with localcontext() as ctx:
        ctx.prec = 3
        bound = web.candidate_budget(context_window_tokens=1, max_tokens=1, prices=p)
    assert bound.retail_reservation_v == D("0.000001")
    assert bound.supplier_total_usd == D("0.007001")


@pytest.mark.parametrize(
    "bad",
    [
        True,
        False,
        0.5,
        -1,
        "-1",
        "NaN",
        "Infinity",
        D("NaN"),
        D("Infinity"),
        "1e-3",
        "",
        None,
        "1000001",
        D("1e-10000"),
        "0.0000000000001",
    ],
)
def test_no_invalid_or_binary_float_prices(bad):
    with pytest.raises(web.WebValidationError):
        prices(retail_search_v_per_call=bad)


def test_all_retail_prices_are_explicit_and_snapshot_immutable():
    with pytest.raises(TypeError):
        web.ReviewedPrices(review_id="test", expires_at=NOW)
    p = prices(retail_search_v_per_call="0")  # Explicit zero is allowed, not defaulted.
    assert p.retail_search_v_per_call == 0
    with pytest.raises(FrozenInstanceError):
        p.retail_search_v_per_call = D("999")


@pytest.mark.parametrize(
    "changes",
    [
        {"review_id": ""},
        {"review_id": "secret\nreflected"},
        {"review_id": []},
        {"expires_at": "2026-09-22"},
        {"expires_at": NOW.replace(tzinfo=None)},
    ],
)
def test_bad_review_metadata(changes):
    with pytest.raises(web.WebValidationError):
        prices(**changes)


def decision(**changes):
    return web.preflight(
        **(
            {
                "context_window_tokens": 8192,
                "max_tokens": 64,
                "prices": prices(),
                "now": NOW,
                "execution": execution(),
            }
            | changes
        )
    )


def test_preflight_requires_explicit_operator_review_not_only_plausible_estimates():
    result = decision(execution=None)
    assert result.allowed is False
    assert result.candidate is not None
    assert {b.code for b in result.blocks} == {"execution_review_required"}
    with pytest.raises(web.WebPreflightBlocked) as exc:
        result.require_ready()
    assert exc.value.blocks == result.blocks
    assert all(block.source.startswith("https://openrouter.ai/") for block in result.blocks)


def test_missing_review_never_invents_retail_price():
    result = decision(prices=None)
    assert result.candidate is None
    assert "reviewed_prices_required" in {b.code for b in result.blocks}


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"expires_at": NOW}, "price_review_expired"),
        ({"supplier_search_usd_per_call": "0.001"}, "search_price_review_mismatch"),
        ({"supplier_search_usd_per_call": "0.01"}, "search_price_review_mismatch"),
        ({"supplier_cap_usd": "0.02"}, "supplier_cap_exceeded"),
        ({"retail_cap_v": "0.29"}, "retail_cap_exceeded"),
    ],
)
def test_separate_reviewed_caps_and_expiration(changes, code):
    assert code in {b.code for b in decision(prices=prices(**changes)).blocks}


def test_exact_caps_fit_with_fresh_scoped_execution_review():
    result = decision(prices=prices(supplier_cap_usd="0.023640", retail_cap_v="0.291400"))
    assert not any(b.code.endswith("cap_exceeded") for b in result.blocks)
    assert result.allowed is True
    assert result.require_ready().enforceable is True


@pytest.mark.parametrize(
    "context,maximum", [(True, 1), (0, 1), (8192, True), (8192, 8193), (10_000_001, 1), (100, 0)]
)
def test_invalid_token_budgets_return_blocks(context, maximum):
    result = decision(context_window_tokens=context, max_tokens=maximum)
    assert result.candidate is None
    assert any(b.code in {"invalid_context_limit", "invalid_output_limit"} for b in result.blocks)


def test_bad_review_clock_or_untyped_review():
    with pytest.raises(web.WebValidationError, match="invalid_review_clock"):
        decision(now=NOW.replace(tzinfo=None))
    with pytest.raises(web.WebValidationError, match="invalid_price_review"):
        decision(prices={"approved": True})


def test_legacy_alternative_is_not_fake_safe_fallback():
    blocks = web.legacy_plugin_blocks()
    assert {b.code for b in blocks} >= {
        "legacy_plugin_content_bound_unverified",
        "legacy_plugin_accounting_unverified",
        "account_plugins_unverified",
    }
    assert all(b.source in web.DOC_SOURCES for b in blocks)


def test_aggregate_metering_allows_more_than_single_output_budget_without_double_charging():
    result = receipt()
    assert result.output_tokens == 100
    assert result.output_tokens > 64
    assert result.web_search_requests == 1 and result.web_search_used
    assert result.actual_cost_usd == D("0.0074")  # NOT 0.0144; already includes search.
    assert result.supplier_search_ceiling_usd == D("0.007")
    assert result.web_search_cost_v == D("0.125")
    assert result.retail_charge_candidate_v == D("0.129")
    assert result.sources == ("https://news.example.com/article",)
    assert result.settlement_authorized is False


def test_zero_is_explicit_and_one_search_can_have_no_results():
    zero = receipt(usage(0), message([]))
    assert zero.web_search_used is False
    assert zero.web_search_cost_v == zero.supplier_search_ceiling_usd == 0
    one = receipt(usage(), message([]))
    assert one.web_search_used is True and one.sources == ()
    assert one.web_search_cost_v == D("0.125")


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        [],
        {"web_search_requests": None},
        {"web_search_requests": True},
        {"web_search_requests": "1"},
        {"web_search_requests": -1},
        {"web_search_requests": 2},
        {"web_search_requests": 1.0},
        {"web_search_requests": 1, "web_fetch_requests": 0},
    ],
)
def test_unknown_or_unbounded_search_usage_is_not_zero(bad):
    with pytest.raises(web.WebValidationError):
        receipt(usage(server_tool_use=bad), message([]))


def test_missing_search_usage_not_inferred_from_annotations_or_text():
    u = usage()
    del u["server_tool_use"]
    with pytest.raises(web.WebValidationError, match="missing_search_usage"):
        receipt(u)
    with pytest.raises(web.WebValidationError, match="citations_without_search"):
        receipt(usage(0))


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"prompt_tokens": 16385}, "invalid_token_usage"),
        ({"completion_tokens": 129}, "invalid_token_usage"),
        ({"prompt_tokens": True}, "invalid_token_usage"),
        ({"completion_tokens": "100"}, "invalid_token_usage"),
        ({"total_tokens": False}, "invalid_total_tokens"),
        ({"total_tokens": 301}, "inconsistent_total_tokens"),
        ({"input_tokens": 201}, "conflicting_token_usage"),
        ({"output_tokens": 99}, "conflicting_token_usage"),
        ({"completion_tokens_details": []}, "invalid_reasoning_usage"),
        ({"completion_tokens_details": {"reasoning_tokens": 101}}, "invalid_reasoning_usage"),
        ({"cost": "0.007400000001"}, "supplier_charge_exceeds_review"),
        ({"cost": float("nan")}, "invalid_decimal_price"),
        ({"cost": None}, "invalid_decimal_price"),
    ],
)
def test_malformed_or_excess_metering(changes, code):
    with pytest.raises(web.WebValidationError, match=code):
        receipt(usage(**changes))


def test_usage_aliases_and_decimal_json_supported():
    u = usage(input_tokens=200, output_tokens=100)
    del u["prompt_tokens"], u["completion_tokens"]
    assert receipt(u).input_tokens == 200
    decoded = json.loads('{"cost": 0.0074}', parse_float=D)
    assert receipt(usage(**decoded)).actual_cost_usd == D("0.0074")
    with pytest.raises(web.WebValidationError, match="missing_token_usage"):
        del u["input_tokens"]
        receipt(u)


def test_receipt_caps_are_separate_and_prices_are_not_reloaded():
    for changes, code in [
        ({"supplier_cap_usd": "0.007"}, "supplier_charge_exceeds_review"),
        ({"retail_cap_v": "0.128"}, "retail_charge_exceeds_review"),
    ]:
        with pytest.raises(web.WebValidationError, match=code):
            web.parse_receipt(
                usage(),
                message(),
                context_window_tokens=8192,
                max_tokens=64,
                prices=prices(**changes),
            )
    p = prices()
    newer = replace(p, retail_search_v_per_call=D("0.5"))
    r = web.parse_receipt(usage(), message(), context_window_tokens=8192, max_tokens=64, prices=p)
    assert r.web_search_cost_v == p.retail_search_v_per_call != newer.retail_search_v_per_call


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,x",
        "//example.com/a",
        "https://user:pass@example.com",
        "https://example.com@localhost/",
        "http://localhost/a",
        "http://127.0.0.1/a",
        "http://169.254.169.254/a",
        "http://10.0.0.1/a",
        "http://[::1]/a",
        "http://[fc00::1]/a",
        "http://2130706433/a",
        "http://127.1/a",
        "http://0x7f000001/a",
        "https://site.internal/a",
        "https://example.com:99999/a",
        "https://example.com:444/a",
        "https://exa mple.com/a",
        "https://example.com/%0aevil",
        "https://example.com/\\bad",
        "https://%65xample.com/a",
        "https://example.com/`bad",
        "https://example.com/<bad>",
        "https://example.com/\x00",
        "https://[broken/",
        "",
        "https://example.com/\ud800",
    ],
)
def test_unsafe_urls_are_rejected_not_fetched(url):
    with pytest.raises(web.WebValidationError, match="invalid_citation_url"):
        receipt(m=message([annotation(url=url)]))


@pytest.mark.parametrize(
    "change,code",
    [
        ({"title": None}, "invalid_citation_title"),
        ({"title": "t" * 513}, "invalid_citation_title"),
        ({"content": "x" * 2001}, "invalid_citation_content"),
        ({"content": 2}, "invalid_citation_content"),
        ({"content": "\ud800"}, "invalid_citation_text"),
        ({"title": "\ud800"}, "invalid_citation_text"),
        ({"start_index": True}, "invalid_citation_span"),
        ({"start_index": -1}, "invalid_citation_span"),
        ({"start_index": 6, "end_index": 6}, "invalid_citation_span"),
        ({"end_index": 999}, "invalid_citation_span"),
        ({"end_index": None}, "invalid_citation_span"),
    ],
)
def test_citation_shape_and_bounds(change, code):
    with pytest.raises(web.WebValidationError, match=code):
        receipt(m=message([annotation(**change)]))


def test_optional_excerpt_and_spans_repeated_sources_do_not_inflate_usage():
    a = annotation()
    for key in ("content", "start_index", "end_index"):
        del a["url_citation"][key]
    result = receipt(m=message([a, copy.deepcopy(a)]))
    assert result.web_search_requests == 1
    assert len(result.citations) == 2 and len(result.sources) == 1
    assert result.citations[0].content is None
    assert result.citations[0].start_index is None


def test_extra_result_and_annotation_caps():
    with pytest.raises(web.WebValidationError, match="citation_result_limit_exceeded"):
        receipt(m=message([annotation(url=f"https://example.com/{i}") for i in range(4)]))
    with pytest.raises(web.WebValidationError, match="invalid_annotations"):
        receipt(m=message([annotation()] * 65))


@pytest.mark.parametrize(
    "bad", [None, "url", {"type": "file"}, {}, {"type": "url_citation", "url_citation": []}]
)
def test_unrecognized_or_file_annotations_rejected(bad):
    with pytest.raises(web.WebValidationError):
        receipt(m=message([bad]))


@pytest.mark.parametrize(
    "change",
    [
        {"content": None},
        {"content": []},
        {"content": "\ud800"},
        {"role": "tool"},
        {"annotations": None},
        {"annotations": {}},
        {"content": "x" * (web.MAX_TEXT_BYTES + 1)},
    ],
)
def test_invalid_message(change):
    with pytest.raises(web.WebValidationError):
        receipt(m=message() | change)


def test_errors_do_not_reflect_upstream_content_and_arguments_are_not_mutated():
    u, m = usage(), message()
    before = copy.deepcopy((u, m))
    receipt(u, m)
    assert (u, m) == before
    with pytest.raises(web.WebValidationError) as exc:
        receipt(m=message([annotation(url="secret-reflected-provider-value")]))
    assert str(exc.value) == "invalid_citation_url"


def test_no_network_or_credentials_required(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("No network is permitted")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    assert decision().allowed is True
    assert receipt().web_search_requests == 1


def test_authoritative_snapshot_keeps_request_budget_limits_and_review_together():
    snapshot = web.WebAccountingSnapshot(8192, 64, prices(), web.WebLimits(1, 50))
    assert snapshot.preflight(now=NOW).candidate.output_tokens == 128
    assert snapshot.parse_receipt(usage(), message()).web_search_cost_v == D("0.125")
    with pytest.raises(web.WebPreflightBlocked):
        snapshot.preflight(now=NOW).require_ready()
    with pytest.raises(FrozenInstanceError):
        snapshot.max_tokens = 1
    with pytest.raises(FrozenInstanceError):
        snapshot.limits.max_results = 10
    with pytest.raises(web.WebValidationError, match="invalid_citation_content"):
        snapshot.parse_receipt(usage(), message([annotation(content="x" * 51)]))


@pytest.mark.parametrize(
    "changes", [{"prices": {}}, {"limits": {}}, {"max_tokens": True}, {"context_window_tokens": 0}]
)
def test_untyped_snapshot_not_authoritative(changes):
    with pytest.raises(web.WebValidationError):
        web.WebAccountingSnapshot(
            **({"context_window_tokens": 8192, "max_tokens": 64, "prices": prices()} | changes)
        )


@pytest.mark.parametrize(
    "field,code",
    [
        ("account_plugins_reviewed", "account_plugins_unverified"),
        ("combined_output_budget_reviewed", "combined_output_budget_unverified"),
        ("aggregate_usage_reviewed", "aggregate_metering_unverified"),
        ("token_prices_cover_all_fees", "token_pricing_scope_unverified"),
    ],
)
def test_operator_gates_are_explicit_and_independently_resolvable(field, code):
    result = decision(execution=execution(**{field: False}))
    assert result.allowed is False
    assert {b.code for b in result.blocks} == {code}
    with pytest.raises(web.WebPreflightBlocked):
        result.require_ready()
    assert decision(execution=execution(**{field: True})).allowed is True


@pytest.mark.parametrize(
    "changes",
    [
        {"account_plugins_reviewed": "true"},
        {"aggregate_usage_reviewed": 1},
        {"combined_output_budget_reviewed": None},
        {"token_prices_cover_all_fees": {}},
        {"model": "openrouter/auto"},
        {"model": "fixture/model:online"},
        {"model": "fixture/latest-model"},
        {"model": "@preset/web"},
        {"provider_slug": ""},
        {"evidence_ref": ""},
        {"review_id": []},
        {"expires_at": NOW.replace(tzinfo=None)},
        {"max_output_tokens": True},
        {"context_window_tokens": 0},
    ],
)
def test_execution_reviews_reject_untyped_or_dynamic_scope(changes):
    with pytest.raises(web.WebValidationError):
        execution(**changes)


def test_review_scope_expiry_context_and_output_limits():
    for changes, code in [
        ({"execution": execution(expires_at=NOW)}, "execution_review_expired"),
        ({"context_window_tokens": 4096}, "context_review_mismatch"),
        ({"max_tokens": 1025}, "output_review_mismatch"),
        ({"max_tokens": 15}, "output_review_mismatch"),
        ({"prices": prices(review_id="different-model-price")}, "price_review_scope_mismatch"),
    ]:
        result = decision(**changes)
        assert not result.allowed and code in {b.code for b in result.blocks}
    with pytest.raises(web.WebValidationError, match="invalid_execution_review"):
        decision(execution={"approved": True})


def prepared(p=None, e=None):
    return web.WebAccountingSnapshot(
        8192, 64, prices() if p is None else p, execution=execution() if e is None else e
    ).prepare(now=NOW)


def test_stop_schema_has_step_and_cost_final_pass_allowance():
    p = prepared(prices(supplier_cap_usd="0.023640", retail_cap_v="0.291400"))
    assert p.budget.final_pass_supplier_usd == D("0.008320")
    assert p.loop_spend_threshold_usd == D("0.008320")
    data = p.payload([{"role": "user", "content": "Search current information"}])
    assert data["stop_server_tools_when"] == [
        {"type": "step_count_is", "step_count": 1},
        {"type": "max_cost", "max_cost_in_dollars": 0.00832},
    ]
    assert "max_tool_calls" not in data  # Explicit replacement preserves the step bound.
    assert data["model"] == execution().model and data["max_tokens"] == 64
    assert data["provider"] == {
        "only": ["fixture-endpoint"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "max_price": {"prompt": 1.0, "completion": 2.0, "request": 0},
    }
    assert len(data["tools"]) == 1
    assert data["tools"][0]["parameters"]["engine"] == "exa"
    assert data["tools"][0]["parameters"]["max_uses"] == 1
    json.dumps(data, allow_nan=False)


def test_spend_threshold_does_not_underreserve_post_step_overshoot_and_final_turn():
    p = prepared(prices(supplier_cap_usd="0.023640"))
    u = usage(prompt_tokens=16384, completion_tokens=128, total_tokens=16512, cost="0.023640")
    result = p.parse_receipt(u, message([]))
    assert result.actual_cost_usd > p.loop_spend_threshold_usd
    assert result.actual_cost_usd == p.budget.supplier_total_usd
    assert result.retail_charge_candidate_v == p.budget.retail_reservation_v
    assert result.web_search_cost_v == D("0.125")
    assert result.settlement_authorized
    # This would fit threshold + a final pass but omit first-pass/search costs.
    with pytest.raises(web.WebPreflightBlocked):
        prepared(prices(supplier_cap_usd="0.02"))


def test_prepared_billing_reuses_review_at_dispatch_not_new_catalog_or_wall_clock():
    p = prepared()
    assert p.snapshot.preflight(now=NOW + timedelta(days=1)).allowed is False
    assert p.parse_receipt(usage(), message()).settlement_authorized is True
    other_catalog = prices(retail_search_v_per_call="0.7")
    result = p.parse_receipt(usage(), message())
    assert result.web_search_cost_v == D("0.125") != other_catalog.retail_search_v_per_call
    with pytest.raises(web.WebValidationError, match="prepared_budget_mismatch"):
        replace(p, budget=replace(p.budget, supplier_total_usd=D("0")))


def test_payload_is_detached_text_only_and_excludes_extra_paid_surfaces():
    p = prepared()
    messages = [{"role": "user", "content": "Original"}]
    data = p.payload(messages)
    data["messages"][0]["content"] = "Mutated"
    assert messages[0]["content"] == "Original"
    data["stop_server_tools_when"].clear()
    assert len(p.payload(messages)["stop_server_tools_when"]) == 2
    assert "presets" not in data and "reasoning" not in data and "models" not in data
    assert all(plugin["enabled"] is False for plugin in data["plugins"])


@pytest.mark.parametrize(
    "messages",
    [
        [],
        None,
        "text",
        [{}],
        [{"role": "user", "content": [{"type": "file", "file": {"file_data": "not-read"}}]}],
        [{"role": "user", "content": "text", "annotations": []}],
        [{"role": "tool", "content": "unreviewed"}],
        [{"role": "user", "content": "\ud800"}],
        [{"role": "user", "content": "x" * web.MAX_TEXT_BYTES}],
        [{"role": "user", "content": "x"}] * 201,
    ],
)
def test_payload_rejects_attachments_extra_fields_and_unbounded_messages(messages):
    with pytest.raises(web.WebValidationError):
        prepared().payload(messages)


def test_unreviewed_snapshot_cannot_prepare_or_dispatch():
    snapshot = web.WebAccountingSnapshot(8192, 64, prices())
    with pytest.raises(web.WebPreflightBlocked):
        snapshot.prepare(now=NOW)


def test_current_chat_usage_alias_and_overlap_are_not_double_counted():
    u = usage()
    u["server_tool_use_details"] = {
        "web_search_requests": 1,
        "tool_calls_requested": 1,
        "tool_calls_executed": 1,
    }
    assert prepared().parse_receipt(u, message()).web_search_requests == 1
    del u["server_tool_use"]
    result = prepared().parse_receipt(u, message())
    assert result.web_search_requests == 1 and result.web_search_cost_v == D("0.125")
    assert result.actual_cost_usd == D("0.0074")


@pytest.mark.parametrize(
    "server,code",
    [
        ({"web_search_requests": 0}, "conflicting_search_usage"),
        ({"web_search_requests": 1, "tool_calls_requested": 2}, "invalid_server_tool_count"),
        ({"web_search_requests": 1, "tool_calls_requested": True}, "invalid_server_tool_count"),
        ({"web_search_requests": 1, "tool_calls_executed": 0}, "inconsistent_server_tool_usage"),
        ({"web_search_requests": 1, "tool_calls_requested": 0}, "inconsistent_server_tool_usage"),
        ({"tool_calls_requested": 1}, "missing_search_usage"),
    ],
)
def test_usage_overlap_conflicts_or_malformed_counts_fail_closed(server, code):
    with pytest.raises(web.WebValidationError, match=code):
        prepared().parse_receipt(usage(server_tool_use_details=server), message())


def test_documented_optional_usage_nulls_and_byok_guard():
    assert receipt(usage(completion_tokens_details=None)).output_tokens == 100
    assert receipt(usage(completion_tokens_details={"reasoning_tokens": None})).output_tokens == 100
    assert (
        receipt(
            usage(server_tool_use={"web_search_requests": 1, "tool_calls_requested": None})
        ).web_search_requests
        == 1
    )
    for invalid in (True, None, 1, "false"):
        with pytest.raises(web.WebValidationError, match="unapproved_byok_usage"):
            receipt(usage(is_byok=invalid))


def test_zero_token_tariff_still_reserves_search_and_cannot_disable_step_limit():
    p = prepared(
        prices(
            supplier_input_usd_per_million="0",
            supplier_output_usd_per_million="0",
            supplier_cap_usd="0.007",
        )
    )
    assert p.loop_spend_threshold_usd == 0
    assert p.budget.supplier_total_usd == D("0.007")
    assert p.payload([{"role": "user", "content": "Search"}])["stop_server_tools_when"] == [
        {"type": "step_count_is", "step_count": 1},
        {"type": "max_cost", "max_cost_in_dollars": 0.0},
    ]


def test_wire_threshold_never_rounds_above_reviewed_decimal_and_sources_are_exact():
    for amount in (D("0.123456789012"), D("0"), D("999999.123456789012")):
        assert D(str(web._wire_price(amount))) <= amount
    assert "https://openrouter.ai/docs/openapi/openapi.yaml" in web.DOC_SOURCES
    assert len(web.OPENAPI_SHA256) == 64


def test_json_integer_zero_cost_is_valid_but_boolean_is_not():
    assert receipt(usage(0, cost=0), message([])).actual_cost_usd == 0
    with pytest.raises(web.WebValidationError, match="invalid_decimal_price"):
        receipt(usage(cost=False))


def test_final_turn_cannot_return_unexecuted_tools():
    p = prepared()
    for calls in ([{"type": "function"}], {}, "tool", False):
        with pytest.raises(web.WebValidationError, match="unexecuted_tool_calls"):
            p.parse_receipt(usage(), message() | {"tool_calls": calls})
    assert p.parse_receipt(usage(), message() | {"tool_calls": []}).settlement_authorized


def server_review():
    return json.loads(
        json.dumps(
            {
                "reviewed_at": NOW - timedelta(hours=1),
                "expires_at": NOW + timedelta(hours=1),
                "account_access": True,
                "completion_chat": True,
                "context_window_tokens": 8192,
                "cost_input_per_1m": "1",
                "cost_output_per_1m": "2",
                "web_search": {
                    "schema_version": 1,
                    "prices": asdict(prices()),
                    "execution": asdict(execution()),
                },
            },
            default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v),
        )
    )


def test_prepare_server_review_has_no_implicit_retail_price_and_freezes_values():
    review = server_review()
    p = web.prepare_server_review(execution().model, 64, review, now=NOW)
    assert p.budget.enforceable
    assert p.budget.retail_reservation_v == D("0.2914")
    review["web_search"]["prices"]["retail_search_v_per_call"] = "999"
    assert p.snapshot.prices.retail_search_v_per_call == D("0.125")
    del review["web_search"]["prices"]["retail_search_v_per_call"]
    with pytest.raises(web.WebValidationError):
        web.prepare_server_review(execution().model, 64, review, now=NOW)


@pytest.mark.parametrize(
    "path,value",
    [
        (("reviewed_at",), (NOW + timedelta(minutes=1)).isoformat()),
        (("expires_at",), NOW.isoformat()),
        (("expires_at",), (NOW + timedelta(days=31)).isoformat()),
        (("expires_at",), "2026-09-21"),
        (("account_access",), False),
        (("completion_chat",), 1),
        (("context_window_tokens",), True),
        (("cost_input_per_1m",), "2"),
        (("web_search", "schema_version"), True),
        (("web_search", "extra"), True),
        (("web_search", "prices", "retail_search_v_per_call"), "0.0000001"),
        (("web_search", "prices", "retail_search_v_per_call"), 0.2),
        (("web_search", "prices", "supplier_cap_usd"), "0.001"),
        (("web_search", "prices", "retail_cap_v"), "0.01"),
        (("web_search", "prices", "expires_at"), (NOW + timedelta(days=1)).isoformat()),
        (("web_search", "execution", "expires_at"), (NOW + timedelta(days=1)).isoformat()),
        (("web_search", "execution", "account_plugins_reviewed"), False),
        (("web_search", "execution", "aggregate_usage_reviewed"), False),
        (("web_search", "execution", "combined_output_budget_reviewed"), False),
        (("web_search", "execution", "token_prices_cover_all_fees"), False),
        (("web_search", "execution", "context_window_tokens"), 8193),
        (("web_search", "execution", "price_review_id"), "unrelated"),
        (("web_search", "execution", "model"), "fixture/different"),
        (("web_search", "limits"), {"max_results": 11}),
    ],
)
def test_server_review_rejects_unreviewed_bounds_or_scope(path, value):
    data = server_review()
    target = data
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(web.WebValidationError):
        web.prepare_server_review(execution().model, 64, data, now=NOW)


@pytest.mark.parametrize(
    "key,value",
    [
        ("provider", "openai"),
        ("model_id", "fixture/wrong"),
        ("enabled", False),
        ("max_tokens", 63),
        ("max_tokens", True),
        ("cost_input_per_1m", "2"),
        ("cost_output_per_1m", "3"),
        ("v_price_input_per_1m", "11"),
        ("v_price_output_per_1m", "21"),
    ],
)
def test_server_review_requires_exact_current_catalog_rates(key, value):
    config = {
        "provider": "openrouter",
        "model_id": execution().model,
        "enabled": True,
        "max_tokens": 1024,
        "cost_input_per_1m": "1",
        "cost_output_per_1m": "2",
        "v_price_input_per_1m": "10",
        "v_price_output_per_1m": "20",
    }
    web.prepare_server_review(execution().model, 64, server_review(), config, now=NOW)
    config[key] = value
    with pytest.raises(web.WebValidationError):
        web.prepare_server_review(execution().model, 64, server_review(), config, now=NOW)


@pytest.mark.parametrize("review", [None, [], "{}", {}, {"reviewed_at": None}])
def test_missing_review_is_not_capability_support(review):
    assert web.reviewed_web_capability(execution().model, review) is False
