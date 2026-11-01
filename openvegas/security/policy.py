"""Policy mediation helpers for inference/tool capabilities."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class PolicyDecision:
    allow: bool
    code: str
    reason: str


def contains_disallowed_scraping(prompt: str) -> bool:
    text = str(prompt or "").strip().lower()
    if not text:
        return False
    blocked_targets = {
        "zillow",
        "apartments.com",
        "linkedin",
    }
    has_target = any(t in text for t in blocked_targets)
    has_bypass_intent = any(
        phrase in text
        for phrase in (
            "bypass",
            "selenium",
            "captcha",
            "access controls",
            "anti-bot",
            "bypass restrictions",
        )
    )
    return bool(has_target and has_bypass_intent)


def _safe_web_patterns() -> list[str]:
    raw = str(os.getenv("OPENVEGAS_WEB_SOURCE_ALLOWLIST", "*")).strip()
    if not raw:
        return ["*"]
    out = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return out or ["*"]


def _blocked_web_patterns() -> list[str]:
    raw = str(os.getenv("OPENVEGAS_WEB_SOURCE_BLOCKLIST", "")).strip()
    if not raw:
        return []
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _match_any(value: str, patterns: list[str]) -> bool:
    val = str(value or "").strip().lower()
    if not val:
        return False
    return any(fnmatch.fnmatch(val, pattern) for pattern in patterns)


def score_source_trust(url: str) -> float:
    token = str(url or "").strip()
    if not token:
        return 0.0
    try:
        parsed = urlsplit(token)
    except Exception:
        return 0.0
    host = str(parsed.netloc or "").lower()
    if not host:
        return 0.0
    if _match_any(host, _blocked_web_patterns()):
        return 0.0
    allow_patterns = _safe_web_patterns()
    if allow_patterns != ["*"] and not _match_any(host, allow_patterns):
        return 0.2
    if host.endswith(".gov") or host.endswith(".edu"):
        return 0.95
    if host in {"openai.com", "platform.openai.com", "docs.anthropic.com"}:
        return 0.95
    if host.endswith(".org"):
        return 0.8
    return 0.65


def filter_trusted_sources(urls: list[str], *, min_score: float = 0.25) -> tuple[list[str], list[dict[str, object]]]:
    out: list[str] = []
    scored: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in urls:
        token = str(raw or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        score = float(score_source_trust(token))
        scored.append({"url": token, "score": score})
        if score >= float(min_score):
            out.append(token)
    return out, scored


def enforce_before_tool_call(user_id: str, capability: str, payload: dict) -> PolicyDecision:
    _ = str(user_id or "")
    feature = str(capability or "").strip().lower()
    if feature == "web_search":
        prompt = str((payload or {}).get("prompt") or "")
        if contains_disallowed_scraping(prompt):
            return PolicyDecision(
                allow=False,
                code="policy.scrape_block",
                reason="Scraping-restricted request.",
            )
    return PolicyDecision(allow=True, code="ok", reason="allowed")


def contains_obvious_secret(text: str) -> bool:
    token = str(text or "")
    if not token:
        return False
    patterns = [
        r"sk-[a-zA-Z0-9]{20,}",
        r"AIza[0-9A-Za-z\-_]{20,}",
        r"ghp_[0-9A-Za-z]{20,}",
    ]
    return any(re.search(p, token) for p in patterns)
