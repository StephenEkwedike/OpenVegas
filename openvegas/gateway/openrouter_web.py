"""Offline, reviewed OpenRouter bounded web-search/accounting helpers.

Integration contract (parent owns transport, catalog, route and billing):
* Build an immutable WebAccountingSnapshot from trusted server reviews. Call
  snapshot.prepare() BEFORE credentials, reservation or dispatch. It fails for
  missing/expired reviews, unsupported scope and insufficient USD/$V caps.
* Persist the PreparedWebSearch snapshot with the authoritative request ID/hash.
  Reserve prepared.budget.retail_reservation_v; use prepared.payload(messages)
  unchanged, with no retries. Parent owns transport, identity and finish checks.
* prepared.parse_receipt() validates against the original dispatch snapshot even
  if the catalog changes or review expires during the request. Add its nonnegative
  web_search_cost_v ONCE to token-priced retail charges, not a live catalog price.
  Keep usage.cost as supplier total; do NOT add the search fee to it again.
  Retail is separately reviewed $V, never a USD conversion or inferred markup.
  Reject/uncertain receipts require reconciliation, never a free replay.

Sources checked 2026-09-21/22 are enumerated in DOC_SOURCES. Research findings:
The server tool supports explicit Exa, result/content/use limits and reports
web_search_requests. Fast Exa is $0.007/search for <=10 results. OpenAPI documents
step_count_is plus max_cost with OR semantics, then pending tools and one final
tools-disabled turn. Thus one step permits at most two generations in this
single server-tool configuration. Reserve 2*C input and 2*M combined reasoning/output.
max_cost is post-spend, not a hard invoice cap: retain a complete first-pass,
search and final-pass allowance. max_price.request filters provider unit fees,
not server fees. Model-specific output/price/metering and account reviews remain
required, rather than assuming all providers share reasoning/output semantics.
The transport may compose reviewed local function definitions (never executed
here) and server-owned attachments on the same exact, fee-free native endpoint.
Their full schema/media budgets are checked at dispatch; no auto paid OCR is used.

Legacy alternative: the Exa web plugin runs once but is deprecated; its docs
describe adaptive excerpts, not an enforceable max_characters parameter. Do
not invent one. Text-only input and disabled file-parser avoid opting into
automatic paid OCR; locked account defaults can override request settings.
No currently documented legacy alternative passes this bounded contract.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
from urllib.parse import unquote, urlsplit

DOC_SOURCES = (
    "https://openrouter.ai/docs/guides/features/server-tools/web-search",
    "https://openrouter.ai/docs/guides/features/server-tools#tool-call-limits",
    "https://openrouter.ai/docs/guides/routing/provider-selection#max-price",
    "https://openrouter.ai/docs/cookbook/administration/usage-accounting",
    "https://openrouter.ai/docs/guides/best-practices/reasoning-tokens",
    "https://openrouter.ai/docs/guides/features/plugins/web-search",
    "https://openrouter.ai/docs/guides/features/plugins#disabling-a-default-plugin",
    "https://openrouter.ai/docs/guides/overview/multimodal/pdfs",
    "https://openrouter.ai/docs/openapi/openapi.yaml",
    "https://openrouter.ai/docs/api/api-reference/chat/create-a-chat-completion",
    "https://openrouter.ai/docs/guides/features/message-transforms",
)
# Public schema fetched 2026-09-21; useful when auditing future contract changes.
OPENAPI_SHA256 = "78f08554a81a85f1914dbfa1f23b273da17e2e9d96d735b9940b7abd94165126"
EXA_FAST_OBSERVED_USD = Decimal("0.007")  # Observation, never a retail default.
SCALE = Decimal("0.000001")
MAX_TOKENS = 10_000_000
MAX_TEXT_BYTES = 1_000_000
MAX_ANNOTATIONS = 64
_MONEY = re.compile(r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,12})?\Z")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class WebValidationError(ValueError):
    """Fixed diagnostic codes only; never echo upstream text or URLs."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _integer(value: object, minimum: int, maximum: int, code: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise WebValidationError(code)
    return value


def _money(value: object) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise WebValidationError("invalid_decimal_price")
    if isinstance(value, str) and not _MONEY.fullmatch(value):
        raise WebValidationError("invalid_decimal_price")
    try:
        amount = Decimal(value)
        if (
            not amount.is_finite()
            or not 0 <= amount <= 1_000_000
            or amount.as_tuple().exponent < -12
        ):
            raise WebValidationError("invalid_decimal_price")
        return amount
    except InvalidOperation:
        raise WebValidationError("invalid_decimal_price") from None


def _ceil(amount: Decimal) -> Decimal:
    return amount.quantize(SCALE, rounding=ROUND_CEILING)


def _wire_price(amount: Decimal) -> float:
    """Never round a JSON number above the reviewed rate/threshold."""
    result = float(amount)
    if Decimal(str(result)) > amount:
        result = math.nextafter(result, 0)
    return result


