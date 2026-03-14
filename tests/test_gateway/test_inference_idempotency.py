from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.inference import AIGateway


class _DummyWallet:
    pass


class _DummyCatalog:
    pass


class _FakeTx:
    def __init__(self, rows: dict[tuple[str, str], dict]):
        self.rows = rows

    async def fetchrow(self, query: str, *args):
        if "INSERT INTO inference_requests" in query and "ON CONFLICT" in query:
            request_id, user_id, idem_key, payload_hash = args
            key = (str(user_id), str(idem_key))
            if key in self.rows:
                return None
            self.rows[key] = {
                "id": str(request_id),
                "user_id": str(user_id),
                "idempotency_key": str(idem_key),
                "payload_hash": str(payload_hash),
                "status": "processing",
                "response_status": None,
                "response_body_text": None,
                "updated_at": datetime.now(timezone.utc),
                "final_charge_v": None,
                "final_provider_cost_usd": None,
                "provider_request_id": None,
            }
            return {"id": str(request_id)}

        if "FROM inference_requests" in query and "FOR UPDATE" in query:
            user_id, idem_key = args
            row = self.rows.get((str(user_id), str(idem_key)))
            return dict(row) if row else None

        return None

    async def execute(self, query: str, *args):
        if "UPDATE inference_requests" in query:
            row_id = str(args[0])
            for key, row in self.rows.items():
                if row["id"] == row_id:
                    self.rows[key] = {
                        **row,
                        "status": "processing",
                        "response_status": None,
                        "response_body_text": None,
                        "final_charge_v": None,
                        "final_provider_cost_usd": None,
                        "provider_request_id": None,
                        "updated_at": datetime.now(timezone.utc),
                    }
                    break
            return "UPDATE 1"
        return "OK"


class _FakeDB:
    def __init__(self, rows: dict[tuple[str, str], dict]):
        self.rows = rows

    @asynccontextmanager
    async def transaction(self):
        yield _FakeTx(self.rows)


@pytest.mark.asyncio
async def test_idempotent_same_key_same_hash_replays_succeeded_row():
    success_body = json.dumps(
        {
            "text": "hello",
            "input_tokens": 10,
            "output_tokens": 5,
            "v_cost": "0.120000",
            "actual_cost_usd": "0.020000",
            "provider_request_id": "prov_1",
        }
    )
    rows = {
        ("u1", "k1"): {
            "id": "req-1",
            "payload_hash": "h1",
            "status": "succeeded",
            "response_status": 200,
            "response_body_text": success_body,
            "updated_at": datetime.now(timezone.utc),
            "final_charge_v": Decimal("0.120000"),
            "final_provider_cost_usd": Decimal("0.020000"),
            "provider_request_id": "prov_1",
        }
    }

    gateway = AIGateway(_FakeDB(rows), _DummyWallet(), _DummyCatalog())
    request_id, replay = await gateway._begin_inference_request(
        user_id="u1",
        idempotency_key="k1",
        payload_hash="h1",
    )

    assert request_id == "req-1"
    assert replay is not None
    assert replay.text == "hello"
    assert replay.input_tokens == 10
    assert replay.output_tokens == 5
    assert replay.v_cost == Decimal("0.120000")


@pytest.mark.asyncio
async def test_idempotent_same_key_different_hash_conflicts():
    rows = {
        ("u1", "k1"): {
            "id": "req-1",
            "payload_hash": "h1",
            "status": "processing",
            "response_status": None,
            "response_body_text": None,
            "updated_at": datetime.now(timezone.utc),
            "final_charge_v": None,
            "final_provider_cost_usd": None,
            "provider_request_id": None,
        }
    }

    gateway = AIGateway(_FakeDB(rows), _DummyWallet(), _DummyCatalog())
    with pytest.raises(ContractError) as exc:
        await gateway._begin_inference_request(
            user_id="u1",
            idempotency_key="k1",
            payload_hash="h2",
        )

    assert exc.value.code == APIErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.asyncio
async def test_processing_not_stale_raises_hold_conflict(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = {
        ("u1", "k1"): {
            "id": "req-1",
            "payload_hash": "h1",
            "status": "processing",
            "response_status": None,
            "response_body_text": None,
            "updated_at": now,
            "final_charge_v": None,
            "final_provider_cost_usd": None,
            "provider_request_id": None,
        }
    }

    monkeypatch.setenv("INFERENCE_REQUEST_STALE_SEC", "600")
    gateway = AIGateway(_FakeDB(rows), _DummyWallet(), _DummyCatalog())
    with pytest.raises(ContractError) as exc:
        await gateway._begin_inference_request(
            user_id="u1",
            idempotency_key="k1",
            payload_hash="h1",
        )

    assert exc.value.code == APIErrorCode.HOLD_CONFLICT


@pytest.mark.asyncio
async def test_processing_stale_is_reopened_for_execution(monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    rows = {
        ("u1", "k1"): {
            "id": "req-1",
            "payload_hash": "h1",
            "status": "processing",
            "response_status": None,
            "response_body_text": None,
            "updated_at": old,
            "final_charge_v": Decimal("0.100000"),
            "final_provider_cost_usd": Decimal("0.001000"),
            "provider_request_id": "prov-old",
        }
    }

    monkeypatch.setenv("INFERENCE_REQUEST_STALE_SEC", "10")
    gateway = AIGateway(_FakeDB(rows), _DummyWallet(), _DummyCatalog())
    request_id, replay = await gateway._begin_inference_request(
        user_id="u1",
        idempotency_key="k1",
        payload_hash="h1",
    )

    assert request_id == "req-1"
    assert replay is None
    reopened = rows[("u1", "k1")]
    assert reopened["status"] == "processing"
    assert reopened["response_body_text"] is None
    assert reopened["final_charge_v"] is None
    assert reopened["provider_request_id"] is None
