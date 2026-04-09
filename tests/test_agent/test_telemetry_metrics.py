from __future__ import annotations

from pathlib import Path

import pytest

from openvegas.agent.tool_cas import claim_started_tx
from openvegas.cli import _preprocess_tool_request_for_runtime
from openvegas.telemetry import (
    ack_alert,
    emit_run_metrics,
    get_alert_audit,
    get_dashboard_slices,
    get_alert_workflow_state,
    get_metrics_snapshot,
    get_ops_alerts,
    get_http_request_summary,
    get_run_metric_by_id,
    get_run_metrics_trend,
    record_http_request,
    get_run_metrics_summary,
    reset_metrics,
    silence_alert,
)
from openvegas.telemetry import emit_metric


class _FakeTx:
    async def execute(self, *_args, **_kwargs):
        return "UPDATE 0"

    async def fetchrow(self, *_args, **_kwargs):
        return {"status": "started", "execution_token": "tok"}


def test_tool_alias_and_fallback_metrics_are_emitted():
    reset_metrics()
    _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "fs_create", "arguments": {}},
        user_message="apply a tiny patch to a temp file in this repo",
        model_text="",
        workspace_root=str(Path.cwd()),
        tool_observations=[],
    )
    _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "fs_list", "arguments": {"path": "."}},
        user_message="apply a tiny patch to a temp file in this repo",
        model_text="",
        workspace_root=str(Path.cwd()),
        tool_observations=[{"tool_name": "fs_list"}],
    )
    metrics = get_metrics_snapshot()
    assert any(k.startswith("tool_alias_rewrite_total|") for k in metrics)
    assert any(k.startswith("tool_fallback_promotion_total|") for k in metrics)


@pytest.mark.asyncio
async def test_tool_cas_conflict_metric_is_emitted_on_duplicate_start():
    reset_metrics()
    outcome = await claim_started_tx(_FakeTx(), run_id="r1", tool_call_id="t1", execution_token="tok")
    assert outcome == "idempotent"
    metrics = get_metrics_snapshot()
    assert any(k.startswith("tool_cas_conflict_total|endpoint=tool_start") for k in metrics)


def test_dashboard_slices_include_patch_retry_and_finalize_reason_distribution():
    reset_metrics()
    emit_metric("tool_apply_patch_same_intent_fail_total", {"count": "1"})
    emit_metric("tool_apply_patch_same_intent_fail_total", {"count": "2"})
    emit_metric("tool_apply_patch_retry_total", {"status": "succeeded"})
    emit_metric("tool_apply_patch_retry_total", {"status": "failed"})
    emit_metric("tool_apply_patch_retry_total", {"status": "failed"})
    emit_metric("tool_loop_finalize_reason", {"reason": "completed"})
    emit_metric("tool_loop_finalize_reason", {"reason": "patch_recovery_failed_same_intent_circuit_break"})
    emit_metric("tool_loop_finalize_reason", {"reason": "patch_recovery_failed_same_intent_circuit_break"})
    slices = get_dashboard_slices()
    assert slices["tool_apply_patch_same_intent_fail_total"] == 2
    assert slices["tool_apply_patch_retry_total_by_status"] == {"failed": 2, "succeeded": 1}
    assert slices["tool_loop_finalize_reason_distribution"] == {
        "completed": 1,
        "patch_recovery_failed_same_intent_circuit_break": 2,
    }


def test_dashboard_slices_include_topup_telemetry_groups():
    reset_metrics()
    emit_metric("topup_suggest_suppressed_total", {"reason": "already_pending_topup"})
    emit_metric("topup_suggest_suppressed_total", {"reason": "already_pending_topup"})
    emit_metric("topup_checkout_created_total", {"mode": "simulated"})
    emit_metric("topup_status_transition_total", {"from": "checkout_created", "to": "paid", "mode": "simulated"})
    slices = get_dashboard_slices()
    assert slices["topup_suggest_suppressed_total_by_reason"] == {"already_pending_topup": 2}
    assert slices["topup_checkout_created_total_by_mode"] == {"simulated": 1}
    assert slices["topup_status_transition_total"] == {"checkout_created->paid|simulated": 1}


def test_emit_run_metrics_requires_canonical_fields():
    reset_metrics()
    with pytest.raises(ValueError):
        emit_run_metrics("r-1", {"provider": "openai"})


def test_emit_run_metrics_emits_counter():
    reset_metrics()
    emit_run_metrics(
        "r-2",
        {
            "provider": "openai",
            "model": "gpt-5.4",
            "turn_latency_ms": 1200,
            "input_tokens": 10,
            "output_tokens": 20,
            "tool_calls": 1,
            "tool_failures": 0,
            "fallbacks": 0,
            "cost_usd": "0.0012",
        },
    )
    snap = get_metrics_snapshot()
    assert any(key.startswith("inference.run.metrics|") for key in snap)