def _expiry(value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise WebValidationError("invalid_price_review_expiry")


def _clock(now: datetime | None) -> datetime:
    now = datetime.now(UTC) if now is None else now
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise WebValidationError("invalid_review_clock")
    return now


@dataclass(frozen=True)
class WebLimits:
    max_results: int = 3
    max_characters: int = 2000

    def __post_init__(self):
        # Staying <=10 avoids Exa's additional-result fee tier altogether.
        _integer(self.max_results, 1, 10, "invalid_result_limit")
        _integer(self.max_characters, 1, 10_000, "invalid_content_limit")


DEFAULT_LIMITS = WebLimits()


def candidate_request_fields(limits: WebLimits = DEFAULT_LIMITS) -> dict:
    """Review-only fragment; use PreparedWebSearch.payload() for dispatch."""
    return {
        "tools": [
            {
                "type": "openrouter:web_search",
                "parameters": {
                    "engine": "exa",
                    "mode": "fast",
                    "max_uses": 1,
                    "max_results": limits.max_results,
                    "max_total_results": limits.max_results,
                    "max_characters": limits.max_characters,
                },
            }
        ],
        "max_tool_calls": 1,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "plugins": [
            {"id": "web", "enabled": False},
            {"id": "file-parser", "enabled": False},
            {"id": "response-healing", "enabled": False},
            {"id": "context-compression", "enabled": False},
        ],
        "stream": False,
    }


@dataclass(frozen=True)
class ReviewedPrices:
    """Trusted server review, never customer parameters. All amounts explicit."""

    review_id: str
    expires_at: datetime
    supplier_input_usd_per_million: Decimal
    supplier_output_usd_per_million: Decimal
    supplier_search_usd_per_call: Decimal
    supplier_cap_usd: Decimal
    retail_input_v_per_million: Decimal
    retail_output_v_per_million: Decimal
    retail_search_v_per_call: Decimal
    retail_cap_v: Decimal

    def __post_init__(self):
        if not isinstance(self.review_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", self.review_id
        ):
            raise WebValidationError("invalid_price_review_id")
        _expiry(self.expires_at)
        for field in self.__dataclass_fields__:
            if field not in {"review_id", "expires_at"}:
                object.__setattr__(self, field, _money(getattr(self, field)))


@dataclass(frozen=True)
class ReviewedExecution:
    """Operator attestations scoped to one exact model/endpoint, never user flags.

    evidence_ref identifies an operator review record, not a secret. Account
    review covers locked/default plugins, no injected tools/inner loops, no OCR,
    no BYOK/other charges, and the pinned Exa configuration. Pricing review must
    cover cache writes, reasoning and context tiers within the full window with
    no extra provider unit fees. Combined output review confirms max_tokens is
    honored on BOTH turns, including reasoning (and provider minimums). Usage
    review confirms the chosen endpoint's request-total token/cost accounting.
    """

    review_id: str
    evidence_ref: str
    price_review_id: str
    expires_at: datetime
    model: str
    provider_slug: str
    context_window_tokens: int
    max_output_tokens: int
    account_plugins_reviewed: bool
    combined_output_budget_reviewed: bool
    aggregate_usage_reviewed: bool
    token_prices_cover_all_fees: bool

    def __post_init__(self):
        if any(
            not isinstance(v, str) or not _ID.fullmatch(v)
            for v in (self.review_id, self.evidence_ref, self.price_review_id)
        ):
            raise WebValidationError("invalid_execution_review_id")
        _expiry(self.expires_at)
        if (
            not isinstance(self.model, str)
            or not re.fullmatch(
                r"[a-z0-9][a-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.model
            )
            or self.model.startswith("openrouter/")
            or re.search(
                r"(^|[-_.])(auto|latest|router)([-_.]|$)",
                self.model.split("/", 1)[-1],
                re.IGNORECASE,
            )
        ):
            raise WebValidationError("invalid_reviewed_model")
        if not isinstance(self.provider_slug, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9._/-]{0,127}", self.provider_slug
        ):
            raise WebValidationError("invalid_reviewed_endpoint")
        c = _integer(self.context_window_tokens, 1, MAX_TOKENS, "invalid_context_limit")
        _integer(self.max_output_tokens, 16, c, "invalid_output_limit")
        for name in _EXECUTION_GATES:
            if type(getattr(self, name)) is not bool:
                raise WebValidationError("invalid_execution_attestation")


@dataclass(frozen=True)
class CandidateBudget:
    """Two-pass allowance; preflight validates its endpoint/review/cap premises."""

    input_tokens: int
    output_tokens: int
    supplier_tokens_usd: Decimal
    supplier_search_usd: Decimal
    supplier_total_usd: Decimal
    retail_tokens_v: Decimal
    retail_search_v: Decimal
    retail_reservation_v: Decimal
    final_pass_supplier_usd: Decimal
    enforceable: bool = False


