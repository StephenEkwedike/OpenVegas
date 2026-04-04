from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from openvegas.gateway.inference import AIGateway, InferenceRequest


class _DummyWallet:
    pass


class _DummyCatalog:
    pass


class _FakeResponsesAPI:
    def __init__(self, payload):
        self.payload = payload
        self.called = False
        self.kwargs = None
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.called = True
        self.kwargs = dict(kwargs)
        self.calls.append(dict(kwargs))
        return self.payload


class _FakeOpenAIError(Exception):
    def __init__(self, message: str, *, code: str = "", param: str = "", error: dict | None = None):
        super().__init__(message)
        self.code = code
        self.param = param
        self.error = error or {}


class _FakeResponsesRejectWebSearchFirst:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        if len(self.calls) == 1:
            raise _FakeOpenAIError(
                "web search unsupported",
                code="invalid_request_error",
                param="tools[0].type",
                error={"message": "Unsupported tool type: web_search_preview"},
            )
        return self.payload


class _FakeChatCompletionsAPI:
    def __init__(self, payload):
        self.payload = payload
        self.called = False
        self.kwargs = None

    async def create(self, **kwargs):
        self.called = True
        self.kwargs = dict(kwargs)
        return self.payload


class _FakeChatCompletionsRejectMaxCompletionFirst:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        if "max_completion_tokens" in kwargs and len(self.calls) == 1:
            raise Exception(
                "Error code: 400 - {'error': {'message': \"Unsupported parameter: 'max_completion_tokens'\"}}"
            )
        return self.payload


class _FakeOpenAIClient:
    def __init__(self, responses_api, chat_api):
        self.responses = responses_api
        self.chat = SimpleNamespace(completions=chat_api)


def _install_fake_openai(monkeypatch, *, client):
    module = SimpleNamespace(AsyncOpenAI=lambda api_key: client)
    monkeypatch.setitem(sys.modules, "openai", module)


@pytest.mark.asyncio
async def test_gpt54_routes_to_openai_responses_api(monkeypatch):
    responses_payload = SimpleNamespace(
        id="resp_1",
        output_text="hello from responses",
        output=[],
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )
    responses_api = _FakeResponsesAPI(responses_payload)
    chat_api = _FakeChatCompletionsAPI(payload=None)
    _install_fake_openai(
        monkeypatch,
        client=_FakeOpenAIClient(responses_api=responses_api, chat_api=chat_api),
    )

    gw = AIGateway(db=SimpleNamespace(), wallet=_DummyWallet(), catalog=_DummyCatalog())
    req = InferenceRequest(
        account_id="user:u1",
        provider="openai",
        model="gpt-5.4",
        messages=[{"role": "user", "content": "say hi"}],
        max_tokens=256,
    )

    result = await gw._call_openai(req, api_key="sk-test")

    assert responses_api.called is True
    assert chat_api.called is False
    assert responses_api.kwargs["model"] == "gpt-5.4"
    assert responses_api.kwargs["max_output_tokens"] == 256
    assert responses_api.kwargs["input"][0]["role"] == "user"
    assert responses_api.kwargs["input"][0]["content"][0]["type"] == "input_text"
    assert result.text == "hello from responses"
    assert result.input_tokens == 11
    assert result.output_tokens == 7


@pytest.mark.asyncio
async def test_legacy_openai_model_routes_to_chat_completions(monkeypatch):
    msg = SimpleNamespace(content="hello from chat", tool_calls=[])
    chat_payload = SimpleNamespace(
        id="chatcmpl_1",
        choices=[SimpleNamespace(message=msg)],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
    )
    responses_api = _FakeResponsesAPI(payload=None)
    chat_api = _FakeChatCompletionsAPI(payload=chat_payload)
    _install_fake_openai(
        monkeypatch,
        client=_FakeOpenAIClient(responses_api=responses_api, chat_api=chat_api),
    )

    gw = AIGateway(db=SimpleNamespace(), wallet=_DummyWallet(), catalog=_DummyCatalog())
    req = InferenceRequest(
        account_id="user:u1",
        provider="openai",
        model="gpt-4o",
        messages=[{"role": "user", "content": "say hi"}],
        max_tokens=64,
    )

    result = await gw._call_openai(req, api_key="sk-test")

    assert chat_api.called is True
    assert responses_api.called is False
    assert chat_api.kwargs["model"] == "gpt-4o"
    assert chat_api.kwargs["max_completion_tokens"] == 64
    assert result.text == "hello from chat"
    assert result.input_tokens == 5
    assert result.output_tokens == 3


