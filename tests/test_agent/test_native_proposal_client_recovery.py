"""Lost read-only proposal responses get one identical metadata-only retry."""

import asyncio
import json

import httpx
import pytest

import openvegas.client as client_mod

REQUEST = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def make_client(monkeypatch):
    monkeypatch.setattr(client_mod, "get_backend_url", lambda: "https://backend.invalid")
    monkeypatch.setattr(client_mod, "get_bearer_token", lambda: None)
    monkeypatch.setattr(client_mod, "get_session", dict)
    monkeypatch.setattr(client_mod, "token_expires_soon", lambda *a, **k: False)
    factory = httpx.AsyncClient

    def create(handler):
        monkeypatch.setattr(client_mod.httpx, "AsyncClient", lambda **kwargs: factory(
            transport=httpx.MockTransport(handler), trust_env=False, **kwargs
        ))
        return client_mod.OpenVegasClient()

    return create


def proposal(**changes):
    return {
        "run_id": "run-test", "runtime_session_id": "session-test",
        "expected_run_version": 7, "expected_valid_actions_signature": "signature-test",
        "idempotency_key": "one-proposal", "tool_name": "fs_read",
        "arguments": {"path": "fixture.txt"}, "shell_mode": "read_only",
        "native_inference_request_id": REQUEST, "native_provider_call_id": "call-test",
        **changes,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ReadError, httpx.ConnectError])
async def test_exact_payload_recovered_once_without_start(make_client, failure):
    requests = []
    args = {"path": "fixture.txt"}

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            args["path"] = "changed-during-await.txt"
            raise failure("synthetic lost response", request=request)
        return httpx.Response(200, json={"tool_request": {"tool_call_id": "original"}})

    async with make_client(handle) as client:
        result = await client.agent_tool_propose(**proposal(arguments=args))
    assert result["tool_request"]["tool_call_id"] == "original"
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert json.loads(requests[1].content)["arguments"] == {"path": "fixture.txt"}
    assert all(r.url.path == "/agent/runs/run-test/tools/propose" for r in requests)


@pytest.mark.asyncio
async def test_second_network_failure_propagates_without_third_attempt(make_client):
    requests = []

    def handle(request):
        requests.append(request)
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    async with make_client(handle) as client:
        with pytest.raises(client_mod.APIError):
            await client.agent_tool_propose(**proposal())
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"native_inference_request_id": None}, {"native_inference_request_id": "bad-id"},
    {"native_provider_call_id": None}, {"native_provider_call_id": ""},
    {"tool_name": "fs_apply_patch"}, {"tool_name": "shell"},
    {"shell_mode": "mutating"}, {"idempotency_key": ""},
])
async def test_unbound_or_mutating_proposal_never_retried(make_client, change):
    requests = []

    def handle(request):
        requests.append(request)
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    async with make_client(handle) as client:
        with pytest.raises(client_mod.APIError):
            await client.agent_tool_propose(**proposal(**change))
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [409, 422, 500, 503])
async def test_http_rejection_is_not_transport_recovery(make_client, status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={"error": "synthetic_rejection"})

    async with make_client(handle) as client:
        with pytest.raises(client_mod.APIError):
            await client.agent_tool_propose(**proposal())
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_cancellation_never_retries(make_client):
    requests = []

    def handle(request):
        requests.append(request)
        raise asyncio.CancelledError

    async with make_client(handle) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.agent_tool_propose(**proposal())
    assert len(requests) == 1