def candidate_budget(
    *, context_window_tokens: int, max_tokens: int, prices: ReviewedPrices
) -> CandidateBudget:
    """Assuming <=2 total billable passes, <=C input and <=M output EACH.

    2*C input deliberately over-reserves repeated history, tool schemas, URLs,
    titles and previous generated tokens, whose sizes max_characters does NOT
    bound. 2*M output includes query generation and final answer, conditional
    on a reviewed combined reasoning/output limit. No byte/token ratio guess.
    Prices must also bound cache writes, long-context tiers and all token fees.
    """
    c = _integer(context_window_tokens, 1, MAX_TOKENS, "invalid_context_limit")
    m = _integer(max_tokens, 1, c, "invalid_output_limit")
    with localcontext() as ctx:
        ctx.prec = 60
        supplier_tokens = (
            2 * c * prices.supplier_input_usd_per_million
            + 2 * m * prices.supplier_output_usd_per_million
        ) / 1_000_000
        retail_tokens = (
            2 * c * prices.retail_input_v_per_million + 2 * m * prices.retail_output_v_per_million
        ) / 1_000_000
        return CandidateBudget(
            2 * c,
            2 * m,
            _ceil(supplier_tokens),
            prices.supplier_search_usd_per_call,
            _ceil(supplier_tokens + prices.supplier_search_usd_per_call),
            _ceil(retail_tokens),
            prices.retail_search_v_per_call,
            _ceil(retail_tokens + prices.retail_search_v_per_call),
            _ceil(supplier_tokens / 2),
        )


@dataclass(frozen=True)
class PreflightBlock:
    code: str
    detail: str
    source: str


_EXECUTION_GATES = {
    "account_plugins_reviewed": PreflightBlock(
        "account_plugins_unverified",
        "Review account defaults/locks and exclude paid plugins, extra tools, inner loops and OCR.",
        DOC_SOURCES[6],
    ),
    "combined_output_budget_reviewed": PreflightBlock(
        "combined_output_budget_unverified",
        "Review this endpoint's combined reasoning/output limit for both generations.",
        DOC_SOURCES[4],
    ),
    "aggregate_usage_reviewed": PreflightBlock(
        "aggregate_metering_unverified",
        "Review request-total token/cost accounting for this endpoint.",
        DOC_SOURCES[3],
    ),
    "token_prices_cover_all_fees": PreflightBlock(
        "token_pricing_scope_unverified",
        "Review full-context token/cache pricing and exclude additional provider unit fees.",
        DOC_SOURCES[2],
    ),
}


class WebPreflightBlocked(WebValidationError):
    def __init__(self, blocks: tuple[PreflightBlock, ...]):
        super().__init__("openrouter_web_preflight_blocked")
        self.blocks = blocks


@dataclass(frozen=True)
class PreflightResult:
    blocks: tuple[PreflightBlock, ...]
    candidate: CandidateBudget | None

    @property
    def allowed(self) -> bool:
        return not self.blocks and self.candidate is not None and self.candidate.enforceable

    def require_ready(self) -> CandidateBudget:
        if not self.allowed:
            raise WebPreflightBlocked(self.blocks)
        return self.candidate


def preflight(
    *,
    context_window_tokens: int,
    max_tokens: int,
    prices: ReviewedPrices | None,
    execution: ReviewedExecution | None = None,
    now: datetime | None = None,
) -> PreflightResult:
    """Review gates, not feature activation or permission to make a paid call."""
    blocks = []
    candidate = None
    now = _clock(now)
    if execution is None:
        blocks.append(
            PreflightBlock(
                "execution_review_required",
                "An explicit model/endpoint/account review is required.",
                DOC_SOURCES[8],
            )
        )
    elif not isinstance(execution, ReviewedExecution):
        raise WebValidationError("invalid_execution_review")
    else:
        if now >= execution.expires_at:
            blocks.append(
                PreflightBlock(
                    "execution_review_expired",
                    "Refresh the operator execution review.",
                    DOC_SOURCES[8],
                )
            )
        blocks.extend(
            block for name, block in _EXECUTION_GATES.items() if not getattr(execution, name)
        )
        if context_window_tokens != execution.context_window_tokens:
            blocks.append(
                PreflightBlock(
                    "context_review_mismatch",
                    "Reserve the endpoint's full reviewed context window.",
                    DOC_SOURCES[8],
                )
            )
        if type(max_tokens) is not int or not 16 <= max_tokens <= execution.max_output_tokens:
            blocks.append(
                PreflightBlock(
                    "output_review_mismatch",
                    "Output must fit reviewed endpoint limits (minimum 16).",
                    DOC_SOURCES[8],
                )
            )
    if prices is None:
        blocks.append(
            PreflightBlock(
                "reviewed_prices_required",
                "Supply separate reviewed USD/$V rates and caps; "
                "there is no default retail search price.",
                DOC_SOURCES[0],
            )
        )
    elif not isinstance(prices, ReviewedPrices):
        raise WebValidationError("invalid_price_review")
    else:
        if execution is not None and prices.review_id != execution.price_review_id:
            blocks.append(
                PreflightBlock(
                    "price_review_scope_mismatch",
                    "Price and execution reviews must reference each other.",
                    DOC_SOURCES[8],
                )
            )
        if now >= prices.expires_at:
            blocks.append(
                PreflightBlock(
                    "price_review_expired", "Refresh the server-owned price review.", DOC_SOURCES[0]
                )
            )
        if prices.supplier_search_usd_per_call != EXA_FAST_OBSERVED_USD:
            blocks.append(
                PreflightBlock(
                    "search_price_review_mismatch",
                    "Re-review the pinned Exa fast tariff.",
                    DOC_SOURCES[0],
                )
            )
        try:
            candidate = candidate_budget(
                context_window_tokens=context_window_tokens, max_tokens=max_tokens, prices=prices
            )
        except WebValidationError as exc:
            blocks.append(PreflightBlock(exc.code, "Invalid token budget.", DOC_SOURCES[1]))
        if candidate is not None:
            for exceeded, code in (
                (candidate.supplier_total_usd > prices.supplier_cap_usd, "supplier_cap_exceeded"),
                (candidate.retail_reservation_v > prices.retail_cap_v, "retail_cap_exceeded"),
            ):
                if exceeded:
                    blocks.append(
                        PreflightBlock(code, "Candidate exceeds the reviewed cap.", DOC_SOURCES[2])
                    )
    if candidate is not None and not blocks:
        candidate = replace(candidate, enforceable=True)
    return PreflightResult(tuple(blocks), candidate)


