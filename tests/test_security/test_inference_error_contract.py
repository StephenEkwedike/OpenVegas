from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.contracts.enums import EffectiveReason
from openvegas.wallet.ledger import InsufficientBalance
from server.middleware.auth import get_current_user
from server.routes import inference as inference_routes


def _app_with_router() -> FastAPI:
    app = FastAPI()
    app.include_router(inference_routes.router, prefix="/inference")
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u-test"}
    return app


class _ModeState:
    def as_dict(self):
        return {
            "user_pref_mode": "wrapper",
            "effective_mode": "wrapper",
            "effective_reason": EffectiveReason.USER_PREF_APPLIED.value,
            "conversation_mode": "persistent",
        }


class _ModeService:
    async def resolve_for_user(self, **_kwargs):
        return _ModeState()


class _ByokModeState:
    def as_dict(self):
        return {
            "user_pref_mode": "byok",
            "effective_mode": "byok",
            "effective_reason": EffectiveReason.USER_PREF_APPLIED.value,
            "conversation_mode": "persistent",
        }


class _ByokModeService:
    async def resolve_for_user(self, **_kwargs):
        return _ByokModeState()


class _ThreadCtx:
    thread_id = "thread-1"
    thread_status = "created"


class _ThreadService:
    async def prepare_thread(self, **_kwargs):
        return _ThreadCtx()

    async def append_exchange(self, **_kwargs):
        return None


class _MismatchThreadService:
    async def prepare_thread(self, **_kwargs):
        raise ContractError(APIErrorCode.PROVIDER_THREAD_MISMATCH, "Thread belongs to a different provider.")

    async def append_exchange(self, **_kwargs):
        return None


def test_inference_provider_unavailable_contract(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    class _Gateway:
        async def infer(self, _req):
            raise ContractError(
                APIErrorCode.PROVIDER_UNAVAILABLE,
                "No active provider credentials configured.",
            )

    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: _Gateway())
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ThreadService())

    client = TestClient(_app_with_router())
    response = client.post(
        "/inference/ask",
        json={"prompt": "hi", "provider": "openai", "model": "gpt-5"},
    )
    assert response.status_code == 503
    assert response.json()["error"] == APIErrorCode.PROVIDER_UNAVAILABLE.value
    assert response.json()["user_pref_mode"] == "wrapper"
    assert response.json()["effective_mode"] == "wrapper"
    assert "effective_reason" in response.json()


def test_inference_insufficient_balance_contract(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    class _Gateway:
        async def infer(self, _req):
            raise InsufficientBalance("Need 1.0")

    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: _Gateway())
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ThreadService())

    client = TestClient(_app_with_router())
    response = client.post(
        "/inference/ask",
        json={"prompt": "hi", "provider": "openai", "model": "gpt-5"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == APIErrorCode.INSUFFICIENT_BALANCE.value
    assert response.json()["user_pref_mode"] == "wrapper"
    assert response.json()["effective_mode"] == "wrapper"
    assert "effective_reason" in response.json()


def test_inference_mode_endpoints_return_write_through_shape(monkeypatch):
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ThreadService())
    client = TestClient(_app_with_router())

    resp_get = client.get("/inference/mode")
    assert resp_get.status_code == 200
    assert resp_get.json()["user_pref_mode"] == "wrapper"
    assert resp_get.json()["effective_mode"] == "wrapper"
    assert resp_get.json()["effective_reason"] == EffectiveReason.USER_PREF_APPLIED.value

    resp_post = client.post("/inference/mode", json={"llm_mode": "wrapper", "conversation_mode": "persistent"})
    assert resp_post.status_code == 200
    assert resp_post.json()["conversation_mode"] == "persistent"


def test_inference_byok_mode_returns_stable_not_allowed_contract(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    class _Gateway:
        async def infer(self, _req):
            return None

    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: _Gateway())
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ByokModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ThreadService())
    client = TestClient(_app_with_router())

    response = client.post(
        "/inference/ask",
        json={"prompt": "hi", "provider": "openai", "model": "gpt-5"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == APIErrorCode.BYOK_NOT_ALLOWED.value
    assert response.json()["effective_mode"] == "byok"


def test_inference_thread_provider_mismatch_contract(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    class _Gateway:
        async def infer(self, _req):
            return None

    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: _Gateway())
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _MismatchThreadService())
    client = TestClient(_app_with_router())

    response = client.post(
        "/inference/ask",
        json={"prompt": "hi", "provider": "openai", "model": "gpt-5", "thread_id": "abc"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == APIErrorCode.PROVIDER_THREAD_MISMATCH.value
