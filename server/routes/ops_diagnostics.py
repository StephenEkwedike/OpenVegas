"""Ops diagnostics and telemetry visibility routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from openvegas.telemetry import (
    ack_alert,
    get_alert_audit,
    get_alert_thresholds,
    get_alert_workflow_state,
    get_dashboard_slices,
    get_metrics_snapshot,
    get_ops_alerts,
    get_recent_run_metrics,
    get_run_metric_by_id,
    get_run_metrics_trend,
    get_rollback_plan,
    get_run_metrics_summary,
    silence_alert,
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
        "alert_audit": get_alert_audit(limit=50),
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


@router.get("/ops/runs/{run_id}")
async def ops_run_detail(run_id: str, _: dict = Depends(get_current_user)):
    row = get_run_metric_by_id(run_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "not_found", "detail": "run_id not found"})
    return {"run": row}


@router.get("/ops/trends")
async def ops_trends(limit: int = 120, _: dict = Depends(get_current_user)):
    return {"trend": get_run_metrics_trend(limit=limit)}


@router.get("/ops/alerts/state")
async def ops_alert_state(_: dict = Depends(get_current_user)):
    return get_alert_workflow_state()


@router.get("/ops/alerts/audit")
async def ops_alert_audit(limit: int = 100, _: dict = Depends(get_current_user)):
    return {"audit": get_alert_audit(limit=limit)}


class OpsAlertAckRequest(BaseModel):
    metric: str = Field(min_length=1, max_length=120)
    model_config = ConfigDict(extra="forbid")


class OpsAlertSilenceRequest(BaseModel):
    metric: str = Field(min_length=1, max_length=120)
    duration_sec: int = Field(default=900, ge=30, le=604800)
    reason: str = Field(default="", max_length=240)
    model_config = ConfigDict(extra="forbid")


@router.post("/ops/alerts/ack")
async def ops_alert_ack(req: OpsAlertAckRequest, _: dict = Depends(get_current_user)):
    try:
        return ack_alert(req.metric)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc), "detail": str(exc)})


@router.post("/ops/alerts/silence")
async def ops_alert_silence(req: OpsAlertSilenceRequest, _: dict = Depends(get_current_user)):
    try:
        return silence_alert(req.metric, duration_sec=req.duration_sec, reason=req.reason)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc), "detail": str(exc)})