def legacy_plugin_blocks() -> tuple[PreflightBlock, ...]:
    """Research result, not a fallback payload or silent retry path."""
    return (
        PreflightBlock(
            "legacy_plugin_content_bound_unverified",
            "Deprecated Exa plugin has adaptive excerpts, no documented exact "
            "content cap; do not copy server-tool parameters into a plugin.",
            DOC_SOURCES[5],
        ),
        PreflightBlock(
            "legacy_plugin_accounting_unverified",
            "Once-per-request search is not proof of bounded internal LLM "
            "work or server_tool_use metering for the legacy plugin.",
            DOC_SOURCES[5],
        ),
        _EXECUTION_GATES["account_plugins_reviewed"],
    )


def _public_url(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or re.search(r"[\s\x00-\x1f\x7f\\<>\"`{}]", unquote(value))
    ):
        raise WebValidationError("invalid_citation_url")
    try:
        value.encode("utf-8")
        parts = urlsplit(value)
        host = parts.hostname
        if (
            parts.scheme not in {"https", "http"}
            or not host
            or parts.username is not None
            or parts.password is not None
            or "%" in parts.netloc
            or parts.port not in {None, 80, 443}
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            host = host.encode("idna").decode("ascii").lower()
            if (
                len(host) > 253
                or "." not in host
                or host.endswith((".local", ".localhost", ".internal", ".invalid", ".test"))
                or not re.search(r"[a-z]", host.rsplit(".", 1)[-1])
                or any(
                    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in host.split(".")
                )
            ):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
    except (ValueError, UnicodeError):
        raise WebValidationError("invalid_citation_url") from None
    # Syntactic safety only, not DNS validation or evidence that a page is true.
    # These links must NEVER be auto-fetched by this helper/the billing path.
    return value


@dataclass(frozen=True)
class Citation:
    url: str
    title: str
    content: str | None
    start_index: int | None
    end_index: int | None


def validate_citations(
    message: object, *, search_requests: int, limits: WebLimits = DEFAULT_LIMITS
) -> tuple[Citation, ...]:
    _integer(search_requests, 0, 1, "invalid_search_count")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise WebValidationError("invalid_assistant_message")
    text = message.get("content")
    if not isinstance(text, str):
        raise WebValidationError("invalid_response_text")
    try:
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise WebValidationError("response_text_too_large")
    except UnicodeError:
        raise WebValidationError("invalid_response_text") from None
    annotations = message.get("annotations", [])
    if not isinstance(annotations, list) or len(annotations) > MAX_ANNOTATIONS:
        raise WebValidationError("invalid_annotations")
    if annotations and search_requests == 0:
        raise WebValidationError("citations_without_search")
    result = []
    urls = set()
    for annotation in annotations:
        if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
            raise WebValidationError("unsupported_annotation")
        item = annotation.get("url_citation")
        if not isinstance(item, dict):
            raise WebValidationError("invalid_citation")
        url = _public_url(item.get("url"))
        title, content = item.get("title"), item.get("content")
        if not isinstance(title, str) or not 1 <= len(title) <= 512:
            raise WebValidationError("invalid_citation_title")
        if content is not None and (
            not isinstance(content, str) or len(content) > limits.max_characters
        ):
            raise WebValidationError("invalid_citation_content")
        try:
            title.encode("utf-8")
            if content is not None:
                content.encode("utf-8")
        except UnicodeError:
            raise WebValidationError("invalid_citation_text") from None
        start, end = item.get("start_index"), item.get("end_index")
        if "start_index" in item or "end_index" in item:
            _integer(start, 0, len(text), "invalid_citation_span")
            _integer(end, start + 1, len(text), "invalid_citation_span")
        urls.add(url)
        if len(urls) > limits.max_results:
            raise WebValidationError("citation_result_limit_exceeded")
        result.append(Citation(url, title, content, start, end))
    return tuple(result)


@dataclass(frozen=True)
class ObservedReceipt:
    input_tokens: int
    output_tokens: int
    web_search_requests: int
    actual_cost_usd: Decimal
    supplier_search_ceiling_usd: Decimal
    web_search_cost_v: Decimal
    retail_charge_candidate_v: Decimal
    citations: tuple[Citation, ...]
    settlement_authorized: bool = False

    @property
    def web_search_used(self) -> bool:
        return self.web_search_requests > 0

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(c.url for c in self.citations))


