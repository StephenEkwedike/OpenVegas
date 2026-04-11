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


class _FakeStreamResponse:
    def __init__(self, *, lines: list[str], status_code: int = 200, body: str = "", reason_phrase: str = "OK"):
        self.lines = list(lines)
        self.status_code = status_code
        self._body = body
        self.reason_phrase = reason_phrase

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aread(self):
        return self._body.encode("utf-8")

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class _FakeHTTPClient:
    def __init__(self, responses: list[_FakeStreamResponse]):
        self.responses = list(responses)
        self.stream_calls: list[dict] = []

    def stream(self, method: str, url: str, **kwargs):
        self.stream_calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


def _install_fake_openai(monkeypatch, *, client, capture: dict | None = None):
    def _factory(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        return client

    module = SimpleNamespace(AsyncOpenAI=_factory)
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


@pytest.mark.asyncio
async def test_openai_client_reuses_shared_httpx_client(monkeypatch):
    capture: dict[str, object] = {}
    responses_payload = SimpleNamespace(
        id="resp_shared",
        output_text="shared client",
        output=[],
        usage=SimpleNamespace(input_tokens=2, output_tokens=1),
    )
    responses_api = _FakeResponsesAPI(responses_payload)
    chat_api = _FakeChatCompletionsAPI(payload=None)
    shared_http_client = object()
    _install_fake_openai(
        monkeypatch,
        client=_FakeOpenAIClient(responses_api=responses_api, chat_api=chat_api),
        capture=capture,
    )

    gw = AIGateway(
        db=SimpleNamespace(),
        wallet=_DummyWallet(),
        catalog=_DummyCatalog(),
        http_client=shared_http_client,
    )
    req = InferenceRequest(
        account_id="user:u1",
        provider="openai",
        model="gpt-5.4",
        messages=[{"role": "user", "content": "say hi"}],
    )

    result = await gw._call_openai(req, api_key="sk-test")

    assert result.text == "shared client"
    assert capture["api_key"] == "sk-test"
    assert capture["http_client"] is shared_http_client


@pytest.mark.asyncio
async def test_stream_openai_responses_yields_live_deltas_and_final_result():
    http_client = _FakeHTTPClient(
        responses=[
            _FakeStreamResponse(
                lines=[
                    'data: {"type":"response.output_text.delta","delta":"hello "}',
                    "",
                    'data: {"type":"response.output_text.delta","delta":"world"}',
                    "",
                    'data: {"type":"response.completed","response":{"id":"resp_stream","output":[{"type":"message","content":[{"type":"output_text","text":"hello world"}]}],"usage":{"input_tokens":11,"output_tokens":7}}}',
                    "",
                    "data: [DONE]",
                    "",
                ]
            )
        ]
    )
    gw = AIGateway(
        db=SimpleNamespace(),
        wallet=_DummyWallet(),
        catalog=_DummyCatalog(),
        http_client=http_client,  # type: ignore[arg-type]
    )
    req = InferenceRequest(
        account_id="user:u1",
        provider="openai",
        model="gpt-5.4",
        messages=[{"role": "user", "content": "say hi"}],
        max_tokens=64,
    )

    events = [event async for event in gw._stream_openai_responses(req=req, api_key="sk-test")]

    assert [event["type"] for event in events] == ["text_delta", "text_delta", "completed"]
    assert [event["text"] for event in events[:-1]] == ["hello ", "world"]
    result = events[-1]["result"]
    assert result.text == "hello world"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.provider_request_id == "resp_stream"
    assert len(http_client.stream_calls) == 1
    assert http_client.stream_calls[0]["method"] == "POST"
    assert http_client.stream_calls[0]["url"] == "https://api.openai.com/v1/responses"
    assert http_client.stream_calls[0]["json"]["stream"] is True


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
