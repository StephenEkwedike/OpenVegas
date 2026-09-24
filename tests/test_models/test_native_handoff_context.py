"""Exact public history and real payload building, with no paid transport."""
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openvegas.agent.native_handoff_document import PortableTaskDocument
from openvegas.contracts.errors import ContractError
from openvegas.gateway.inference import InferenceRequest
from openvegas.gateway.openrouter import build_payload, input_token_bound
from server.services import native_handoff_attachments as attachments
from server.services import native_handoff_context as context
from server.services.attachment_history import references
from server.services.file_uploads import FileUploadError
from server.services.openrouter_attachment_request import (
    AttachmentRequestContext,
    prepare_attachment_request,
)
from server.services.openrouter_attachments import AttachmentError
from tests.test_models.test_native_handoff_document import read_observation
from tests.test_models.test_openrouter_attachments import (
    IDS,
    MODEL,
    OTHER,
    OWNER,
    config,
    picture,
    review,
    row,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No network is allowed in handoff composition tests")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)


@pytest.fixture
def case(monkeypatch):
    inspected = review()
    monkeypatch.setattr(attachments, "get_model_review", lambda *args: inspected)
    monkeypatch.setattr(context, "get_model_review", lambda *args: inspected)
    monkeypatch.setattr("server.services.openrouter_attachment_request.get_model_review", lambda *args: inspected)
    tasks = [{"user_text": "Inspect my file.", "attachment_refs": references([row()]),
              "generations": [{"assistant_text": "I'll read it.", "observations": [read_observation()]},
                              {"assistant_text": "It's a greeting.", "observations": [],
                               "web_search_used": True, "web_search_sources": ["https://example.com/source"]}]}]
    request = InferenceRequest(account_id="user:" + OWNER, provider="openrouter", model=MODEL,
                               messages=[{"role": "system", "content": "Fresh server instructions."},
                                         {"role": "user", "content": "Now use that greeting."}],
                               max_tokens=100, enable_tools=True, reasoning_effort="high")
    return SimpleNamespace(tasks=tasks, request=request, config=config(), review=inspected,
                           capabilities={"tools": True, "context_window_tokens": 500000,
                                         "reasoning_efforts": ["low", "high"]},
                           uploads=SimpleNamespace(resolve_uploaded_for_inference=AsyncMock(return_value=[row()])))


async def prepare(case):
    doc = PortableTaskDocument.from_tasks(case.tasks)
    preview = await attachments.preview_handoff_attachments(
        document=doc, user_id=OWNER, model_id=MODEL, model_config=case.config, upload_service=case.uploads)
    return await context.prepare_destination_request(
        request=case.request, document=doc, preview=preview, user_id=OWNER,
        model_config=case.config, capabilities=case.capabilities, upload_service=case.uploads)


async def add_current_files(case, rows=None):
    rows = [row(picture(), "image/png", "current.png", index=1)] if rows is None else rows
    case.rows = {item["file_id"]: item for item in [row(), *rows]}
    case.uploads.resolve_uploaded_for_inference.side_effect = (
        lambda *, user_id, file_ids: [deepcopy(case.rows[ident]) for ident in file_ids]
    )
    messages, prepared, refs = await prepare_attachment_request(
        history=case.request.messages[:-1], prompt=case.request.messages[-1]["content"],
        file_ids=[item["file_id"] for item in rows], user_id=OWNER, model_id=MODEL,
        model_config=case.config, upload_service=case.uploads,
    )
    case.request.messages = messages
    case.request._managed_attachment_context = prepared
    return prepared.prepared, refs


@pytest.mark.asyncio
async def test_public_history_data_fresh_tools_reasoning_and_full_bound(case):
    before = deepcopy(case.request.messages)
    result = await prepare(case)
    assert case.request.messages == before and case.request._managed_attachment_context is None
    assert result.messages[0] == before[0] and result.messages[-1] == before[-1]
    assert result.messages[1]["content"] == context.HISTORY_NOTICE
    user = result.messages[2]
    assert user["content"][0]["text"] == "Inspect my file."
    assert user["content"][1]["text"].endswith("complete text")
    assert result.messages[3] == {"role": "assistant", "content": "I'll read it."}
    assert '"content":"hello\\n"' in result.messages[4]["content"]
    assert result.messages[4]["content"].startswith("Historical tool observations")
    assert result.messages[5]["content"] == "It's a greeting."
    assert "https://example.com/source" in result.messages[6]["content"]
    assert all(m["role"] != "tool" and set(m) == {"role", "content"} for m in result.messages)
    payload = build_payload(result, case.config, case.capabilities)
    assert payload["tools"] and payload["reasoning"] == {"effort": "high", "exclude": True}
    assert payload["transforms"] == [] and payload["provider"]["allow_fallbacks"] is False
    assert input_token_bound(result) > sum(len(str(m["content"]).encode()) for m in result.messages)
    assert case.uploads.resolve_uploaded_for_inference.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tools", "reasoning", "context", "message_count", "wire_bytes"])
async def test_unsupported_or_oversize_input_rejected_without_mutating_request(case, change, monkeypatch):
    original = deepcopy(case.request.messages)
    if change == "tools":
        case.capabilities["tools"] = False
    elif change == "reasoning":
        case.capabilities["reasoning_efforts"] = ["low"]
    elif change == "context":
        case.capabilities["context_window_tokens"] = 101
    elif change == "wire_bytes":
        monkeypatch.setattr(context, "MAX_REQUEST_BYTES", 100)
    else:
        case.tasks[0]["generations"] = [
            {"assistant_text": "First", "observations": [read_observation()]}
            for _ in range(100)
        ] + [{"assistant_text": "Final", "observations": []}]
    with pytest.raises((ContractError, AttachmentError)):
        await prepare(case)
    assert case.request.messages == original


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["account", "provider", "native", "media", "web", "assistant", "raw_media"])
async def test_private_or_nonfresh_context_not_accepted(case, change):
    if change == "account":
        case.request.account_id = "agent:someone-else"
    elif change == "provider":
        case.request.provider = "openai"
    elif change == "native":
        case.request._native_generation_claim = SimpleNamespace(previous_request_id="old-request")
    elif change == "media":
        case.request._managed_attachment_context = object()
    elif change == "web":
        case.request._managed_web_context = object()
    elif change == "assistant":
        case.request.messages.insert(0, {"role": "assistant", "content": "unowned history"})
    else:
        case.request.messages[-1]["content"] = [{"type": "text", "text": "injected"}]
    with pytest.raises(ContractError):
        await prepare(case)
    # Only preview lookup occurred; rejected request did not start dispatch resolution.
    assert case.uploads.resolve_uploaded_for_inference.await_count == 1