def _tokens(usage: dict, primary: str, alias: str, maximum: int) -> int:
    if primary not in usage and alias not in usage:
        raise WebValidationError("missing_token_usage")
    counts = [
        _integer(usage[k], 0, maximum, "invalid_token_usage")
        for k in (primary, alias)
        if k in usage
    ]
    if len(set(counts)) != 1:
        raise WebValidationError("conflicting_token_usage")
    return counts[0]


def _search_count(usage: dict) -> int:
    # Guides name server_tool_use; current ChatUsage uses server_tool_use_details.
    # Both are documented. Never sum overlapping counters or infer a missing one.
    values = []
    for key in ("server_tool_use", "server_tool_use_details"):
        if key not in usage:
            continue
        server = usage[key]
        if not isinstance(server, dict) or "web_search_requests" not in server:
            raise WebValidationError("missing_search_usage")
        if set(server) - {"web_search_requests", "tool_calls_requested", "tool_calls_executed"}:
            raise WebValidationError("unapproved_server_tool_usage")
        count = _integer(server["web_search_requests"], 0, 1, "invalid_search_count")
        counts = {}
        for name in ("tool_calls_requested", "tool_calls_executed"):
            if name in server and server[name] is not None:
                counts[name] = _integer(server[name], 0, 1, "invalid_server_tool_count")
        requested = counts.get("tool_calls_requested")
        executed = counts.get("tool_calls_executed")
        if (
            (requested is not None and requested < count)
            or (executed is not None and executed < count)
            or (requested is not None and executed is not None and executed > requested)
        ):
            raise WebValidationError("inconsistent_server_tool_usage")
        values.append(count)
    if not values:
        raise WebValidationError("missing_search_usage")
    if len(set(values)) != 1:
        raise WebValidationError("conflicting_search_usage")
    return values[0]


def parse_receipt(
    usage: object,
    message: object,
    *,
    context_window_tokens: int,
    max_tokens: int,
    prices: ReviewedPrices,
    limits: WebLimits = DEFAULT_LIMITS,
) -> ObservedReceipt:
    """Diagnostic receipt only. Decode JSON numbers with parse_float=Decimal.

    The parent validates HTTP/model/request identity, finish status, response byte
    limit and credentials. Never reuse the old single-pass text-only cost ceiling
    for this surface. Missing search metering is unknown, NOT zero or one.
    """
    if not isinstance(usage, dict):
        raise WebValidationError("missing_usage")
    searches = _search_count(usage)
    budget = candidate_budget(
        context_window_tokens=context_window_tokens, max_tokens=max_tokens, prices=prices
    )
    inputs = _tokens(usage, "prompt_tokens", "input_tokens", budget.input_tokens)
    outputs = _tokens(usage, "completion_tokens", "output_tokens", budget.output_tokens)
    if "total_tokens" in usage:
        total = _integer(usage["total_tokens"], 0, 4 * MAX_TOKENS, "invalid_total_tokens")
        if total != inputs + outputs:
            raise WebValidationError("inconsistent_total_tokens")
    details = usage.get("completion_tokens_details")
    if details is None:
        details = {}
    if not isinstance(details, dict):
        raise WebValidationError("invalid_reasoning_usage")
    if details.get("reasoning_tokens") is not None:
        _integer(details["reasoning_tokens"], 0, outputs, "invalid_reasoning_usage")
    cost = usage.get("cost")
    actual = _money(str(cost) if type(cost) is int else cost)
    if usage.get("is_byok", False) is not False:
        raise WebValidationError("unapproved_byok_usage")
    citations = validate_citations(message, search_requests=searches, limits=limits)
    with localcontext() as ctx:
        ctx.prec = 60
        search_ceiling = searches * prices.supplier_search_usd_per_call
        supplier_ceiling = (
            inputs * prices.supplier_input_usd_per_million
            + outputs * prices.supplier_output_usd_per_million
        ) / 1_000_000
        supplier_ceiling += search_ceiling
        if actual > supplier_ceiling or actual > prices.supplier_cap_usd:
            raise WebValidationError("supplier_charge_exceeds_review")
        retail = (
            inputs * prices.retail_input_v_per_million
            + outputs * prices.retail_output_v_per_million
        ) / 1_000_000
        search_retail = searches * prices.retail_search_v_per_call
        retail = _ceil(retail + search_retail)
        if retail > prices.retail_cap_v or retail > budget.retail_reservation_v:
            raise WebValidationError("retail_charge_exceeds_review")
    return ObservedReceipt(
        inputs, outputs, searches, actual, search_ceiling, search_retail, retail, citations
    )


