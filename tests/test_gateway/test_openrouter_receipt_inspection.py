from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from scripts import inspect_openrouter_receipt as inspector

GENERATION = "gen-fixture123"
MODEL = "mistralai/test-model"
SECRET = "sk-or-v1-TEST-ONLY-NOT-A-KEY"
PRIVATE = "private-transcript-must-not-appear"


def receipt(cost: str = "0.0012300", **updates) -> bytes:
    data = {
        "id": GENERATION, "model": MODEL, "is_byok": False, "cancelled": False,
        "finish_reason": "stop", "total_cost": "NUMBER", "usage": "NUMBER",
        "upstream_inference_cost": None, "prompt": PRIVATE, "error_metadata": SECRET,
    }
    data.update(updates)
    return json.dumps({"data": data}).replace('"NUMBER"', cost).encode()


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    requests = []

    def install(body: bytes = b"", *, status: int = 200, error=None, headers=None, stream=None):
        def handler(request):
            requests.append(request)
            assert request.method == "GET"
            assert str(request.url) == inspector.ENDPOINT + "?id=" + GENERATION
            assert request.headers["authorization"] == "Bearer " + SECRET
            assert request.headers["accept-encoding"] == "identity"
            assert request.extensions["timeout"] == dict.fromkeys(
                ("connect", "read", "write", "pool"), 5.0,
            )
            if error:
                raise error
            return httpx.Response(
                status, headers={"content-type": "application/json", **(headers or {})},
                stream=stream if stream is not None else httpx.ByteStream(body),
            )

        def factory(**options):
            assert options == {"retries": 0, "trust_env": False}
            return httpx.MockTransport(handler)

        monkeypatch.setattr(inspector.httpx, "AsyncHTTPTransport", factory)
        return requests

    return install


def inspect() -> dict:
    return asyncio.run(inspector.inspect_receipt(GENERATION, MODEL))


@pytest.mark.parametrize("cost,expected", [
    ("0.0012300", "0.00123"), ("0", "0"), ("-0.0", "0"),
    ("1.234567890123456789012345678901e-5", "0.00001234567890123456789012345678901"),
])
@pytest.mark.parametrize("cancelled", [False, True])
def test_managed_receipt_reports_exact_decimal_snapshot(transport, cost, expected, cancelled):
    requests = transport(receipt(cost, cancelled=cancelled, finish_reason=None if cancelled else "stop"))
    result = inspect()
    assert result["status"] == ("verified_non_billed" if Decimal(cost) == 0 else "verified_billed")
    assert result["cost_usd"] == expected
    assert result["cancelled"] is cancelled
    assert len(requests) == 1
    for flag in ("redispatch_authorized", "customer_funds_changed", "account_reconciled", "final_settlement_verified"):
        assert result[flag] is False
    assert PRIVATE not in json.dumps(result) and SECRET not in json.dumps(result)
    assert GENERATION not in json.dumps(result) and MODEL not in json.dumps(result)


@pytest.mark.parametrize("updates,reason", [
    ({"id": "gen-other"}, "mismatched_identity"),
    ({"model": "other/model"}, "mismatched_identity"),
    ({"id": None}, "incomplete_identity"),
    ({"total_cost": None}, "incomplete_cost"),
    ({"usage": None}, "incomplete_cost"),
    ({"usage": 2}, "inconsistent_cost"),
    ({"finish_reason": None}, "incomplete_generation"),
    ({"finish_reason": "processing"}, "incomplete_generation"),
    ({"finish_reason": []}, "malformed_finish_reason"),
    ({"is_byok": True}, "external_billing_not_supported"),
    ({"is_byok": None}, "incomplete_billing_state"),
    ({"cancelled": 0}, "incomplete_billing_state"),
])
def test_ambiguous_receipts_remain_unknown(transport, updates, reason):
    transport(receipt(**updates))
    result = inspect()
    assert (result["status"], result["reason"], result["cost_usd"]) == ("unknown", reason, None)


@pytest.mark.parametrize("cost", ["NaN", "Infinity", "-Infinity", "-1", "true", '"0"', "[]", "{}", "1e999999999"])
def test_invalid_or_nonfinite_cost_cannot_be_zero(transport, cost):
    transport(receipt(cost))
    result = inspect()
    assert result["status"] == "unknown"
    assert result["reason"] in {"invalid_cost", "malformed_json"}
    assert result["cost_usd"] is None


@pytest.mark.parametrize("status", [301, 307, 401, 402, 404, 429, 500])
def test_http_errors_do_not_follow_retry_or_print_remote_body(transport, capsys, status):
    requests = transport(
        (SECRET + PRIVATE).encode(), status=status,
        headers={"location": "https://example.invalid/" + SECRET},
    )
    assert inspector.main(["--generation-id", GENERATION, "--expected-model", MODEL]) == 2
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert result["status"] == "unknown" and result["cost_usd"] is None
    assert result["reason"] == ("not_found" if status == 404 else "http_error")
    assert len(requests) == 1
    assert not out.err and SECRET not in out.out and PRIVATE not in out.out


@pytest.mark.parametrize("error,reason", [
    (httpx.ConnectError(SECRET + PRIVATE), "network_error"),
    (httpx.ReadTimeout(SECRET + PRIVATE), "timeout"),
])
def test_network_failure_safe_and_no_retry(transport, error, reason, capsys):
    requests = transport(error=error)
    assert inspector.main(["--generation-id", GENERATION, "--expected-model", MODEL]) == 2
    out = capsys.readouterr()
    assert json.loads(out.out)["reason"] == reason
    assert SECRET not in out.out + out.err and PRIVATE not in out.out + out.err
    assert len(requests) == 1


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"{}", b'{"error": "private"}',
                                   b'{"data": {}, "data": {}}', b'\xff'])
def test_malformed_receipt(transport, body):
    transport(body)
    result = inspect()
    assert result["status"] == "unknown" and result["cost_usd"] is None


def test_bounded_response(transport):
    transport(b" " * (inspector.MAX_BYTES + 1))
    assert inspect()["reason"] == "response_too_large"


def test_whole_request_deadline_covers_dribbling_body(monkeypatch, transport):
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(1000):
                await asyncio.sleep(0.005)
                yield b" "

    monkeypatch.setattr(inspector, "TOTAL_TIMEOUT_SEC", 0.02)
    requests = transport(stream=SlowBody())
    assert inspect()["reason"] == "timeout"
    assert len(requests) == 1


def test_no_dotenv_or_alternate_key_loading(monkeypatch, tmp_path, transport):
    requests = transport(receipt())
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=" + SECRET + "\n")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert inspect()["reason"] == "missing_or_invalid_api_key"
    assert requests == []


def test_invalid_cli_arguments_are_not_echoed(capsys):
    with pytest.raises(SystemExit) as exc:
        inspector.main(["--api-key", SECRET])
    assert exc.value.code == 2
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert json.loads(out.err)["reason"] == "invalid_arguments"


def test_invalid_identity_never_contacts_any_host(transport):
    requests = transport(receipt())
    result = asyncio.run(inspector.inspect_receipt("https://example.invalid/" + SECRET, MODEL))
    assert result["reason"] == "invalid_expected_identity"
    assert requests == []


def test_success_cli_prints_only_allowlisted_fields(transport, capsys):
    transport(receipt())
    assert inspector.main(["--generation-id", GENERATION, "--expected-model", MODEL]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["status"] == "verified_billed"
    assert PRIVATE not in out.out and SECRET not in out.out and not out.err
