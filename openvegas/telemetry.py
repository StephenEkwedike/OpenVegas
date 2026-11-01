"""Lightweight in-process telemetry helpers.

This keeps metric emission dependency-free while giving tests and local runs a
deterministic counter surface.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
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
_HTTP_REQUESTS: list[dict[str, object]] = []
_HTTP_REQUESTS_MAX = 5000
_ALERT_ACKED: set[str] = set()
_ALERT_SILENCED_UNTIL: dict[str, float] = {}
_ALERT_SILENCE_REASON: dict[str, str] = {}
_ALERT_AUDIT: list[dict[str, object]] = []
_ALERT_AUDIT_MAX = 2000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_alert_audit(*, action: str, metric: str, detail: str = "") -> None:
    row = {
        "ts": _utc_now_iso(),
        "action": str(action or ""),
        "metric": str(metric or ""),
        "detail": str(detail or ""),
    }
    with _LOCK:
        _ALERT_AUDIT.append(row)
        if len(_ALERT_AUDIT) > _ALERT_AUDIT_MAX:
            del _ALERT_AUDIT[0 : len(_ALERT_AUDIT) - _ALERT_AUDIT_MAX]


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


def record_http_request(*, method: str, route: str, status_code: int, latency_ms: float) -> None:
    """Record bounded per-request telemetry for ops latency/error alerts."""
    status = int(status_code)
    route_token = str(route or "unknown")
    method_token = str(method or "GET").upper()
    latency = max(0.0, float(latency_ms or 0.0))
    status_class = f"{status // 100}xx" if 100 <= status <= 599 else "other"
    emit_metric("http_request_total", {"method": method_token, "route": route_token, "status_class": status_class})
    if status >= 500:
        emit_metric("http_5xx_total", {"method": method_token, "route": route_token})
    with _LOCK:
        _HTTP_REQUESTS.append(
            {
                "method": method_token,
                "route": route_token,
                "status_code": status,
                "latency_ms": latency,
            }
        )
        if len(_HTTP_REQUESTS) > _HTTP_REQUESTS_MAX:
            del _HTTP_REQUESTS[0 : len(_HTTP_REQUESTS) - _HTTP_REQUESTS_MAX]


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
        _RUN_METRICS.append(
            {
                "run_id": str(run_id or ""),
                "recorded_at": _utc_now_iso(),
                **{k: data[k] for k in required},
            }
        )
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

    retry_by_status: dict[str, int] = defaultdict(int)
    finalize_reason_dist: dict[str, int] = defaultdict(int)
    same_intent_fail_total = 0
    topup_suggest_suppressed: dict[str, int] = defaultdict(int)
    topup_transitions: dict[str, int] = defaultdict(int)
    topup_checkout_created: dict[str, int] = defaultdict(int)

    for metric_key, count in snapshot.items():
        name, tags = _parse_metric_key(metric_key)
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


def _parse_metric_key(metric_key: str) -> tuple[str, dict[str, str]]:
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


def _sum_counter(
    snapshot: dict[str, int],
    metric_name: str,
    *,
    match_tags: dict[str, str] | None = None,
) -> int:
    total = 0
    required = {str(k): str(v) for k, v in (match_tags or {}).items()}
    for key, count in snapshot.items():
        name, tags = _parse_metric_key(key)
        if name != metric_name:
            continue
        if required and any(str(tags.get(k, "")) != v for k, v in required.items()):
            continue
        total += int(count)
    return total


def get_http_request_summary() -> dict[str, float]:
    with _LOCK:
        rows = list(_HTTP_REQUESTS)
    if not rows:
        return {
            "http_request_count": 0.0,
            "http_5xx_count": 0.0,
            "http_5xx_rate": 0.0,
            "http_latency_ms_p95": 0.0,
        }
    latencies = [max(0.0, float(row.get("latency_ms", 0.0) or 0.0)) for row in rows]
    total = len(rows)
    failures = sum(1 for row in rows if int(row.get("status_code", 0) or 0) >= 500)
    return {
        "http_request_count": float(total),
        "http_5xx_count": float(failures),
        "http_5xx_rate": float(failures) / float(max(1, total)),
        "http_latency_ms_p95": _percentile(latencies, 95),
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


def get_run_metrics_trend(*, limit: int = 100) -> list[dict[str, object]]:
    """Return bounded run trend rows (newest-first) for dashboards."""
    max_limit = max(1, min(500, int(limit)))
    with _LOCK:
        recent = list(_RUN_METRICS[-max_limit:])
    recent.reverse()
    trend: list[dict[str, object]] = []
    for row in recent:
        trend.append(
            {
                "run_id": str(row.get("run_id", "")),
                "recorded_at": str(row.get("recorded_at", "")),
                "provider": str(row.get("provider", "")),
                "model": str(row.get("model", "")),
                "turn_latency_ms": float(row.get("turn_latency_ms", 0.0) or 0.0),
                "tool_failures": int(row.get("tool_failures", 0) or 0),
                "fallbacks": int(row.get("fallbacks", 0) or 0),
                "cost_usd": float(row.get("cost_usd", 0.0) or 0.0),
            }
        )
    return trend


def get_run_metric_by_id(run_id: str) -> dict[str, object] | None:
    token = str(run_id or "").strip()
    if not token:
        return None
    with _LOCK:
        for row in reversed(_RUN_METRICS):
            if str(row.get("run_id", "")) == token:
                return dict(row)
    return None


def ack_alert(metric: str) -> dict[str, object]:
    token = str(metric or "").strip()
    if not token:
        raise ValueError("metric_required")
    with _LOCK:
        _ALERT_ACKED.add(token)
    _append_alert_audit(action="ack", metric=token, detail="manual_ack")
    emit_metric("ops_alert_ack_total", {"metric": token})
    return {"metric": token, "acked": True}


def silence_alert(metric: str, *, duration_sec: int, reason: str = "") -> dict[str, object]:
    import time

    token = str(metric or "").strip()
    if not token:
        raise ValueError("metric_required")
    ttl = max(30, min(7 * 24 * 3600, int(duration_sec)))
    until_epoch = time.time() + float(ttl)
    with _LOCK:
        _ALERT_SILENCED_UNTIL[token] = float(until_epoch)
        _ALERT_SILENCE_REASON[token] = str(reason or "").strip()
    _append_alert_audit(action="silence", metric=token, detail=f"duration_sec={ttl};reason={str(reason or '').strip()}")
    emit_metric("ops_alert_silence_total", {"metric": token})
    return {
        "metric": token,
        "silenced": True,
        "duration_sec": ttl,
        "silenced_until_epoch": until_epoch,
        "reason": str(reason or "").strip(),
    }


def get_alert_workflow_state() -> dict[str, object]:
    import time

    now = time.time()
    with _LOCK:
        # Drop expired silence entries lazily.
        expired = [k for k, until in _ALERT_SILENCED_UNTIL.items() if float(until) <= now]
        for key in expired:
            _ALERT_SILENCED_UNTIL.pop(key, None)
            _ALERT_SILENCE_REASON.pop(key, None)
        silenced = {
            k: {
                "silenced_until_epoch": float(v),
                "reason": str(_ALERT_SILENCE_REASON.get(k, "")),
            }
            for k, v in _ALERT_SILENCED_UNTIL.items()
        }
        acked = sorted(_ALERT_ACKED)
    return {"acked": acked, "silenced": silenced}


def get_alert_audit(*, limit: int = 100) -> list[dict[str, object]]:
    max_limit = max(1, min(500, int(limit)))
    with _LOCK:
        rows = list(_ALERT_AUDIT[-max_limit:])
    rows.reverse()
    return rows


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
        "http_5xx_rate": _env_float("OPENVEGAS_ALERT_HTTP_5XX_RATE", 0.02, min_value=0.0, max_value=1.0),
        "http_latency_ms_p95": _env_float("OPENVEGAS_ALERT_HTTP_P95_LATENCY_MS", 2000.0, min_value=50.0, max_value=120000.0),
        "upload_failure_rate": _env_float("OPENVEGAS_ALERT_UPLOAD_FAILURE_RATE", 0.10, min_value=0.0, max_value=1.0),
        "auth_refresh_failure_rate": _env_float("OPENVEGAS_ALERT_AUTH_REFRESH_FAILURE_RATE", 0.25, min_value=0.0, max_value=1.0),
        "payment_failure_rate": _env_float("OPENVEGAS_ALERT_PAYMENT_FAILURE_RATE", 0.10, min_value=0.0, max_value=1.0),
    }


def get_ops_alerts() -> dict[str, object]:
    summary = get_run_metrics_summary()
    snapshot = get_metrics_snapshot()
    http_summary = get_http_request_summary()
    thresholds = get_alert_thresholds()
    alerts: list[dict[str, object]] = []
    workflow = get_alert_workflow_state()
    acked_metrics = set(str(x) for x in (workflow.get("acked") or []))
    silenced_map = dict(workflow.get("silenced") or {})

    upload_success = _sum_counter(snapshot, "file_upload_request_total", match_tags={"outcome": "success"})
    upload_failure = _sum_counter(snapshot, "file_upload_request_total", match_tags={"outcome": "failure"})
    upload_total = upload_success + upload_failure

    auth_refresh_success = _sum_counter(snapshot, "auth_refresh_attempt_total", match_tags={"outcome": "success"})
    auth_refresh_failure = _sum_counter(snapshot, "auth_refresh_attempt_total", match_tags={"outcome": "failure"})
    auth_refresh_total = auth_refresh_success + auth_refresh_failure

    payment_success = _sum_counter(snapshot, "topup_saved_card_charge_total", match_tags={"status": "success"}) + _sum_counter(
        snapshot, "topup_checkout_session_total", match_tags={"status": "success"}
    )
    payment_failure = _sum_counter(snapshot, "topup_saved_card_charge_total", match_tags={"status": "failure"}) + _sum_counter(
        snapshot, "topup_checkout_session_total", match_tags={"status": "failure"}
    )
    payment_total = payment_success + payment_failure

    derived = {
        "http_5xx_rate": float(http_summary.get("http_5xx_rate", 0.0) or 0.0),
        "http_latency_ms_p95": float(http_summary.get("http_latency_ms_p95", 0.0) or 0.0),
        "upload_failure_rate": float(upload_failure) / float(max(1, upload_total)),
        "auth_refresh_failure_rate": float(auth_refresh_failure) / float(max(1, auth_refresh_total)),
        "payment_failure_rate": float(payment_failure) / float(max(1, payment_total)),
    }

    def _check(metric: str, severity: str = "warning") -> None:
        observed = float(derived.get(metric, summary.get(metric, 0.0)) or 0.0)
        threshold = float(thresholds.get(metric, 0.0))
        fired = bool(observed > threshold)
        if fired:
            is_silenced = metric in silenced_map
            is_acked = metric in acked_metrics
            alerts.append(
                {
                    "metric": metric,
                    "severity": severity,
                    "observed": observed,
                    "threshold": threshold,
                    "status": "silenced" if is_silenced else "fired",
                    "acked": is_acked,
                    "silenced_until_epoch": (
                        float((silenced_map.get(metric) or {}).get("silenced_until_epoch", 0.0))
                        if is_silenced
                        else None
                    ),
                    "silence_reason": str((silenced_map.get(metric) or {}).get("reason", "")) if is_silenced else "",
                }
            )

    _check("turn_latency_ms_p95", severity="critical")
    _check("tool_fail_rate", severity="warning")
    _check("fallback_rate", severity="warning")
    _check("avg_cost_usd", severity="warning")
    _check("http_5xx_rate", severity="critical")
    _check("http_latency_ms_p95", severity="warning")
    _check("upload_failure_rate", severity="warning")
    _check("auth_refresh_failure_rate", severity="warning")
    _check("payment_failure_rate", severity="critical")

    return {
        "alerts": alerts,
        "thresholds": thresholds,
        "run_summary": summary,
        "http_summary": http_summary,
        "derived": derived,
        "workflow": workflow,
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
        _HTTP_REQUESTS.clear()
        _ALERT_ACKED.clear()
        _ALERT_SILENCED_UNTIL.clear()
        _ALERT_SILENCE_REASON.clear()
        _ALERT_AUDIT.clear()


def _reset_emit_once_cache_for_tests() -> None:
    """Test-only helper to keep emit-once assertions order independent."""
    with _LOCK:
        _EMITTED_ONCE_KEYS.clear()