@dataclass(frozen=True)
class WebAccountingSnapshot:
    """Server-owned per-request state retained through reservation/reconciliation.

    Parent must bind this snapshot to its authoritative request ID/hash, exact
    reviewed model/endpoint and catalog revision. Persist these values, not a
    mutable request or a reference to the live catalog. Never load prices anew
    on receipt/replay. No snapshot implies no web-search reservation/dispatch.
    These helpers do not authorize settlement while preflight is blocked.
    """

    context_window_tokens: int
    max_tokens: int
    prices: ReviewedPrices
    limits: WebLimits = DEFAULT_LIMITS
    execution: ReviewedExecution | None = None

    def __post_init__(self):
        if not isinstance(self.prices, ReviewedPrices) or not isinstance(self.limits, WebLimits):
            raise WebValidationError("invalid_accounting_snapshot")
        if self.execution is not None and not isinstance(self.execution, ReviewedExecution):
            raise WebValidationError("invalid_execution_review")
        c = _integer(self.context_window_tokens, 1, MAX_TOKENS, "invalid_context_limit")
        _integer(self.max_tokens, 1, c, "invalid_output_limit")

    def preflight(self, *, now: datetime | None = None) -> PreflightResult:
        return preflight(
            context_window_tokens=self.context_window_tokens,
            max_tokens=self.max_tokens,
            prices=self.prices,
            execution=self.execution,
            now=now,
        )

    def prepare(self, *, now: datetime | None = None) -> PreparedWebSearch:
        now = _clock(now)
        budget = self.preflight(now=now).require_ready()
        return PreparedWebSearch(self, now, budget)

    def parse_receipt(self, usage: object, message: object) -> ObservedReceipt:
        return parse_receipt(
            usage,
            message,
            context_window_tokens=self.context_window_tokens,
            max_tokens=self.max_tokens,
            prices=self.prices,
            limits=self.limits,
        )


@dataclass(frozen=True)
class PreparedWebSearch:
    """Retain this trusted object with the request; never reconstruct from client data.

    The review is evaluated at dispatch, not again against a changed catalog on
    settlement. These helpers do not execute paid calls or perform wallet writes.
    The parent still validates completion/model/provider identity and idempotency.
    """

    snapshot: WebAccountingSnapshot
    prepared_at: datetime
    budget: CandidateBudget

    def __post_init__(self):
        expected = self.snapshot.preflight(now=self.prepared_at).require_ready()
        if self.budget != expected:
            raise WebValidationError("prepared_budget_mismatch")

    @property
    def loop_spend_threshold_usd(self) -> Decimal:
        # A post-step threshold cannot replace the first-pass overshoot reserve.
        # One-step termination independently bounds the whole request to 2*T+S.
        with localcontext() as ctx:
            ctx.prec = 60
            return (
                self.snapshot.prices.supplier_cap_usd
                - self.budget.final_pass_supplier_usd
                - self.budget.supplier_search_usd
            )

    def payload(self, messages: list[dict]) -> dict:
        """Exact bounded payload; no extension kwargs, presets or injected tools."""
        if not isinstance(messages, list) or not 1 <= len(messages) <= 200:
            raise WebValidationError("invalid_web_messages")
        copied = []
        for msg in messages:
            if (
                not isinstance(msg, dict)
                or set(msg) != {"role", "content"}
                or msg["role"] not in ("system", "user", "assistant")
                or not isinstance(msg["content"], str)
            ):
                raise WebValidationError("web_requires_text_only_messages")
            copied.append(dict(msg))
        s = self.snapshot
        fields = candidate_request_fields(s.limits)
        del fields["max_tool_calls"]  # stop_server_tools_when overrides this field.
        fields.update(
            {
                "model": s.execution.model,
                "messages": copied,
                "max_tokens": s.max_tokens,
                "stop_server_tools_when": [
                    {"type": "step_count_is", "step_count": 1},
                    {
                        "type": "max_cost",
                        "max_cost_in_dollars": _wire_price(self.loop_spend_threshold_usd),
                    },
                ],
                "provider": {
                    "only": [s.execution.provider_slug],
                    "allow_fallbacks": False,
                    "require_parameters": True,
                    "data_collection": "deny",
                    "zdr": True,
                    "max_price": {
                        "prompt": _wire_price(s.prices.supplier_input_usd_per_million),
                        "completion": _wire_price(s.prices.supplier_output_usd_per_million),
                        "request": 0,
                    },
                },
            }
        )
        try:
            if len(json.dumps(fields, ensure_ascii=False).encode("utf-8")) > MAX_TEXT_BYTES:
                raise WebValidationError("web_request_too_large")
        except UnicodeError:
            raise WebValidationError("invalid_web_message_text") from None
        return fields

    def parse_receipt(self, usage: object, message: object) -> ObservedReceipt:
        receipt = self.snapshot.parse_receipt(usage, message)
        if message.get("tool_calls") not in (None, []):
            raise WebValidationError("unexecuted_tool_calls")
        return replace(receipt, settlement_authorized=True)


def _review_datetime(value: object) -> datetime:
    try:
        result = datetime.fromisoformat(value)
        _expiry(result)
        return result
    except (TypeError, ValueError):
        raise WebValidationError("invalid_review_datetime") from None