@pytest.mark.asyncio
async def test_repeated_tasks_keep_order_and_files_without_summarizing(case):
    second = deepcopy(case.tasks[0])
    second["user_text"] = "Second user task."
    second["generations"] = [{"assistant_text": "Second completed answer.", "observations": []}]
    case.tasks.append(second)
    result = await prepare(case)
    texts = [m["content"] for m in result.messages]
    assert texts[-2] == "Second completed answer."
    assert texts[-3][0]["text"] == "Second user task."
    assert len(result._managed_attachment_context.prepared.blocks) == 2


@pytest.mark.asyncio
async def test_current_input_is_snapshotted_before_attachment_await(case):
    original = deepcopy(case.request.messages)
    doc = PortableTaskDocument.from_tasks(case.tasks)
    preview = await attachments.preview_handoff_attachments(
        document=doc, user_id=OWNER, model_id=MODEL, model_config=case.config, upload_service=case.uploads)

    async def mutate(**kwargs):
        case.request.messages[-1].update(role="assistant", content="Substituted during await")
        case.request.model = "different/model"
        return [row()]

    case.uploads.resolve_uploaded_for_inference.side_effect = mutate
    result = await context.prepare_destination_request(request=case.request, document=doc, preview=preview,
        user_id=OWNER, model_config=case.config, capabilities=case.capabilities, upload_service=case.uploads)
    assert result.messages[-1] == original[-1] and result.model == MODEL
    assert "Substituted during await" not in str(result.messages)


