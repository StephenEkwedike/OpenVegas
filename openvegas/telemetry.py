"""Lightweight in-process telemetry helpers.

This keeps metric emission dependency-free while giving tests and local runs a
deterministic counter surface.
"""

from __future__ import annotations

from collections import defaultdict
import json
import os
from statistics import median
from threading import Lock
from typing import DefaultDict


_LOCK = Lock()
_COUNTERS: DefaultDict[str, int] = defaultdict(int)
_EMITTED_ONCE_KEYS: set[str] = set()
_RUN_METRICS: list[dict[str, object]] = []
_RUN_METRICS_MAX = 2000


def _key(name: str, tags: dict[str, object] | None = None) -> str:
    if not tags:
        return name
    tag_text = ",".join(f"{k}={tags[k]}" for k in sorted(tags))
    return f"{name}|{tag_text}"


def emit_metric(name: str, tags: dict[str, object] | None = None, value: int = 1) -> None:
    """Increment a counter-style metric."""
    metric_key = _key(str(name), tags)
    with _LOCK:
        _COUNTERS[metric_key] += int(value)


def emit_run_metrics(run_id: str, data: dict[str, object]) -> None:
    """Emit a canonical run metrics event with required fields."""
    required = [
        "provider",
        "model",
        "turn_latency_ms",
        "input_tokens",
        "output_tokens",
        "tool_calls",
        "tool_failures",
        "fallbacks",
        "cost_usd",
    ]
    for key in required:
        if key not in data:
            raise ValueError(f"missing metric {key}")
    tags = {"run_id": str(run_id or "")}
    tags.update({k: data[k] for k in required})
    emit_metric("inference.run.metrics", tags)
    with _LOCK:
        _RUN_METRICS.append({"run_id": str(run_id or ""), **{k: data[k] for k in required}})
        if len(_RUN_METRICS) > _RUN_METRICS_MAX:
            del _RUN_METRICS[0 : len(_RUN_METRICS) - _RUN_METRICS_MAX]


def emit_once_process(name: str, tags: dict[str, object] | None = None, value: int = 1) -> None:
    """Emit a metric once per-process for a stable name+tag key."""
    metric_key = _key(str(name), tags)
    with _LOCK:
        if metric_key in _EMITTED_ONCE_KEYS:
            return
        _EMITTED_ONCE_KEYS.add(metric_key)
        _COUNTERS[metric_key] += int(value)


def get_metrics_snapshot() -> dict[str, int]:
    with _LOCK:
        return dict(_COUNTERS)


def get_dashboard_slices() -> dict[str, object]:
    """Return pre-aggregated slices for runtime reliability dashboards."""
    with _LOCK:
        snapshot = dict(_COUNTERS)

    def _parse_tags(metric_key: str) -> tuple[str, dict[str, str]]:
        if "|" not in metric_key:
            return metric_key, {}
        name, raw_tags = metric_key.split("|", 1)
        tags: dict[str, str] = {}
        for token in raw_tags.split(","):
            if "=" not in token:
                continue
            k, v = token.split("=", 1)
            tags[k] = v
        return name, tags

    retry_by_status: dict[str, int] = defaultdict(int)
    finalize_reason_dist: dict[str, int] = defaultdict(int)
    same_intent_fail_total = 0
    topup_suggest_suppressed: dict[str, int] = defaultdict(int)
    topup_transitions: dict[str, int] = defaultdict(int)
    topup_checkout_created: dict[str, int] = defaultdict(int)

    for metric_key, count in snapshot.items():
        name, tags = _parse_tags(metric_key)
        if name == "tool_apply_patch_same_intent_fail_total":
            same_intent_fail_total += int(count)
        elif name == "tool_apply_patch_retry_total":
            retry_by_status[str(tags.get("status", "unknown"))] += int(count)
        elif name == "tool_loop_finalize_reason":
            finalize_reason_dist[str(tags.get("reason", "unknown"))] += int(count)
        elif name == "topup_suggest_suppressed_total":
            topup_suggest_suppressed[str(tags.get("reason", "unknown"))] += int(count)
        elif name == "topup_status_transition_total":
            edge = f"{tags.get('from', 'unknown')}->{tags.get('to', 'unknown')}|{tags.get('mode', 'unknown')}"
            topup_transitions[edge] += int(count)
        elif name == "topup_checkout_created_total":
            topup_checkout_created[str(tags.get("mode", "unknown"))] += int(count)

    return {
        "tool_apply_patch_same_intent_fail_total": int(same_intent_fail_total),
        "tool_apply_patch_retry_total_by_status": dict(sorted(retry_by_status.items())),
        "tool_loop_finalize_reason_distribution": dict(sorted(finalize_reason_dist.items())),
        "topup_suggest_suppressed_total_by_reason": dict(sorted(topup_suggest_suppressed.items())),
        "topup_status_transition_total": dict(sorted(topup_transitions.items())),
        "topup_checkout_created_total_by_mode": dict(sorted(topup_checkout_created.items())),
    }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    idx = max(0, min(len(values) - 1, int(round((pct / 100.0) * (len(values) - 1)))))
    ordered = sorted(values)
    return float(ordered[idx])