def test_run_metrics_summary_exposes_percentiles_and_rates():
    reset_metrics()
    emit_run_metrics(
        "r-1",
        {
            "provider": "openai",
            "model": "gpt-5.4",
            "turn_latency_ms": 100,
            "input_tokens": 10,
            "output_tokens": 20,
            "tool_calls": 1,
            "tool_failures": 0,
            "fallbacks": 1,
            "cost_usd": 0.01,
        },
    )
    emit_run_metrics(
        "r-2",
        {
            "provider": "openai",
            "model": "gpt-5.4",
            "turn_latency_ms": 300,
            "input_tokens": 11,
            "output_tokens": 22,
            "tool_calls": 2,
            "tool_failures": 1,
            "fallbacks": 0,
            "cost_usd": 0.03,
        },
    )
    summary = get_run_metrics_summary()
    assert summary["run_count"] == 2
    assert summary["turn_latency_ms_p50"] >= 100
    assert summary["turn_latency_ms_p95"] >= summary["turn_latency_ms_p50"]
    assert summary["tool_fail_rate"] > 0
    assert summary["avg_cost_usd"] > 0


def test_ops_alerts_include_http_upload_auth_and_payment_rates(monkeypatch):
    reset_metrics()
    monkeypatch.setenv("OPENVEGAS_ALERT_HTTP_5XX_RATE", "0.01")
    monkeypatch.setenv("OPENVEGAS_ALERT_UPLOAD_FAILURE_RATE", "0.20")
    monkeypatch.setenv("OPENVEGAS_ALERT_AUTH_REFRESH_FAILURE_RATE", "0.20")
    monkeypatch.setenv("OPENVEGAS_ALERT_PAYMENT_FAILURE_RATE", "0.20")

    for _ in range(3):
        record_http_request(method="GET", route="/health", status_code=200, latency_ms=30.0)
    for _ in range(2):
        record_http_request(method="POST", route="/inference/ask", status_code=500, latency_ms=2500.0)

    emit_metric("file_upload_request_total", {"endpoint": "init", "outcome": "success"})
    emit_metric("file_upload_request_total", {"endpoint": "complete", "outcome": "failure", "reason": "upload_expired"})

    emit_metric("auth_refresh_attempt_total", {"surface": "browser", "trigger": "reactive", "outcome": "success"})
    emit_metric(
        "auth_refresh_attempt_total",
        {"surface": "browser", "trigger": "reactive", "outcome": "failure", "reason": "refresh_preflight_failed"},
    )

    emit_metric("topup_saved_card_charge_total", {"status": "success"})
    emit_metric("topup_saved_card_charge_total", {"status": "failure", "reason": "intent_create_failed"})

    payload = get_ops_alerts()
    alerts = payload.get("alerts", [])
    metrics = {str(item.get("metric")) for item in alerts}
    assert "http_5xx_rate" in metrics
    assert "upload_failure_rate" in metrics
    assert "auth_refresh_failure_rate" in metrics
    assert "payment_failure_rate" in metrics

    http_summary = get_http_request_summary()
    assert float(http_summary["http_5xx_rate"]) > 0.0


def test_ops_alert_workflow_ack_and_silence_state():
    reset_metrics()
    ack = ack_alert("turn_latency_ms_p95")
    assert ack["acked"] is True
    silenced = silence_alert("turn_latency_ms_p95", duration_sec=120, reason="maint")
    assert silenced["silenced"] is True
    state = get_alert_workflow_state()
    assert "turn_latency_ms_p95" in set(state.get("acked", []))
    assert "turn_latency_ms_p95" in dict(state.get("silenced", {}))
    audit_rows = get_alert_audit(limit=10)
    assert audit_rows
    assert {str(row.get("action")) for row in audit_rows} >= {"ack", "silence"}


def test_run_metric_trend_and_drilldown():
    reset_metrics()
    emit_run_metrics(
        "run-trend-1",
        {
            "provider": "openai",
            "model": "gpt-5",
            "turn_latency_ms": 88.0,
            "input_tokens": 10,
            "output_tokens": 15,
            "tool_calls": 1,
            "tool_failures": 0,
            "fallbacks": 0,
            "cost_usd": 0.02,
        },
    )
    trend = get_run_metrics_trend(limit=5)
    assert trend
    assert trend[0]["run_id"] == "run-trend-1"
    row = get_run_metric_by_id("run-trend-1")
    assert row is not None
    assert row["run_id"] == "run-trend-1"