@pytest.mark.asyncio
async def test_only_one_current_user_message_follows_historical_context(case):
    case.request.messages.insert(1, {"role": "user", "content": "An earlier current-user message"})
    with pytest.raises(ContractError):
        await prepare(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("historical_files", [False, True])
@pytest.mark.parametrize("kind", ["text", "image", "mixed"])
async def test_current_files_compose_without_moving_or_dropping_occurrences(case, historical_files, kind):
    rows = [row(b"Current full text", filename="current.txt", index=1)]
    if kind == "image":
        rows = [row(picture(), "image/png", "current.png", index=1)]
    elif kind == "mixed":
        rows.append(row(picture(), "image/png", "current.png", index=2))
    if not historical_files:
        case.tasks[0]["attachment_refs"] = []
    current, _ = await add_current_files(case, rows)
    original = deepcopy(case.request)
    result = await prepare(case)
    combined = result._managed_attachment_context.prepared
    assert result.messages[0] == original.messages[0]
    assert result.messages[1] == {"role": "system", "content": context.HISTORY_NOTICE}
    assert result.messages[-1] == original.messages[-1]
    assert result.messages[-1]["content"][1:] == current.blocks
    expected_ids = ((IDS[0],) if historical_files else ()) + current.file_ids
    assert combined.file_ids == expected_ids
    assert combined.blocks[-len(current.blocks):] == current.blocks
    assert combined.media_tokens == current.media_tokens
    assert combined.review == current.review and combined.user_id == OWNER
    assert case.request.messages == original.messages
    assert case.request._managed_attachment_context == original._managed_attachment_context
    assert result._managed_attachment_context is not case.request._managed_attachment_context
    payload = build_payload(result, case.config, case.capabilities)
    assert payload["messages"] == result.messages and payload["tools"]
    assert payload["reasoning"] == {"effort": "high", "exclude": True}
    assert payload["provider"]["only"] == [current.review.provider]
    assert payload["provider"]["allow_fallbacks"] is False
    assert payload["transforms"] == []
    assert case.uploads.resolve_uploaded_for_inference.await_count == (4 if historical_files else 2)
    for call in case.uploads.resolve_uploaded_for_inference.await_args_list:
        assert call.kwargs["user_id"] == OWNER


@pytest.mark.asyncio
async def test_repeated_image_across_history_and_current_keeps_every_original_bound(case):
    image = row(picture(), "image/png", "shared.png")
    case.tasks[0]["attachment_refs"] = references([image])
    case.tasks.append(deepcopy(case.tasks[0]))
    current, _ = await add_current_files(case, [image])
    result = await prepare(case)
    combined = result._managed_attachment_context.prepared
    assert combined.file_ids == current.file_ids * 3
    assert combined.blocks == current.blocks * 3
    assert combined.media_tokens == current.media_tokens * 3
    user_files = [m["content"][1:] for m in result.messages if isinstance(m["content"], list)]
    assert user_files == [current.blocks] * 3
    assert input_token_bound(result) >= 3 * current.review.image_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "owner", "model", "review", "expired", "missing", "duplicate", "reordered", "substituted",
    "extra_text", "string", "bad_prompt", "system_media", "web", "fake_context",
])
async def test_current_media_requires_exact_owned_context_before_dispatch(case, change):
    prepared, _ = await add_current_files(case, [row(index=1), row(picture(), "image/png", "a.png", index=2)])
    blocks = case.request.messages[-1]["content"]
    if change == "owner":
        prepared = replace(prepared, user_id=OTHER)
    elif change == "model":
        prepared = replace(prepared, review=replace(prepared.review, model_id="other/model"))
    elif change == "review":
        prepared = replace(prepared, review=replace(prepared.review, image_tokens=1))
    elif change == "expired":
        prepared = replace(prepared, review=replace(prepared.review, expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    elif change == "missing":
        blocks.pop()
    elif change == "duplicate":
        blocks.append(deepcopy(blocks[-1]))
    elif change == "reordered":
        blocks[1], blocks[2] = blocks[2], blocks[1]
    elif change == "substituted":
        blocks[-1]["image_url"]["url"] = "https://private.example/injected.png"
    elif change == "extra_text":
        blocks[1]["text"] += " injected secret"
    elif change == "string":
        case.request.messages[-1]["content"] = "Drop my media silently"
    elif change == "bad_prompt":
        blocks[0] = {"type": "image_url", "image_url": {"url": "https://private.example/a"}}
    elif change == "system_media":
        case.request.messages[0]["content"] = deepcopy(blocks)
    elif change == "web":
        case.request._managed_web_context = object()
    case.request._managed_attachment_context = (
        SimpleNamespace(prepared=prepared) if change == "fake_context" else AttachmentRequestContext(prepared)
    )
    with pytest.raises((ContractError, AttachmentError)) as error:
        await prepare(case)
    assert "private.example" not in str(error.value) and "injected secret" not in str(error.value)
    # Initial current preparation and historical preview only.
    assert case.uploads.resolve_uploaded_for_inference.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["media_tokens", "bytes", "owner", "missing", "id", "expired", "incomplete"])
async def test_current_uploads_are_reauthorized_and_compared_not_reusable_authority(case, change):
    prepared, _ = await add_current_files(case)
    if change == "media_tokens":
        case.request._managed_attachment_context = AttachmentRequestContext(replace(prepared, media_tokens=0))
    elif change == "bytes":
        case.rows[IDS[1]] = row(picture(size=(8, 8)), "image/png", "current.png", index=1)
    elif change == "owner":
        case.rows[IDS[1]]["user_id"] = OTHER
    elif change == "id":
        case.rows[IDS[1]]["file_id"] = IDS[2]
    else:
        def changed(*, user_id, file_ids):
            if file_ids == [IDS[1]]:
                if change == "missing":
                    return []
                raise FileUploadError(410 if change == "expired" else 409, "unavailable", "private file detail")
            return [deepcopy(case.rows[ident]) for ident in file_ids]

        case.uploads.resolve_uploaded_for_inference.side_effect = changed
    with pytest.raises((ContractError, AttachmentError)) as error:
        await prepare(case)
    assert "private file detail" not in str(error.value)
    assert case.uploads.resolve_uploaded_for_inference.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("historical_count,allowed", [(11, True), (12, False)])
async def test_twelve_occurrences_is_combined_limit_not_per_context(case, historical_count, allowed):
    case.tasks = [deepcopy(case.tasks[0]) for _ in range(historical_count)]
    await add_current_files(case, [row()])
    if allowed:
        result = await prepare(case)
        assert result._managed_attachment_context.prepared.file_ids == (IDS[0],) * 12
    else:
        with pytest.raises(ContractError):
            await prepare(case)
        assert case.uploads.resolve_uploaded_for_inference.await_count == 1 + historical_count


@pytest.mark.asyncio
@pytest.mark.parametrize("same_file", [False, True])
@pytest.mark.parametrize("headroom", [0, -1])
async def test_original_byte_limit_counts_history_and_current_even_for_same_id(case, monkeypatch, same_file, headroom):
    await add_current_files(case, [row(index=0 if same_file else 1)])
    monkeypatch.setattr(context, "MAX_TOTAL_BYTES", 2 * len(row()["content_bytes"]) + headroom)
    if headroom == 0:
        assert (await prepare(case))._managed_attachment_context is not None
    else:
        with pytest.raises(ContractError):
            await prepare(case)


@pytest.mark.asyncio
async def test_actual_total_original_byte_limit_is_not_reset_for_current_task(case):
    large = row(b"a" * (64 * 1024))
    case.tasks[0]["attachment_refs"] = references([large])
    case.tasks = [deepcopy(case.tasks[0]) for _ in range(8)]
    await add_current_files(case, [large])
    assert 8 * large["size_bytes"] == context.MAX_TOTAL_BYTES
    with pytest.raises(ContractError):
        await prepare(case)
    assert case.uploads.resolve_uploaded_for_inference.await_count == 18


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "expired", "endpoint", "media_bound"])
async def test_review_must_still_match_after_current_upload_await(case, change):
    await add_current_files(case)

    def revoke(*, user_id, file_ids):
        if file_ids == [IDS[1]]:
            if change == "revoked":
                case.review["account_access"] = False
            elif change == "expired":
                case.review["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            elif change == "endpoint":
                case.review["attachments"]["provider"] = "other-provider"
            else:
                case.review["attachments"]["image_tokens"] += 1
        return [deepcopy(case.rows[ident]) for ident in file_ids]

    case.uploads.resolve_uploaded_for_inference.side_effect = revoke
    with pytest.raises((ContractError, AttachmentError)):
        await prepare(case)


@pytest.mark.asyncio
async def test_current_context_messages_config_and_capabilities_snapshot_before_await(case):
    current, _ = await add_current_files(case)
    original = deepcopy(case.request.messages)
    doc = PortableTaskDocument.from_tasks(case.tasks)
    preview = await attachments.preview_handoff_attachments(
        document=doc, user_id=OWNER, model_id=MODEL, model_config=case.config, upload_service=case.uploads)

    def mutate(*, user_id, file_ids):
        case.request.messages[-1]["content"].clear()
        case.request._managed_attachment_context = object()
        case.request.model = "substituted/model"
        case.config["max_tokens"] = 1
        case.capabilities["tools"] = False
        return [deepcopy(case.rows[ident]) for ident in file_ids]

    case.uploads.resolve_uploaded_for_inference.side_effect = mutate
    result = await context.prepare_destination_request(
        request=case.request, document=doc, preview=preview, user_id=OWNER,
        model_config=case.config, capabilities=case.capabilities, upload_service=case.uploads)
    assert result.model == MODEL and result.messages[-1] == original[-1]
    assert result._managed_attachment_context.prepared.blocks[-1:] == current.blocks


@pytest.mark.asyncio
async def test_composed_wire_bound_includes_tools_options_and_reasoning(case, monkeypatch):
    await add_current_files(case)
    result = await prepare(case)
    payload = build_payload(result, case.config, case.capabilities)
    wire_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
    assert wire_size > len(json.dumps(result.messages, ensure_ascii=False).encode())
    monkeypatch.setattr(context, "MAX_REQUEST_BYTES", wire_size)
    assert (await prepare(case)).messages == result.messages
    monkeypatch.setattr(context, "MAX_REQUEST_BYTES", wire_size - 1)
    with pytest.raises(ContractError):
        await prepare(case)


@pytest.mark.asyncio
async def test_composed_media_tokens_cannot_fit_using_only_current_bound(case):
    image = row(picture(), "image/png", "shared.png")
    case.tasks[0]["attachment_refs"] = references([image])
    await add_current_files(case, [image])
    result = await prepare(case)
    complete_bound = input_token_bound(result) + result.max_tokens
    case.capabilities["context_window_tokens"] = complete_bound
    assert (await prepare(case))._managed_attachment_context.prepared.media_tokens == 10000
    case.capabilities["context_window_tokens"] = complete_bound - 1
    with pytest.raises(ContractError):
        await prepare(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "prepared_type", "review_type", "ids_list", "ids_empty", "ids_duplicate", "ids_nonstring",
    "tokens_bool", "tokens_negative", "blocks_not_json", "blocks_mapping", "block_kind",
    "prompt_extra_key", "prompt_nontext", "too_many_current_files",
])
async def test_current_context_schema_is_positive_and_failure_diagnostics_are_safe(case, change):
    prepared, _ = await add_current_files(case)
    parts = case.request.messages[-1]["content"]
    if change == "prepared_type":
        prepared = SimpleNamespace(**vars(prepared))
    elif change == "review_type":
        prepared = replace(prepared, review=SimpleNamespace(**vars(prepared.review)))
    elif change == "ids_list":
        prepared = replace(prepared, file_ids=list(prepared.file_ids))
    elif change == "ids_empty":
        prepared = replace(prepared, file_ids=())
    elif change == "ids_duplicate":
        prepared = replace(prepared, file_ids=prepared.file_ids * 2)
    elif change == "ids_nonstring":
        prepared = replace(prepared, file_ids=(True,))
    elif change == "tokens_bool":
        prepared = replace(prepared, media_tokens=True)
    elif change == "tokens_negative":
        prepared = replace(prepared, media_tokens=-1)
    elif change == "blocks_not_json":
        prepared = replace(prepared, _blocks_json="sensitive malformed bytes")
    elif change == "blocks_mapping":
        prepared = replace(prepared, _blocks_json='{"sensitive":"not a list"}')
    elif change == "block_kind":
        parts[1] = {"type": "audio", "sensitive": "unreviewed media"}
        prepared = replace(prepared, _blocks_json=json.dumps(parts[1:]))
    elif change == "prompt_extra_key":
        parts[0]["sensitive"] = "hidden client extension"
    elif change == "prompt_nontext":
        parts[0]["text"] = ["sensitive"]
    else:
        prepared = replace(prepared, file_ids=tuple(IDS), _blocks_json=json.dumps(prepared.blocks * 4))
        parts.extend(deepcopy(parts[1:]) * 3)
    case.request._managed_attachment_context = AttachmentRequestContext(prepared)
    with pytest.raises(ContractError) as error:
        await prepare(case)
    assert "sensitive" not in str(error.value) and "unreviewed media" not in str(error.value)
    assert case.uploads.resolve_uploaded_for_inference.await_count == 2


@pytest.mark.asyncio
async def test_three_current_files_are_preserved_in_order(case):
    rows = [row(b"first", index=1), row(b"second", index=2), row(b"third", index=3)]
    current, _ = await add_current_files(case, rows)
    result = await prepare(case)
    assert result.messages[-1]["content"][1:] == current.blocks
    assert result._managed_attachment_context.prepared.file_ids == tuple(IDS)


@pytest.mark.asyncio
async def test_no_attachments_needs_no_managed_context_or_upload_lookup(case):
    case.tasks[0]["attachment_refs"] = []
    result = await prepare(case)
    assert result._managed_attachment_context is None
    assert result.messages[-1] == case.request.messages[-1]
    case.uploads.resolve_uploaded_for_inference.assert_not_awaited()
