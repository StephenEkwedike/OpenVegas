from __future__ import annotations

from click.testing import CliRunner

from openvegas.cli import cli


class _ClientOps:
    async def get_ops_diagnostics(self) -> dict:
        return {
            "run_summary": {
                "run_count": 3,
                "turn_latency_ms_p50": 120.0,
                "turn_latency_ms_p95": 450.0,
                "turn_latency_ms_avg": 210.0,
                "tool_fail_rate": 0.0,
                "fallback_rate": 0.0,
                "avg_cost_usd": 0.02,
            },
            "thresholds": {"turn_latency_ms_p95": 1000.0},
            "alerts": [],
            "recent_runs": [
                {
                    "run_id": "run-1",
                    "provider": "openai",
                    "model": "gpt-5",
                    "turn_latency_ms": 111.0,
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cost_usd": 0.01,
                    "tool_failures": 0,
                    "fallbacks": 0,
                }
            ],
            "rollback": {"owner": "platform-oncall", "checklist": ["Disable flag", "Verify health"]},
        }

    async def get_ops_alerts(self) -> dict:
        return {
            "alerts": [
                {
                    "metric": "turn_latency_ms_p95",
                    "severity": "critical",
                    "observed": 5000.0,
                    "threshold": 1000.0,
                    "status": "fired",
                }
            ],
            "thresholds": {"turn_latency_ms_p95": 1000.0},
            "run_summary": {"run_count": 2},
            "rollback": {"owner": "platform-oncall", "checklist": ["Disable flag"]},
        }

    async def get_ops_runs(self, *, limit: int = 25) -> dict:
        _ = limit
        return {
            "runs": [
                {
                    "run_id": "run-2",
                    "provider": "openai",
                    "model": "gpt-5.4",
                    "turn_latency_ms": 95.0,
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cost_usd": 0.04,
                    "tool_failures": 0,
                    "fallbacks": 0,
                }
            ]
        }


def test_ops_diagnostics_command_renders_summary(monkeypatch):
    monkeypatch.setattr("openvegas.client.OpenVegasClient", _ClientOps)
    runner = CliRunner()
    result = runner.invoke(cli, ["ops", "diagnostics"])
    assert result.exit_code == 0
    assert "Ops Run Summary" in result.output
    assert "No active alerts." in result.output
    assert "Rollback Owner: platform-oncall" in result.output
    assert "Recent Runs" in result.output


def test_ops_alerts_command_renders_alerts(monkeypatch):
    monkeypatch.setattr("openvegas.client.OpenVegasClient", _ClientOps)
    runner = CliRunner()
    result = runner.invoke(cli, ["ops", "alerts"])
    assert result.exit_code == 0
    assert "Ops Alerts" in result.output
    assert "turn_latency_ms_p95" in result.output
    assert "critical" in result.output


def test_ops_rollback_command_renders_owner(monkeypatch):
    monkeypatch.setattr("openvegas.client.OpenVegasClient", _ClientOps)
    runner = CliRunner()
    result = runner.invoke(cli, ["ops", "rollback"])
    assert result.exit_code == 0
    assert "Rollback Owner: platform-oncall" in result.output
    assert "Disable flag" in result.output


def test_ops_runs_command_renders_rows(monkeypatch):
    monkeypatch.setattr("openvegas.client.OpenVegasClient", _ClientOps)
    runner = CliRunner()
    result = runner.invoke(cli, ["ops", "runs", "--limit", "5"])
    assert result.exit_code == 0
    assert "Recent Runs" in result.output
    assert "run-2" in result.output


def test_ops_watch_command_runs_single_cycle(monkeypatch):
    monkeypatch.setattr("openvegas.client.OpenVegasClient", _ClientOps)
    runner = CliRunner()
    result = runner.invoke(cli, ["ops", "watch", "--cycles", "1", "--interval-sec", "1"])
    assert result.exit_code == 0
    assert "tick=1" in result.output