@pytest.mark.asyncio
async def test_openai_responses_tool_calls_are_parsed(monkeypatch):
    function_call = SimpleNamespace(
        type="function_call",
        name="call_local_tool",
        arguments='{"tool_name":"Read","arguments":{"filepath":"README.md"},"shell_mode":"read_only","timeout_sec":15}',
    )
    responses_payload = SimpleNamespace(
        id="resp_2",
        output_text="",
        output=[function_call],
        usage=SimpleNamespace(input_tokens=9, output_tokens=2),
    )
    responses_api = _FakeResponsesAPI(responses_payload)
    chat_api = _FakeChatCompletionsAPI(payload=None)
    _install_fake_openai(
        monkeypatch,
        client=_FakeOpenAIClient(responses_api=responses_api, chat_api=chat_api),
    )

    gw = AIGateway(db=SimpleNamespace(), wallet=_DummyWallet(), catalog=_DummyCatalog())
    req = InferenceRequest(
        account_id="user:u1",
        provider="openai",
        model="gpt-5.4",
        messages=[{"role": "user", "content": "read README"}],
        enable_tools=True,
    )
    result = await gw._call_openai(req, api_key="sk-test")

    assert responses_api.called is True
    assert result.tool_calls == [
        {
            "tool_name": "Read",
            "arguments": {"filepath": "README.md"},
            "shell_mode": "read_only",
            "timeout_sec": 15,
        }
    ]


@pytest.mark.asyncio
async def test_chat_completions_falls_back_to_max_tokens_when_required(monkeypatch):
    msg = SimpleNamespace(content="ok", tool_calls=[])
    chat_payload = SimpleNamespace(
        id="chatcmpl_fallback",
        choices=[SimpleNamespace(message=msg)],
        usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2),
    )
    responses_api = _FakeResponsesAPI(payload=None)
    chat_api = _FakeChatCompletionsRejectMaxCompletionFirst(payload=chat_payload)
    _install_fake_openai(
        monkeypatch,
        client=_FakeOpenAIClient(responses_api=responses_api, chat_api=chat_api),
    )

    gw = AIGateway(db=SimpleNamespace(), wallet=_DummyWallet(), catalog=_DummyCatalog())
    req = InferenceRequest(
        account_id="user:u1",
        provider="openai",
        model="gpt-4o",
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=99,
    )
    result = await gw._call_openai(req, api_key="sk-test")
    assert result.text == "ok"
    assert len(chat_api.calls) == 2
    assert "max_completion_tokens" in chat_api.calls[0]
    assert "max_tokens" in chat_api.calls[1]


def test_messages_to_openai_responses_input_maps_roles_and_skips_blank_unknown():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "assistant", "content": "   "},
        {"role": "tool", "content": "ignored"},
        {"role": "developer", "content": "rule"},
    ]
    out = AIGateway._messages_to_openai_responses_input(messages)
    assert out == [
        {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "hi"}]},
        {"role": "developer", "content": [{"type": "input_text", "text": "rule"}]},
    ]


def test_messages_to_openai_responses_input_preserves_multimodal_user_parts():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "analyze this"},
                {"type": "input_image", "image_base64": "ZmFrZQ==", "mime_type": "image/png"},
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "input_text", "text": "assistant text still maps to output"}],
        },
    ]
    out = AIGateway._messages_to_openai_responses_input(messages)
    assert out[0]["content"][0] == {"type": "input_text", "text": "analyze this"}
    assert out[0]["content"][1]["type"] == "input_image"
    assert out[0]["content"][1]["image_url"] == "data:image/png;base64,ZmFrZQ=="
    assert out[1]["content"][0]["type"] == "output_text"


