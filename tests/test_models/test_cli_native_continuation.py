"""Execute the real nested CLI transport branch in opt-in native-history mode."""
from unittest.mock import AsyncMock

import pytest
import test_cli_native_scope as native

consumer = native.consumer
native_consumer = native.native_consumer


@pytest.fixture(autouse=True)
def original_user_input(native_consumer):
    native_consumer.namespace["native_history_mode"] = True


def history_payload(c, revision=0, calls=True):
    value = native.native_payload(c, status="incomplete" if calls else "complete")
    receipt = value["native_generation"]
    receipt.update(history_revision=revision, continuation_supported=calls)
    value["tool_calls"] = [{"tool_name": "Read", "arguments": {"path": "notes.txt"},
                            "provider_call_id": "call-1", "native_inference_request_id": receipt["inference_request_id"]}] if calls else []
    return value


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("original", ["Original current input", "Read notes.\r\nKeep exact input.\n"])
def test_actual_cli_uses_revision_reference_not_flattened_observation_authority(native_consumer, stream, original):
    c = native_consumer
    c.namespace["user_message"] = original
    c.namespace["_env_flag"] = lambda name, default: (
        True if name in {"OPENVEGAS_CHAT_NATIVE_GENERATION_SCOPE", "OPENVEGAS_CHAT_NATIVE_GENERATION_HISTORY"}
        else stream if name == "OPENVEGAS_CHAT_STREAM_EVENTS" else default == "1")
    first = history_payload(c)
    final = history_payload(c, revision=1, calls=False)
    c.client.ask = AsyncMock(side_effect=[first, final])
    result = c.run([native.event("response.completed", first)])
    first_kwargs = c.requests[0][1] if stream else c.client.ask.call_args.kwargs
    assert first_kwargs["native_user_text"] == original
    assert first_kwargs["native_user_text"] != "prompt"
    assert result["native_generation"] == first["native_generation"]
    c.namespace["current_run_version"] = 7
    c.namespace["current_signature"] = "sha256:" + "b" * 64
    captured = []

    async def following(*args, **kwargs):
        captured.append((args, kwargs))
        yield native.event("response.completed", final)

    c.client.ask_stream = following
    assert native.invoke(c, key="second")["native_generation"] == final["native_generation"]
    kwargs = captured[0][1] if stream else c.client.ask.call_args.kwargs
    assert kwargs["native_continuation"] == {"previous_inference_request_id": first["native_generation"]["inference_request_id"],
                                             "expected_history_revision": 0}
    assert kwargs["native_scope"]["expected_run_version"] == 7
    assert kwargs["native_history"] is True and kwargs["persist_context"] is False
    assert "native_user_text" not in kwargs
    with pytest.raises(native.APIError, match="final"):
        native.invoke(c, key="third")


@pytest.mark.parametrize("status", [404, 405, 501])
def test_native_history_fallback_keeps_exact_key_scope_and_mode(native_consumer, status):
    c = native_consumer
    c.namespace["_env_flag"] = lambda name, default: name in {
        "OPENVEGAS_CHAT_NATIVE_GENERATION_SCOPE", "OPENVEGAS_CHAT_NATIVE_GENERATION_HISTORY"} or default == "1"
    c.client.ask.return_value = history_payload(c)
    assert c.run([native.APIError(status, "unavailable")]) == c.client.ask.return_value
    args, kwargs = c.requests[0]
    c.client.ask.assert_awaited_once_with(*args, **kwargs)
    assert kwargs["native_history"] is True
    assert "native_continuation" not in kwargs


def test_missing_server_history_receipt_is_not_silent_success(native_consumer):
    c = native_consumer
    c.namespace["_env_flag"] = lambda name, default: name in {
        "OPENVEGAS_CHAT_NATIVE_GENERATION_SCOPE", "OPENVEGAS_CHAT_NATIVE_GENERATION_HISTORY"} or default == "1"
    with pytest.raises(native.APIError, match="history receipt"):
        c.run([native.event("response.completed", native.native_payload(c))])
    with pytest.raises(native.APIError, match="unconfirmed"):
        native.invoke(c, key="replacement")
    assert len(c.requests) == 1