def _decode_snapshot(data: object) -> WebAccountingSnapshot:
    try:
        if not isinstance(data, dict) or set(data) != {
            "context_window_tokens",
            "max_tokens",
            "prices",
            "execution",
            "limits",
        }:
            raise WebValidationError("invalid_stored_snapshot")
        prices = dict(data["prices"])
        execution = dict(data["execution"])
        prices["expires_at"] = _review_datetime(prices["expires_at"])
        execution["expires_at"] = _review_datetime(execution["expires_at"])
        return WebAccountingSnapshot(
            data["context_window_tokens"],
            data["max_tokens"],
            ReviewedPrices(**prices),
            WebLimits(**data["limits"]),
            ReviewedExecution(**execution),
        )
    except (KeyError, TypeError, ValueError):
        raise WebValidationError("invalid_stored_snapshot") from None


def prepare_server_review(
    model: str,
    max_tokens: int,
    review: object,
    model_config: dict | None = None,
    *,
    now: datetime | None = None,
) -> PreparedWebSearch:
    """Consume ONLY fresh server-owned catalog review JSON, never request JSON.

    The root review's web_search object has schema_version=1, prices, execution,
    and optional limits. Nested fields mirror the dataclasses; prices are decimal
    strings and expiries are ISO-8601. No retail price or attestation is inferred.
    """
    now = _clock(now)
    try:
        if not isinstance(review, dict):
            raise WebValidationError("missing_web_review")
        reviewed = _review_datetime(review["reviewed_at"])
        expires = _review_datetime(review["expires_at"])
        if not reviewed <= now < expires or expires - reviewed > timedelta(days=30):
            raise WebValidationError("stale_web_review")
        web = review["web_search"]
        if (
            not isinstance(web, dict)
            or type(web.get("schema_version")) is not int
            or web["schema_version"] != 1
            or set(web) - {"schema_version", "prices", "execution", "limits"}
        ):
            raise WebValidationError("invalid_web_review_schema")
        snapshot = _decode_snapshot(
            {
                "context_window_tokens": review["context_window_tokens"],
                "max_tokens": max_tokens,
                "prices": web["prices"],
                "execution": web["execution"],
                "limits": web.get("limits", {}),
            }
        )
        execution, prices = snapshot.execution, snapshot.prices
        if (
            review.get("account_access") is not True
            or review.get("completion_chat") is not True
            or execution.model != model
            or execution.expires_at > expires
            or prices.expires_at > expires
            or prices.retail_search_v_per_call != prices.retail_search_v_per_call.quantize(SCALE)
        ):
            raise WebValidationError("web_review_scope_mismatch")
        # Supplier rates must agree with the root review even during discovery.
        for key, value in (
            ("cost_input_per_1m", prices.supplier_input_usd_per_million),
            ("cost_output_per_1m", prices.supplier_output_usd_per_million),
        ):
            if _money(str(review[key])) != value:
                raise WebValidationError("web_review_rate_mismatch")
        if model_config is not None:
            if (
                model_config.get("provider") != "openrouter"
                or model_config.get("model_id") != model
                or model_config.get("enabled") is not True
                or type(model_config.get("max_tokens")) is not int
                or max_tokens > model_config["max_tokens"]
            ):
                raise WebValidationError("web_catalog_scope_mismatch")
            for key, value in (
                ("cost_input_per_1m", prices.supplier_input_usd_per_million),
                ("cost_output_per_1m", prices.supplier_output_usd_per_million),
                ("v_price_input_per_1m", prices.retail_input_v_per_million),
                ("v_price_output_per_1m", prices.retail_output_v_per_million),
            ):
                if _money(str(model_config[key])) != value:
                    raise WebValidationError("web_catalog_rate_mismatch")
        return snapshot.prepare(now=now)
    except (KeyError, TypeError, InvalidOperation):
        raise WebValidationError("missing_or_invalid_web_review") from None


def reviewed_web_capability(model: str, review: object) -> bool:
    """Discovery gate only; each request still preflights its actual output cap."""
    try:
        prepare_server_review(model, 16, review)
        return True
    except (WebValidationError, ValueError, TypeError):
        return False


def request_evidence(
    prepared: PreparedWebSearch, *, request_hash: str, enable_tools: bool = False
) -> dict:
    """Persist before dispatch in the authoritative request row, without prompts."""
    if not isinstance(request_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", request_hash):
        raise WebValidationError("invalid_request_hash")
    if type(enable_tools) is not bool:
        raise WebValidationError("invalid_local_tools_flag")
    snapshot = json.loads(
        json.dumps(
            asdict(prepared.snapshot),
            default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v),
        )
    )
    return {
        "version": 1,
        "request_hash": request_hash,
        "prepared_at": prepared.prepared_at.isoformat(),
        "snapshot": snapshot,
        "reserved_v": str(prepared.budget.retail_reservation_v),
        "enable_tools": enable_tools,
    }