@pytest.mark.asyncio
async def test_multimodal_message_forces_openai_responses_path_even_on_gpt4o(monkeypatch):
    responses_payload = SimpleNamespace(
        id="resp_multi",
        output_text="image analyzed",
        output=[],
        usage=SimpleNamespace(input_tokens=20, output_tokens=5),
    )
    responses_api = _FakeResponsesAPI(responses_payload)
    chat_api = _FakeChatCompletionsAPI(payload=None)
    _install_fake_openai(
        monkeypatch,
        client=_FakeOpenAIClient(responses_api=responses_api, chat_api=chat_api),
    )

    gw = AIGateway(db=SimpleNamespace(), wallet=_DummyWallet(), catalog=_DummyCatalog())
    req = InferenceRequest(
        account_id="user:u1",
        provider="openai",
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "what is shown"},
                    {"type": "input_image", "image_base64": "ZmFrZQ==", "mime_type": "image/png"},
                ],
            }
        ],
    )
    result = await gw._call_openai(req, api_key="sk-test")
    assert result.text == "image analyzed"
    assert responses_api.called is True
    assert chat_api.called is False


@pytest.mark.asyncio
async def test_openai_responses_includes_web_search_tool_when_enabled(monkeypatch):
    responses_payload = SimpleNamespace(
        id="resp_web",
        output_text="with web",
        output=[],
        usage=SimpleNamespace(input_tokens=10, output_tokens=4),
    )
    responses_api = _FakeResponsesAPI(responses_payload)
    chat_api = _FakeChatCompletionsAPI(payload=None)
    _install_fake_openai(
        monkeypatch,
        client=_FakeOpenAIClient(responses_api=responses_api, chat_api=chat_api),
    )
    monkeypatch.setenv("OPENVEGAS_OPENAI_WEB_SEARCH_ENABLED", "1")

    gw = AIGateway(db=SimpleNamespace(), wallet=_DummyWallet(), catalog=_DummyCatalog())
    req = InferenceRequest(
        account_id="user:u1",
        provider="openai",
        model="gpt-5.4",
        messages=[{"role": "user", "content": "find austin homes"}],
        enable_web_search=True,
    )
    await gw._call_openai(req, api_key="sk-test")
    assert responses_api.called is True
    tools = responses_api.kwargs.get("tools", [])
    assert {"type": "web_search_preview"} in tools


@pytest.mark.asyncio
async def test_openai_responses_retries_once_without_rejected_web_tool(monkeypatch):
    responses_payload = SimpleNamespace(
        id="resp_retry",
        output_text="fallback succeeded",
        output=[],
        usage=SimpleNamespace(input_tokens=6, output_tokens=3),
    )
    responses_api = _FakeResponsesRejectWebSearchFirst(responses_payload)
    chat_api = _FakeChatCompletionsAPI(payload=None)
    _install_fake_openai(
        monkeypatch,
        client=_FakeOpenAIClient(responses_api=responses_api, chat_api=chat_api),
    )
    monkeypatch.setenv("OPENVEGAS_OPENAI_WEB_SEARCH_ENABLED", "1")

    gw = AIGateway(db=SimpleNamespace(), wallet=_DummyWallet(), catalog=_DummyCatalog())
    req = InferenceRequest(
        account_id="user:u1",
        provider="openai",
        model="gpt-5.4",
        messages=[{"role": "user", "content": "latest homes"}],
        enable_web_search=True,
    )
    result = await gw._call_openai(req, api_key="sk-test")

    assert result.text == "fallback succeeded"
    assert result.web_search_retry_without_tool is True
    assert len(responses_api.calls) == 2
    assert {"type": "web_search_preview"} in list(responses_api.calls[0].get("tools", []))
    assert "tools" not in responses_api.calls[1]


def test_openai_web_source_extraction_dedupes_and_caps():
    ann_1 = SimpleNamespace(url="https://zillow.com/home/1?utm_source=test")
    ann_2 = SimpleNamespace(url="https://zillow.com/home/1")
    ann_3 = SimpleNamespace(url="https://example.com/home/2")
    part_1 = SimpleNamespace(annotations=[ann_1, ann_2], url="")
    part_2 = SimpleNamespace(annotations=[ann_3], url="")
    item = SimpleNamespace(type="message", content=[part_1, part_2])
    resp = SimpleNamespace(output=[item])

    out = AIGateway._extract_openai_web_sources(resp, max_sources=2)
    assert out == [
        "https://zillow.com/home/1?utm_source=test",
        "https://zillow.com/home/1",
    ]
