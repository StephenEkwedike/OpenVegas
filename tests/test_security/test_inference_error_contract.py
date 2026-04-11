from __future__ import annotations

import json

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
    def __init__(self, *, context_enabled: bool = True):
        self._context_enabled = context_enabled

    async def prepare_thread(self, **_kwargs):
        return _ThreadCtx()

    async def append_exchange(self, **_kwargs):
        return None

    async def get_recent_messages_with_stats(self, **_kwargs):
        return [], 0, 0

    def context_enabled(self):
        return self._context_enabled


class _MismatchThreadService:
    async def prepare_thread(self, **_kwargs):
        raise ContractError(APIErrorCode.PROVIDER_THREAD_MISMATCH, "Thread belongs to a different provider.")

    async def append_exchange(self, **_kwargs):
        return None

    async def get_recent_messages_with_stats(self, **_kwargs):
        return [], 0, 0

    def context_enabled(self):
        return True


class _InferResult:
    text = "ok"
    v_cost = "0.01"
    input_tokens = 3
    output_tokens = 5
    provider_request_id = "req-1"
    tool_calls: list[dict] = []
    web_search_used = False
    web_search_sources: list[str] = []
    web_search_retry_without_tool = False


class _CaptureGateway:
    def __init__(self):
        self.last_messages: list[dict] | None = None
        self.last_enable_web_search: bool | None = None

    async def infer(self, req):
        self.last_messages = list(req.messages)
        self.last_enable_web_search = bool(getattr(req, "enable_web_search", False))
        return _InferResult()


class _StreamingGateway(_CaptureGateway):
    async def stream_infer(self, req):
        self.last_messages = list(req.messages)
        self.last_enable_web_search = bool(getattr(req, "enable_web_search", False))
        yield {"type": "text_delta", "text": "hello "}
        yield {"type": "text_delta", "text": "world"}
        out = _InferResult()
        out.text = "hello world"
        yield {"type": "completed", "result": out}


class _ToolStreamingGateway(_CaptureGateway):
    async def stream_infer(self, req):
        self.last_messages = list(req.messages)
        self.last_enable_web_search = bool(getattr(req, "enable_web_search", False))
        yield {"type": "text_delta", "text": "thinking"}
        out = _InferResult()
        out.text = "thinking"
        out.tool_calls = [{"tool_name": "Read", "arguments": {"path": "README.md"}}]
        yield {"type": "completed", "result": out}


class _FileUploadServiceStub:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = list(rows or [])
        self.last_user_id: str | None = None
        self.last_file_ids: list[str] = []

    async def resolve_uploaded_for_inference(self, *, user_id: str, file_ids: list[str]):
        self.last_user_id = str(user_id)
        self.last_file_ids = list(file_ids)
        return list(self.rows)


def _parse_sse(response_text: str) -> list[tuple[str, dict]]:
    blocks = [b for b in str(response_text or "").split("\n\n") if b.strip()]
    out: list[tuple[str, dict]] = []
    for block in blocks:
        event_name = ""
        payload: dict = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: "):].strip()
            elif line.startswith("data: "):
                raw = line[len("data: "):].strip()
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {}
        if event_name:
            out.append((event_name, payload))
    return out


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


def test_inference_replays_history_and_returns_context_diagnostics(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    class _ReplayThreadService(_ThreadService):
        async def get_recent_messages_with_stats(self, **_kwargs):
            return (
                [
                    {"role": "user", "content": "prior user"},
                    {"role": "assistant", "content": "prior assistant"},
                ],
                3,
                1,
            )

    gateway = _CaptureGateway()
    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: gateway)
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ReplayThreadService())

    client = TestClient(_app_with_router())
    response = client.post(
        "/inference/ask",
        json={"prompt": "new prompt", "provider": "openai", "model": "gpt-5"},
    )
    assert response.status_code == 200
    assert gateway.last_messages == [
        {"role": "user", "content": "prior user"},
        {"role": "assistant", "content": "prior assistant"},
        {"role": "user", "content": "new prompt"},
    ]
    payload = response.json()
    assert payload["context_enabled"] is True
    assert payload["history_messages_loaded"] == 3
    assert payload["history_messages_skipped"] == 1
    assert payload["history_messages_used"] == 2
    assert payload["history_messages_dropped"] == 0
    assert payload["did_prune"] is False


