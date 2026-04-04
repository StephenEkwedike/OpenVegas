"""Ops diagnostics and telemetry visibility routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from openvegas.telemetry import (
    get_alert_thresholds,
    get_dashboard_slices,
    get_metrics_snapshot,
    get_ops_alerts,
    get_recent_run_metrics,
    get_rollback_plan,
    get_run_metrics_summary,
)
from server.middleware.auth import get_current_user

router = APIRouter()


@router.get("/ops/diagnostics")
async def ops_diagnostics(_: dict = Depends(get_current_user)):
    return {
        "metrics": get_metrics_snapshot(),
        "dashboard": get_dashboard_slices(),
        "run_summary": get_run_metrics_summary(),
        "recent_runs": get_recent_run_metrics(limit=25),
        "alerts": get_ops_alerts().get("alerts", []),
        "thresholds": get_alert_thresholds(),
        "rollback": get_rollback_plan(),
    }


@router.get("/ops/alerts")
async def ops_alerts(_: dict = Depends(get_current_user)):
    payload = get_ops_alerts()
    payload["rollback"] = get_rollback_plan()
    return payload


@router.get("/ops/runs")
async def ops_runs(limit: int = 25, _: dict = Depends(get_current_user)):
    return {"runs": get_recent_run_metrics(limit=limit)}
