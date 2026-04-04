from __future__ import annotations

from openvegas.security.policy import (
    contains_disallowed_scraping,
    enforce_before_tool_call,
    filter_trusted_sources,
)


def test_contains_disallowed_scraping_detects_blocked_prompt():
    assert contains_disallowed_scraping("can you scrape zillow using selenium") is True
    assert contains_disallowed_scraping("find houses in austin") is False


def test_enforce_before_tool_call_blocks_scraping_prompt():
    decision = enforce_before_tool_call(
        "u1",
        "web_search",
        {"prompt": "scrape zillow listings bypass access controls"},
    )
    assert decision.allow is False
    assert decision.code == "policy.scrape_block"


def test_filter_trusted_sources_scores_and_filters():
    trusted, scored = filter_trusted_sources(
        ["https://example.org/a", "https://example.com/b", "not-a-url"],
        min_score=0.5,
    )
    assert "https://example.org/a" in trusted
    assert len(scored) >= 2

