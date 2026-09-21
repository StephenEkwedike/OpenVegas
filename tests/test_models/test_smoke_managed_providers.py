import importlib.util
import json
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/smoke_managed_providers.py"
spec = importlib.util.spec_from_file_location("smoke_managed_providers", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.asyncio
async def test_smoke_real_adapter_bounded_request(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    calls = []

    def response(req):
        calls.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 0,
                "model": module.MODEL,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "amber"},
                    }
                ],
                "usage": {"prompt_tokens": 30, "completion_tokens": 1, "total_tokens": 31},
            },
        )

    result = await module.smoke("fixture-not-a-real-key", transport=httpx.MockTransport(response))
    assert result["status"] == "passed"
    assert result["requests_sent"] == len(calls) == 1
    assert calls[0]["messages"] == module.MESSAGES
    assert calls[0]["max_completion_tokens"] == 32


@pytest.mark.asyncio
async def test_provider_failure_never_retried_or_echoed(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    calls = []

    def response(req):
        calls.append(req)
        return httpx.Response(500, json={"error": {"message": "private secret body"}})

    result = await module.smoke("private-key", transport=httpx.MockTransport(response))
    assert len(calls) == 1
    assert result["status"] == "failed_or_uncertain"
    assert "private" not in json.dumps(result)


def test_dry_run_never_reads_env_or_writes_report(tmp_path):
    report = tmp_path / "report.json"
    assert module.main(["--env-file", str(tmp_path / "missing"), "--report", str(report)]) == 0
    assert not report.exists()


@pytest.mark.parametrize("budget", ["0", "-1", "1.01", "NaN", "Infinity"])
def test_unapproved_budget_no_call(tmp_path, budget):
    with pytest.raises(SystemExit):
        module.main(
            ["--allow-paid", "--budget-usd", budget, "--report", str(tmp_path / "report.json")]
        )


def test_existing_attempt_never_repeated(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    report = tmp_path / "report.json"
    report.write_text("reserved")
    monkeypatch.setattr(module, "smoke", lambda *a, **k: pytest.fail("no second call"))
    assert module.main(["--allow-paid", "--budget-usd", "1", "--report", str(report)]) == 2
    assert report.read_text() == "reserved"
