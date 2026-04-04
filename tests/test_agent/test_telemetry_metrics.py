from __future__ import annotations

from pathlib import Path

import pytest

from openvegas.agent.tool_cas import claim_started_tx
from openvegas.cli import _preprocess_tool_request_for_runtime
from openvegas.telemetry import (
    emit_run_metrics,
    get_dashboard_slices,
    get_metrics_snapshot,
    get_run_metrics_summary,
    reset_metrics,
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
