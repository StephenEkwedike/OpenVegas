from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from openvegas.telemetry import emit_metric, emit_run_metrics, reset_metrics
from server.middleware.auth import get_current_user
from server.routes import ops_diagnostics as ops_routes


def _app_with_router() -> FastAPI:
    app = FastAPI()
    app.include_router(ops_routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u-1"}
    return app


def test_ops_diagnostics_returns_metrics_and_summary():
    reset_metrics()
    emit_metric("topup_status_transition_total", {"from": "pending", "to": "paid", "mode": "hosted"})
    emit_run_metrics(
        "run-1",
        {
            "provider": "openai",
            "model": "gpt-5",
            "turn_latency_ms": 123.0,
            "input_tokens": 10,
            "output_tokens": 20,
            "tool_calls": 0,
            "tool_failures": 0,
            "fallbacks": 0,
            "cost_usd": 0.01,
        },
    )
    client = TestClient(_app_with_router())
    resp = client.get("/ops/diagnostics")
    assert resp.status_code == 200
    body = resp.json()
    assert "metrics" in body
    assert "dashboard" in body
    assert "run_summary" in body
    assert "thresholds" in body
    assert "rollback" in body
    assert body["run_summary"]["run_count"] >= 1

    alerts_resp = client.get("/ops/alerts")
    assert alerts_resp.status_code == 200
    alerts_body = alerts_resp.json()
    assert "thresholds" in alerts_body
    assert "alerts" in alerts_body
    assert "rollback" in alerts_body

    runs_resp = client.get("/ops/runs?limit=5")
    assert runs_resp.status_code == 200
    runs_body = runs_resp.json()
    assert "runs" in runs_body
    assert isinstance(runs_body["runs"], list)


def test_ops_alerts_fires_when_threshold_exceeded(monkeypatch):
    reset_metrics()
    monkeypatch.setenv("OPENVEGAS_ALERT_P95_LATENCY_MS", "100")
    emit_run_metrics(
        "run-2",
        {
            "provider": "openai",
            "model": "gpt-5",
            "turn_latency_ms": 500.0,
            "input_tokens": 1,
            "output_tokens": 1,
            "tool_calls": 0,
            "tool_failures": 0,
            "fallbacks": 0,
            "cost_usd": 0.01,
        },
    )
    client = TestClient(_app_with_router())
    resp = client.get("/ops/alerts")
    assert resp.status_code == 200
    alerts = resp.json().get("alerts", [])
    assert any(a.get("metric") == "turn_latency_ms_p95" for a in alerts)