def settlement_evidence(
    prepared: PreparedWebSearch,
    receipt: ObservedReceipt,
    *,
    request_hash: str,
    provider_request_id: str,
    completion_status: str,
    grant_v: Decimal,
    tool_calls: list[dict] | None = None,
    enable_tools: bool = False,
) -> dict:
    """Server-generated immutable-price evidence stored in existing result JSON.

    This is not a client API or a signature. Trust comes from the server-owned DB
    row, its request hash, transactional settlement and matching ledger evidence.
    """
    if not receipt.settlement_authorized:
        raise WebValidationError("unprepared_settlement")
    evidence = request_evidence(prepared, request_hash=request_hash, enable_tools=enable_tools)
    annotations = []
    for citation in receipt.citations:
        item = {key: value for key, value in asdict(citation).items() if value is not None}
        annotations.append({"type": "url_citation", "url_citation": item})
    return {
        **evidence,
        "provider_request_id": provider_request_id,
        "completion_status": completion_status,
        "usage": {
            "prompt_tokens": receipt.input_tokens,
            "completion_tokens": receipt.output_tokens,
            "cost": str(receipt.actual_cost_usd),
            "server_tool_use": {"web_search_requests": receipt.web_search_requests},
        },
        "annotations": annotations,
        "grant_v": str(grant_v),
        "local_tool_calls": json.loads(json.dumps(tool_calls or [])),
    }


def validate_stored_web_result(
    body: dict,
    *,
    request_hash: str | None = None,
    model: str | None = None,
    reserved_v: Decimal | None = None,
) -> ObservedReceipt:
    """Revalidate server-stored settled evidence, without current prices or I/O."""
    try:
        evidence = body["managed_web_accounting"]
        if (
            not isinstance(evidence, dict)
            or type(evidence.get("version")) is not int
            or evidence["version"] != 1
            or not re.fullmatch(r"[0-9a-f]{64}", evidence["request_hash"])
            or (request_hash is not None and evidence["request_hash"] != request_hash)
        ):
            raise WebValidationError("stored_web_request_mismatch")
        snapshot = _decode_snapshot(evidence["snapshot"])
        prepared = snapshot.prepare(now=_review_datetime(evidence["prepared_at"]))
        receipt = prepared.parse_receipt(
            evidence["usage"],
            {
                "role": "assistant",
                "content": body["text"],
                "annotations": evidence["annotations"],
            },
        )
        grant = _money(evidence["grant_v"])
        fee = receipt.web_search_cost_v
        budget = prepared.budget.retail_reservation_v
        charge = _money(body["v_cost"])
        calls = evidence.get("local_tool_calls", [])
        if (
            type(evidence.get("enable_tools", False)) is not bool
            or calls
            and evidence.get("enable_tools") is not True
            or not isinstance(calls, list)
            or len(calls) > 16
            or body["tool_calls"] != calls
            or len(json.dumps(calls).encode("utf-8")) > 600_000
            or calls
            and body["completion_status"] != "incomplete"
        ):
            raise WebValidationError("stored_local_calls_mismatch")
        identities = set()
        for call in calls:
            if (
                not isinstance(call, dict)
                or set(call)
                != {"tool_name", "arguments", "shell_mode", "timeout_sec", "provider_call_id"}
                or call["tool_name"]
                not in {"Read", "Search", "Write", "FindAndReplace", "InsertAtEnd", "Bash", "List"}
                or not isinstance(call["arguments"], dict)
                or call["shell_mode"] not in {"read_only", "mutating"}
                or type(call["timeout_sec"]) is not int
                or not 1 <= call["timeout_sec"] <= 300
                or not isinstance(call["provider_call_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", call["provider_call_id"])
                or call["provider_call_id"].lower().startswith("sk-")
                or call["provider_call_id"] in identities
            ):
                raise WebValidationError("invalid_stored_local_call")
            identities.add(call["provider_call_id"])
        if (
            model is not None
            and snapshot.execution.model != model
            or _money(evidence["reserved_v"]) != budget
            or reserved_v is not None
            and reserved_v != budget
            or not 0 <= grant <= receipt.retail_charge_candidate_v - fee
            or charge != receipt.retail_charge_candidate_v - grant
            or charge > budget
            or body["input_tokens"] != receipt.input_tokens
            or type(body["input_tokens"]) is not int
            or body["output_tokens"] != receipt.output_tokens
            or type(body["output_tokens"]) is not int
            or type(body["web_search_requests"]) is not int
            or body["web_search_requests"] != receipt.web_search_requests
            or body["web_search_used"] is not receipt.web_search_used
            or body["web_search_sources"] != list(receipt.sources)
            or _money(body["web_search_cost_v"]) != fee
            or _money(body["actual_cost_usd"]) != receipt.actual_cost_usd.quantize(SCALE)
            or body["provider_request_id"] != evidence["provider_request_id"]
            or body["completion_status"] != evidence["completion_status"]
            or body["completion_status"] not in {"complete", "incomplete"}
            or body["web_search_retry_without_tool"] is not False
        ):
            raise WebValidationError("stored_web_settlement_mismatch")
        return receipt
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise WebValidationError("invalid_stored_web_result") from None