def test_inference_dedupes_current_prompt_if_tail_matches(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    class _ReplayThreadService(_ThreadService):
        async def get_recent_messages_with_stats(self, **_kwargs):
            return (
                [
                    {"role": "assistant", "content": "previous assistant"},
                    {"role": "user", "content": "same prompt"},
                ],
                2,
                0,
            )

    gateway = _CaptureGateway()
    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: gateway)
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ReplayThreadService())

    client = TestClient(_app_with_router())
    response = client.post(
        "/inference/ask",
        json={"prompt": "same prompt", "provider": "openai", "model": "gpt-5"},
    )
    assert response.status_code == 200
    assert gateway.last_messages == [
        {"role": "assistant", "content": "previous assistant"},
        {"role": "user", "content": "same prompt"},
    ]


def test_inference_prunes_history_by_budget(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    long_text = "x" * 80

    class _ReplayThreadService(_ThreadService):
        async def get_recent_messages_with_stats(self, **_kwargs):
            return (
                [
                    {"role": "user", "content": long_text},
                    {"role": "assistant", "content": long_text},
                    {"role": "user", "content": long_text},
                ],
                3,
                0,
            )

    gateway = _CaptureGateway()
    monkeypatch.setenv("OPENVEGAS_CONTEXT_MODEL_WINDOW_TOKENS", "4000")
    monkeypatch.setenv("OPENVEGAS_CONTEXT_HISTORY_BUDGET_FRACTION", "0.007")
    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: gateway)
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ReplayThreadService())

    client = TestClient(_app_with_router())
    response = client.post(
        "/inference/ask",
        json={"prompt": "fresh", "provider": "openai", "model": "gpt-5"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["did_prune"] is True
    assert payload["history_messages_dropped"] == 2
    assert payload["history_messages_used"] == 1
    assert gateway.last_messages == [
        {"role": "user", "content": long_text},
        {"role": "user", "content": "fresh"},
    ]


def test_inference_disabled_context_uses_single_prompt_message(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    class _DisabledThreadCtx:
        thread_id = None
        thread_status = "disabled"

    class _DisabledThreadService(_ThreadService):
        async def prepare_thread(self, **_kwargs):
            return _DisabledThreadCtx()

        async def get_recent_messages_with_stats(self, **_kwargs):
            raise AssertionError("history replay should not be called when context is disabled")

    gateway = _CaptureGateway()
    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: gateway)
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _DisabledThreadService(context_enabled=False))

    client = TestClient(_app_with_router())
    response = client.post(
        "/inference/ask",
        json={"prompt": "only prompt", "provider": "openai", "model": "gpt-5"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["context_enabled"] is False
    assert payload["history_messages_loaded"] == 0
    assert payload["history_messages_skipped"] == 0
    assert payload["history_messages_used"] == 0
    assert payload["history_messages_dropped"] == 0
    assert payload["did_prune"] is False
    assert gateway.last_messages == [{"role": "user", "content": "only prompt"}]


def test_inference_web_search_diagnostics_for_openai(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    class _WebResult(_InferResult):
        web_search_used = True
        web_search_sources = [
            "https://zillow.com/home/1?utm_source=x",
            "https://zillow.com/home/1",
            "ftp://ignored.example",
        ]
        web_search_retry_without_tool = True

    class _Gateway:
        async def infer(self, req):
            assert bool(req.enable_web_search) is True
            return _WebResult()

    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: _Gateway())
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ThreadService())
    monkeypatch.setenv("OPENVEGAS_FEATURES_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_ENABLE_WEB_SEARCH", "1")
    monkeypatch.setenv("OPENVEGAS_ROLLOUT_WEB_SEARCH_PCT", "100")
    monkeypatch.setenv("OPENVEGAS_CHAT_WEB_SEARCH_SOURCES_MAX", "8")
    client = TestClient(_app_with_router())

    response = client.post(
        "/inference/ask",
        json={"prompt": "find austin homes", "provider": "openai", "model": "gpt-5", "enable_web_search": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["web_search_requested"] is True
    assert payload["web_search_effective"] is True
    assert payload["web_search_used"] is True
    assert payload["web_search_retry_without_tool"] is True
    assert payload["warning"] is None
    assert payload["web_search_sources"] == ["https://zillow.com/home/1"]
    assert isinstance(payload.get("web_search_source_ranking"), list)


def test_inference_non_openai_web_search_returns_warning_with_normal_answer(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    gateway = _CaptureGateway()
    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: gateway)
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ThreadService())
    client = TestClient(_app_with_router())

    response = client.post(
        "/inference/ask",
        json={"prompt": "find austin homes", "provider": "anthropic", "model": "claude-sonnet-4", "enable_web_search": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["text"] == "ok"
    assert payload["warning"] == "capability_unavailable:web_search"
    assert payload["web_search_requested"] is True
    assert payload["web_search_effective"] is False
    assert payload["web_search_used"] is False
    assert gateway.last_enable_web_search is False


def test_inference_openai_attachments_build_multimodal_user_parts(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    gateway = _CaptureGateway()
    file_svc = _FileUploadServiceStub(
        rows=[
            {
                "file_id": "f1",
                "filename": "diagram.png",
                "mime_type": "image/png",
                "size_bytes": 12,
                "content_bytes": b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00",
            },
            {
                "file_id": "f2",
                "filename": "notes.txt",
                "mime_type": "text/plain",
                "size_bytes": 5,
                "content_bytes": b"hello",
            },
        ]
    )
    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: gateway)
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ThreadService())
    monkeypatch.setattr(inference_routes, "get_file_upload_service", lambda: file_svc)
    monkeypatch.setenv("OPENVEGAS_FEATURES_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_ENABLE_FILES", "1")
    monkeypatch.setenv("OPENVEGAS_ROLLOUT_FILE_UPLOAD_PCT", "100")
    monkeypatch.setenv("OPENVEGAS_ENABLE_VISION", "1")
    monkeypatch.setenv("OPENVEGAS_ROLLOUT_IMAGE_INPUT_PCT", "100")

    client = TestClient(_app_with_router())
    response = client.post(
        "/inference/ask",
        json={
            "prompt": "summarize these files",
            "provider": "openai",
            "model": "gpt-5",
            "attachments": ["f1", "f2"],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["attachments_requested"] is True
    assert payload["attachments_effective"] is True
    assert payload["attachments_used"] is True
    assert file_svc.last_file_ids == ["f1", "f2"]

    assert gateway.last_messages is not None
    user_message = gateway.last_messages[-1]
    assert user_message["role"] == "user"
    assert isinstance(user_message["content"], list)
    types = [str(p.get("type")) for p in user_message["content"] if isinstance(p, dict)]
    assert "input_text" in types
    assert "input_image" in types


def test_inference_non_openai_attachments_fallback_to_text_context(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    gateway = _CaptureGateway()
    file_svc = _FileUploadServiceStub(
        rows=[
            {
                "file_id": "f3",
                "filename": "memo.txt",
                "mime_type": "text/plain",
                "size_bytes": 4,
                "content_bytes": b"memo",
            }
        ]
    )
    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: gateway)
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ThreadService())
    monkeypatch.setattr(inference_routes, "get_file_upload_service", lambda: file_svc)
    monkeypatch.setenv("OPENVEGAS_FEATURES_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_ENABLE_FILES", "1")
    client = TestClient(_app_with_router())

    response = client.post(
        "/inference/ask",
        json={
            "prompt": "summarize memo",
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "attachments": ["f3"],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["attachments_requested"] is True
    assert payload["attachments_effective"] is False
    assert payload["attachments_used"] is True
    assert payload["warning"] == "capability_unavailable:file_upload"
    assert gateway.last_messages is not None
    last_content = str(gateway.last_messages[-1].get("content") or "")
    assert "Attached file context" in last_content
    assert "[memo.txt]" in last_content


def test_inference_image_attachment_without_vision_returns_deterministic_warning(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    class _Gateway:
        called = False

        async def infer(self, _req):
            self.called = True
            return _InferResult()

    gateway = _Gateway()
    file_svc = _FileUploadServiceStub(
        rows=[
            {
                "file_id": "fimg",
                "filename": "diagram.png",
                "mime_type": "image/png",
                "size_bytes": 12,
                "content_bytes": b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00",
            }
        ]
    )
    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: gateway)
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ThreadService())
    monkeypatch.setattr(inference_routes, "get_file_upload_service", lambda: file_svc)
    monkeypatch.setenv("OPENVEGAS_FEATURES_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_ENABLE_FILES", "1")
    monkeypatch.setenv("OPENVEGAS_ROLLOUT_FILE_UPLOAD_PCT", "100")
    monkeypatch.setenv("OPENVEGAS_ENABLE_VISION", "0")
    monkeypatch.setenv("OPENVEGAS_ROLLOUT_IMAGE_INPUT_PCT", "0")
    monkeypatch.setenv("OPENVEGAS_ENABLE_WEB_SEARCH", "1")
    monkeypatch.setenv("OPENVEGAS_ROLLOUT_WEB_SEARCH_PCT", "100")

    client = TestClient(_app_with_router())
    response = client.post(
        "/inference/ask",
        json={
            "prompt": "analyze this screenshot",
            "provider": "openai",
            "model": "gpt-5",
            "enable_web_search": True,
            "attachments": ["fimg"],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["text"].lower().startswith("image input is unavailable")
    assert payload["warning"] == "capability_unavailable:image_input"
    assert "capability_unavailable:image_input" in (payload.get("warnings") or [])
    assert payload["web_search_requested"] is True
    assert payload["web_search_effective"] is True
    assert payload["web_search_used"] is False
    assert payload["web_search_sources"] == []
    assert payload["attachments_requested"] is True
    assert payload["attachments_used"] is False
    assert gateway.called is False


def test_inference_two_turn_zillow_follow_up_does_not_crash(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    class _StatefulThreadService:
        def __init__(self):
            self.history: list[dict[str, str]] = []

        async def prepare_thread(self, **_kwargs):
            return _ThreadCtx()

        async def append_exchange(self, *, prompt: str, response_text: str, **_kwargs):
            self.history.append({"role": "user", "content": prompt})
            self.history.append({"role": "assistant", "content": response_text})
            return None

        async def get_recent_messages_with_stats(self, **_kwargs):
            return list(self.history), len(self.history), 0

        def context_enabled(self):
            return True

    class _Gateway:
        def __init__(self):
            self.calls = 0
            self.last_messages: list[dict[str, str]] = []

        async def infer(self, req):
            self.calls += 1
            self.last_messages = list(req.messages)
            if self.calls == 1:
                class _First(_InferResult):
                    text = "Use permitted listing sources; Austin under $500k with 2+ bed, 1+ bath."
                    web_search_used = True
                    web_search_sources = ["https://example.com/listings/austin"]
                return _First()
            class _Second(_InferResult):
                text = "2408 Longview St APT 211, Austin, TX 78705"
                web_search_used = True
                web_search_sources = ["https://example.com/listings/austin/2408-longview"]
            return _Second()

    thread_svc = _StatefulThreadService()
    gateway = _Gateway()
    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: gateway)
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: thread_svc)
    monkeypatch.setenv("OPENVEGAS_FEATURES_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_ENABLE_WEB_SEARCH", "1")
    client = TestClient(_app_with_router())

    first = client.post(
        "/inference/ask",
        json={
            "prompt": "can you scrape zillow for houses in austin tx under $500k with at least 2br/1ba",
            "provider": "openai",
            "model": "gpt-5",
            "enable_web_search": True,
        },
    )
    assert first.status_code == 200
    assert first.json()["web_search_used"] is True
    assert first.json()["web_search_sources"] == ["https://example.com/listings/austin"]

    second = client.post(
        "/inference/ask",
        json={
            "prompt": "ok help me find some property addresses with those requirements",
            "provider": "openai",
            "model": "gpt-5",
            "enable_web_search": True,
        },
    )
    assert second.status_code == 200
    payload = second.json()
    assert "Austin" in payload["text"]
    assert payload["web_search_sources"] == ["https://example.com/listings/austin/2408-longview"]
    assert gateway.calls == 2
    assert len(gateway.last_messages) >= 3


def test_inference_stream_emits_ordered_sse_events(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: _CaptureGateway())
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ThreadService())
    monkeypatch.setattr(inference_routes, "get_file_upload_service", lambda: _FileUploadServiceStub([]))

    client = TestClient(_app_with_router())
    response = client.post(
        "/inference/stream",
        json={"prompt": "hi", "provider": "openai", "model": "gpt-5.4", "enable_web_search": True},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")

    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[0] == "response.started"
    assert "stream_start" in names
    assert "response.delta" in names
    assert "stream_delta" in names
    assert "stream_end" in names
    assert names[-1] == "response.completed"

    seqs = [int(payload.get("sequence_no", 0)) for _, payload in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert all(str(payload.get("schema_version")) == "ui_event_v1" for _, payload in events)

    completed_payload = [payload for name, payload in events if name == "response.completed"][-1]
    completed_data = completed_payload.get("payload", {})
    assert completed_data.get("status") == "ok"
    assert completed_data.get("input_tokens") == 3
    assert completed_data.get("output_tokens") == 5
    assert isinstance(completed_data.get("web_search_source_ranking", []), list)


def test_inference_stream_uses_gateway_streaming_when_available(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    gateway = _StreamingGateway()
    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: gateway)
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ThreadService())
    monkeypatch.setattr(inference_routes, "get_file_upload_service", lambda: _FileUploadServiceStub([]))

    client = TestClient(_app_with_router())
    response = client.post(
        "/inference/stream",
        json={"prompt": "hi", "provider": "openai", "model": "gpt-5.4"},
    )
    assert response.status_code == 200

    events = _parse_sse(response.text)
    delta_payloads = [payload.get("payload", {}) for name, payload in events if name == "response.delta"]
    assert [payload.get("text") for payload in delta_payloads] == ["hello ", "world"]

    completed_payload = [payload for name, payload in events if name == "response.completed"][-1]
    completed_data = completed_payload.get("payload", {})
    assert completed_data.get("status") == "ok"
    assert completed_data.get("text") == "hello world"
    assert gateway.last_messages is not None


def test_inference_stream_tool_mode_preserves_tool_calls_when_gateway_streams(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    gateway = _ToolStreamingGateway()
    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: gateway)
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ThreadService())
    monkeypatch.setattr(inference_routes, "get_file_upload_service", lambda: _FileUploadServiceStub([]))

    client = TestClient(_app_with_router())
    response = client.post(
        "/inference/stream",
        json={"prompt": "read file", "provider": "openai", "model": "gpt-5.4", "enable_tools": True},
    )
    assert response.status_code == 200

    events = _parse_sse(response.text)
    tool_events = [payload.get("payload", {}) for name, payload in events if name == "tool.call"]
    assert tool_events
    completed_payload = [payload for name, payload in events if name == "response.completed"][-1]
    completed_data = completed_payload.get("payload", {})
    assert completed_data.get("tool_calls") == [{"tool_name": "Read", "arguments": {"path": "README.md"}}]


def test_inference_stream_emits_normalized_tool_events(monkeypatch):
    class _Fraud:
        async def check_inference(self, _user_id: str):
            return None

    class _GatewayWithTool(_CaptureGateway):
        async def infer(self, req):
            out = await super().infer(req)
            out.tool_calls = [{"tool_name": "fs_read", "arguments": {"path": "README.md"}}]
            return out

    monkeypatch.setattr(inference_routes, "get_fraud_engine", lambda: _Fraud())
    monkeypatch.setattr(inference_routes, "get_gateway", lambda: _GatewayWithTool())
    monkeypatch.setattr(inference_routes, "get_llm_mode_service", lambda: _ModeService())
    monkeypatch.setattr(inference_routes, "get_provider_thread_service", lambda: _ThreadService())
    monkeypatch.setattr(inference_routes, "get_file_upload_service", lambda: _FileUploadServiceStub([]))

    client = TestClient(_app_with_router())
    response = client.post(
        "/inference/stream",
        json={"prompt": "hi", "provider": "openai", "model": "gpt-5.4"},
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert "tool_start" in names
    assert "tool_progress" in names
    assert "tool_result" in names


def test_inference_stream_emits_error_event(monkeypatch):
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
    monkeypatch.setattr(inference_routes, "get_file_upload_service", lambda: _FileUploadServiceStub([]))

    client = TestClient(_app_with_router())
    response = client.post(
        "/inference/stream",
        json={"prompt": "hi", "provider": "openai", "model": "gpt-5.4"},
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[0] == "response.started"
    assert "response.error" in names
    assert names[-1] == "response.completed"
    completed_payload = [payload for name, payload in events if name == "response.completed"][-1]
    completed_data = completed_payload.get("payload", {})
    assert completed_data.get("status") == "error"
