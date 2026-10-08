"""Bounded recovery checks; no paid provider calls or native desktop claims."""
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from openvegas.agent.native_handoff_client import PendingHandoffError
from openvegas.client import APIError
from openvegas.contracts.native_handoff import NativeHandoffResolution
from tests.test_models.test_cli_native_handoff import NEW, shell as shell
from tests.test_models.test_native_handoff_client import client_for
from tests.test_models.test_native_handoff_session import case as case, staged


def resolution(request, preview, outcome="expired_uncommitted"):
    return {"request": request.model_dump(mode="json"), "outcome": outcome,
            "confirmed": ({**preview, "destination_scope": request.destination_scope.model_dump()}
                          if outcome == "committed" else None)}


async def uncertain(case):
    await staged(case)
    case.client.native_handoff_confirm.side_effect = APIError(409, "Private upstream detail")
    with pytest.raises(PendingHandoffError):
        await case.pending.confirm(case.client)
    assert case.pending.blocks_other_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["committed", "expired_uncommitted"])
async def test_verified_resolution_preserves_source_and_recovers_only_exact_operation(case, outcome):
    await uncertain(case)
    old = deepcopy(vars(case.session))
    case.client.native_handoff_resolve = AsyncMock(return_value=resolution(
        case.pending.confirm_request, case.preview, outcome))
    result = await case.pending.resolve(case.client)
    assert result.outcome == outcome and vars(case.session) == old
    if outcome == "expired_uncommitted":
        assert case.pending.state == "cancelled" and not case.pending.blocks_other_actions
        with pytest.raises(PendingHandoffError):
            case.pending.adopt()
    else:
        assert case.pending.state == "confirmed"
        assert case.pending.adopt().awaiting_first_dispatch
    assert case.client.native_handoff_confirm.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["key", "destination", "digest", "unknown", "private", "timeout", "http"])
async def test_unverified_resolution_cannot_clear_uncertainty(case, fault):
    await uncertain(case)
    payload = resolution(case.pending.confirm_request, case.preview)
    if fault == "key":
        payload["request"]["idempotency_key"] = "wrong-key"
    elif fault == "destination":
        payload["request"]["destination_scope"]["expected_run_version"] += 1
    elif fault == "digest":
        payload["request"]["handoff_sha256"] = "c" * 64
    elif fault == "unknown":
        payload["outcome"] = "not_found"
    elif fault == "private":
        payload["private"] = "Private upstream detail"
    case.client.native_handoff_resolve = AsyncMock(return_value=payload)
    if fault in {"http", "timeout"}:
        case.client.native_handoff_resolve.side_effect = (
            APIError(409, "Private upstream detail") if fault == "http"
            else TimeoutError("Private upstream detail"))
    with pytest.raises(PendingHandoffError) as error:
        await case.pending.resolve(case.client)
    assert "Private upstream detail" not in str(error.value)
    assert case.pending.state == "confirm_uncertain" and case.pending.blocks_other_actions
    with pytest.raises(PendingHandoffError):
        case.pending.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [False, True])
async def test_http_resolution_is_bound_to_frozen_request(case, monkeypatch, mismatch):
    await staged(case)
    request = case.pending.confirm_request
    payload = resolution(request, case.preview)
    if mismatch:
        payload["request"]["idempotency_key"] = "wrong-key"
    seen = []

    def handle(wire):
        seen.append(wire.url.path)
        return httpx.Response(200, json=payload)

    async with client_for(monkeypatch, handle) as client:
        if mismatch:
            with pytest.raises(APIError) as error:
                await client.native_handoff_resolve(request)
            assert error.value.status == 502 and error.value.data == {}
        else:
            assert (await client.native_handoff_resolve(request)).outcome == "expired_uncommitted"
    assert seen == ["/agent/native-handoffs/resolve"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["committed", "expired_uncommitted"])
async def test_actual_cli_recovers_without_redispatch_or_history_loss(shell, outcome):
    old_session = shell.source_session
    shell.lose_confirm = True

    async def resolve(request):
        return resolution(request, shell.preview, outcome)

    shell.client.native_handoff_resolve = AsyncMock(side_effect=resolve)
    success = await shell.helpers.switch("openrouter", NEW, shell.target)
    assert success is (outcome == "committed")
    assert shell.state()["pending_native_handoff"] is None
    if outcome == "expired_uncommitted":
        shell.assert_old()
        assert shell.state()["native_generation_session"] is old_session
        assert any("expired before confirmation" in note for note in shell.notes)
    else:
        assert shell.state()["current_model"] == NEW
        assert shell.state()["native_generation_session"].awaiting_first_dispatch
    assert not shell.inference_bodies
    shell.client.native_handoff_confirm.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolution_contract_rejects_contradictory_terminal_outcome(case):
    await staged(case)
    payload = resolution(case.pending.confirm_request, case.preview, "committed")
    payload["outcome"] = "expired_uncommitted"
    with pytest.raises(ValidationError) as error:
        NativeHandoffResolution.model_validate(payload)
    assert case.pending.confirm_request.idempotency_key not in repr(error.value.errors())
    payload["outcome"], payload["confirmed"] = "committed", None
    with pytest.raises(ValidationError) as error:
        NativeHandoffResolution.model_validate(payload)
    assert case.pending.confirm_request.idempotency_key not in error.value.json()