def get_run_metrics_summary() -> dict[str, object]:
    with _LOCK:
        runs = list(_RUN_METRICS)

    if not runs:
        return {
            "run_count": 0,
            "turn_latency_ms_p50": 0.0,
            "turn_latency_ms_p95": 0.0,
            "tool_fail_rate": 0.0,
            "fallback_rate": 0.0,
            "avg_cost_usd": 0.0,
        }

    latencies: list[float] = []
    tool_failures = 0
    fallbacks = 0
    total_cost = 0.0
    for row in runs:
        try:
            latencies.append(float(row.get("turn_latency_ms", 0) or 0))
        except Exception:
            latencies.append(0.0)
        try:
            tool_failures += int(row.get("tool_failures", 0) or 0)
        except Exception:
            pass
        try:
            fallbacks += int(row.get("fallbacks", 0) or 0)
        except Exception:
            pass
        try:
            total_cost += float(row.get("cost_usd", 0) or 0)
        except Exception:
            pass

    run_count = max(1, len(runs))
    return {
        "run_count": len(runs),
        "turn_latency_ms_p50": _percentile(latencies, 50),
        "turn_latency_ms_p95": _percentile(latencies, 95),
        "turn_latency_ms_avg": float(sum(latencies) / len(latencies)) if latencies else 0.0,
        "turn_latency_ms_median": float(median(latencies)) if latencies else 0.0,
        "tool_fail_rate": float(tool_failures) / float(run_count),
        "fallback_rate": float(fallbacks) / float(run_count),
        "avg_cost_usd": float(total_cost) / float(run_count),
    }


def get_recent_run_metrics(*, limit: int = 25) -> list[dict[str, object]]:
    """Return most-recent run metrics (newest-first), bounded by limit."""
    max_limit = max(1, min(200, int(limit)))
    with _LOCK:
        recent = list(_RUN_METRICS[-max_limit:])
    recent.reverse()
    return recent


def _env_float(name: str, default: float, *, min_value: float = 0.0, max_value: float = 1_000_000.0) -> float:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        value = float(raw)
    except Exception:
        value = float(default)
    return max(min_value, min(max_value, value))


def get_alert_thresholds() -> dict[str, float]:
    return {
        "turn_latency_ms_p95": _env_float("OPENVEGAS_ALERT_P95_LATENCY_MS", 4000.0, min_value=100.0, max_value=120000.0),
        "tool_fail_rate": _env_float("OPENVEGAS_ALERT_TOOL_FAIL_RATE", 0.20, min_value=0.0, max_value=1.0),
        "fallback_rate": _env_float("OPENVEGAS_ALERT_FALLBACK_RATE", 0.25, min_value=0.0, max_value=1.0),
        "avg_cost_usd": _env_float("OPENVEGAS_ALERT_AVG_COST_USD", 3.0, min_value=0.0, max_value=1000.0),
    }


def get_ops_alerts() -> dict[str, object]:
    summary = get_run_metrics_summary()
    thresholds = get_alert_thresholds()
    alerts: list[dict[str, object]] = []

    def _check(metric: str, severity: str = "warning") -> None:
        observed = float(summary.get(metric, 0.0) or 0.0)
        threshold = float(thresholds.get(metric, 0.0))
        fired = bool(observed > threshold)
        if fired:
            alerts.append(
                {
                    "metric": metric,
                    "severity": severity,
                    "observed": observed,
                    "threshold": threshold,
                    "status": "fired",
                }
            )

    _check("turn_latency_ms_p95", severity="critical")
    _check("tool_fail_rate", severity="warning")
    _check("fallback_rate", severity="warning")
    _check("avg_cost_usd", severity="warning")

    return {
        "alerts": alerts,
        "thresholds": thresholds,
        "run_summary": summary,
    }


def get_rollback_plan() -> dict[str, object]:
    owner = str(os.getenv("OPENVEGAS_ROLLBACK_OWNER", "platform-oncall")).strip() or "platform-oncall"
    raw = str(os.getenv("OPENVEGAS_ROLLBACK_CHECKLIST_JSON", "")).strip()
    checklist: list[str]
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                checklist = [str(item).strip() for item in parsed if str(item).strip()]
            else:
                checklist = []
        except Exception:
            checklist = []
    else:
        checklist = []
    if not checklist:
        checklist = [
            "Disable impacted feature flag.",
            "Verify /health and /ops/diagnostics are stable.",
            "Confirm error-rate drop after rollback.",
            "Post incident update with mitigation timestamp.",
        ]
    return {"owner": owner, "checklist": checklist}


def reset_metrics() -> None:
    with _LOCK:
        _COUNTERS.clear()
        _EMITTED_ONCE_KEYS.clear()
        _RUN_METRICS.clear()


def _reset_emit_once_cache_for_tests() -> None:
    """Test-only helper to keep emit-once assertions order independent."""
    with _LOCK:
        _EMITTED_ONCE_KEYS.clear()
