"""Offline request/transport integration; no route mocks or paid-provider calls."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway import openrouter
from openvegas.gateway.inference import AIGateway, InferenceRequest, InferenceResult
from server.services import openrouter_attachments as normalization
from server.services.file_uploads import FileUploadService
from server.services.openrouter_attachment_request import prepare_attachment_request
from server.services.openrouter_attachments import AttachmentError, calculate_request_token_bound

MODEL = "fixture/exact-model-20260901"
OWNER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
IMAGE_ID = "00000000-0000-4000-8000-000000000001"
TEXT_ID = "00000000-0000-4000-8000-000000000002"
REJECTION = (AttachmentError, ContractError)


def image_bytes(color="white"):
    output = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(output, format="PNG")
    return output.getvalue()


def catalog_config():
    return {
        "provider": "openrouter",
        "model_id": MODEL,
        "enabled": True,
        "max_tokens": 1000,
        "cost_input_per_1m": "1",
        "cost_output_per_1m": "2",
        "v_price_input_per_1m": "10",
        "v_price_output_per_1m": "20",
    }


def model_review():
    now = datetime.now(UTC)
    return {
        **{key: value for key, value in catalog_config().items() if "_per_1m" in key},
        "account_access": True,
        "completion_chat": True,
        "max_tokens": 1000,
        "context_window_tokens": 500_000,
        "reviewed_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "capabilities": {"tools": True, "image_input": True},
        "observed_pricing": {
            "prompt": "0.000001",
            "completion": "0.000002",
            "request": "0",
            "image": "0",
        },
        "attachments": {
            "schema_version": 1,
            "model_id": MODEL,
            "provider": "fixture-provider",
            "input_modalities": ["text", "image", "file"],
            "pricing_policy": "input_tokens_only",
            "no_additional_fees": True,
            "non_token_fees": {"image": "0", "native_pdf": "0", "plugins": "0", "request": "0"},
            "token_bound_policy": "utf8_bytes_plus_reviewed_media_v1",
            "token_bound_basis": "Synthetic integration-test bounds, not certification of any real model.",
            "request_overhead_tokens": 256,
            "message_overhead_tokens": 32,
            "part_overhead_tokens": 32,
            "image_tokens": 5000,
            "pdf_page_tokens": 10000,
            "pdf_file_overhead_tokens": 1000,
        },
    }


class UploadDB:
    """Only the upload DB boundary is faked; lifecycle/ownership SQL is real."""

    def __init__(self):
        self.rows = {}
        self.lookups = []
        self.put(IMAGE_ID, image_bytes(), "image/png", "picture.png")
        self.put(TEXT_ID, "All text, including \u4f60\u597d.\n".encode(), "text/plain", "notes.txt")

    def put(self, file_id, payload, mime, filename):
        self.rows[file_id] = {
            "id": file_id,
            "user_id": OWNER,
            "filename": filename,
            "mime_type": mime,
            "size_bytes": len(payload),
            "status": "uploaded",
            "content_bytes": payload,
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        }

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, query, *args):
        # Cleanup persistence is outside this fixture; resolution still checks TTL/status.
        pass

    async def fetchrow(self, query, file_id, user_id):
        assert "id = $1::uuid AND user_id = $2::uuid" in query
        self.lookups.append((file_id, user_id))
        row = self.rows.get(file_id)
        return copy.deepcopy(row) if row and row["user_id"] == user_id else None


@dataclass
class Setup:
    db: UploadDB
    config: dict
    review: dict
    monkeypatch: pytest.MonkeyPatch

    def install_review(self):
        self.monkeypatch.setenv(
            "OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({f"openrouter:{MODEL}": self.review})
        )

    async def request(
        self,
        *,
        history=None,
        prompt="Inspect the upload.",
        file_ids=None,
        user_id=OWNER,
        tools=False,
        upload_service=None,
    ):
        outgoing, context, refs = await prepare_attachment_request(
            history=[] if history is None else history,
            prompt=prompt,
            file_ids=[IMAGE_ID] if file_ids is None else file_ids,
            user_id=user_id,
            model_id=MODEL,
            model_config=self.config,
            upload_service=FileUploadService(self.db) if upload_service is None else upload_service,
        )
        req = InferenceRequest(
            account_id="user:" + user_id,
            provider="openrouter",
            model=MODEL,
            messages=outgoing,
            max_tokens=64,
            enable_tools=tools,
        )
        req._managed_model_config = copy.deepcopy(self.config)
        req._managed_attachment_context = context
        return req, refs

    def build(self, req, *, config=None, capabilities=None):
        return openrouter.build_payload(
            req,
            self.config if config is None else config,
            {"context_window_tokens": 500_000, "tools": True}
            if capabilities is None
            else capabilities,
        )


@pytest.fixture
def setup(monkeypatch):
    import socket

    def no_network(*args, **kwargs):
        pytest.fail("Integration tests must not contact an upstream provider")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setenv("OPENVEGAS_CHAT_MAX_ATTACHMENTS", "3")
    state = Setup(UploadDB(), catalog_config(), model_review(), monkeypatch)
    state.install_review()
    return state


def retained_history(refs):
    return [
        {"role": "user", "content": "Original upload question.", "attachment_refs": refs},
        {"role": "assistant", "content": "Prior answer."},
    ]


@pytest.mark.asyncio
async def test_current_owned_image_reaches_actual_transport_as_data_url(setup):
    req, refs = await setup.request()
    payload = setup.build(req)
    data_url = payload["messages"][0]["content"][1]["image_url"]["url"]
    assert base64.b64decode(data_url.split(",", 1)[1]) == setup.db.rows[IMAGE_ID]["content_bytes"]
    assert payload["messages"] == req.messages
    assert payload["messages"][0]["content"][0] == {"type": "text", "text": "Inspect the upload."}
    assert payload["provider"]["only"] == ["fixture-provider"]
    assert payload["provider"]["allow_fallbacks"] is False
    assert payload["provider"]["require_parameters"] is True
    assert payload["provider"]["max_price"] == {
        "prompt": 1,
        "completion": 2,
        "request": 0,
        "image": 0,
    }
    assert payload["plugins"] == [] and payload["transforms"] == []
    assert refs == [
        {
            "file_id": IMAGE_ID,
            "sha256": hashlib.sha256(setup.db.rows[IMAGE_ID]["content_bytes"]).hexdigest(),
        }
    ]
    assert setup.db.lookups == [(IMAGE_ID, OWNER)]
    assert openrouter.input_token_bound(req) == calculate_request_token_bound(
        req.messages,
        prepared=req._managed_attachment_context.prepared,
        max_output_tokens=64,
    )
    assert openrouter.input_token_bound(req) > 5000


@pytest.mark.asyncio
async def test_current_text_is_complete_and_serialized_not_a_preview(setup):
    text = "full content \u4f60\u597d\n" * 700
    setup.db.put(TEXT_ID, text.encode(), "text/plain", "notes.txt")
    req, refs = await setup.request(file_ids=[TEXT_ID])
    payload = setup.build(req)
    assert (
        payload["messages"][0]["content"][1]["text"]
        == "Attachment [notes.txt] (text/plain)\n" + text
    )
    assert "truncated" not in json.dumps(payload)
    assert refs[0]["file_id"] == TEXT_ID
    assert openrouter.input_token_bound(req) > len(text.encode())


@pytest.mark.asyncio
async def test_current_mixed_files_and_real_tool_schema_share_full_request_bound(setup):
    req, _ = await setup.request(file_ids=[IMAGE_ID, TEXT_ID], tools=True)
    payload = setup.build(req)
    assert [part["type"] for part in payload["messages"][0]["content"]] == [
        "text",
        "image_url",
        "text",
    ]
    assert payload["tools"] == openrouter.local_tool_definitions(MODEL)
    assert payload["tool_choice"] == "auto"
    with_tools = openrouter.input_token_bound(req)
    assert with_tools == calculate_request_token_bound(
        payload["messages"],
        prepared=req._managed_attachment_context.prepared,
        max_output_tokens=64,
        tools=payload["tools"],
    )
    req.enable_tools = False
    assert with_tools > openrouter.input_token_bound(req) + len(
        json.dumps(payload["tools"], separators=(",", ":"))
    )


@pytest.mark.asyncio
async def test_retained_followup_reauthorizes_bytes_and_preserves_message_positions(setup):
    first, refs = await setup.request()
    initial_payload = setup.build(first)
    history = retained_history(refs)
    snapshot = copy.deepcopy(history)
    followup, current_refs = await setup.request(history=history, file_ids=[], prompt="What else?")
    payload = setup.build(followup)
    assert current_refs is None
    assert history == snapshot
    assert setup.db.lookups == [(IMAGE_ID, OWNER), (IMAGE_ID, OWNER)]
    assert payload["messages"][0]["content"][1] == initial_payload["messages"][0]["content"][1]
    assert payload["messages"][1] == {"role": "assistant", "content": "Prior answer."}
    assert payload["messages"][2] == {"role": "user", "content": "What else?"}
    assert all(set(message) == {"role", "content"} for message in payload["messages"])
    assert openrouter.input_token_bound(followup) > openrouter.input_token_bound(first)


@pytest.mark.asyncio
async def test_retained_and_current_same_upload_is_counted_twice_not_flattened(setup):
    first, refs = await setup.request()
    followup, current_refs = await setup.request(history=retained_history(refs))
    payload = setup.build(followup)
    assert current_refs == refs
    assert payload["messages"][0]["content"][1] == payload["messages"][2]["content"][1]
    assert followup._managed_attachment_context.prepared.media_tokens == 10000
    assert openrouter.input_token_bound(followup) > openrouter.input_token_bound(first) + 5000
    assert setup.db.lookups == [(IMAGE_ID, OWNER)] * 3


@pytest.mark.asyncio
async def test_retained_image_plus_new_text_returns_only_new_refs(setup):
    _, refs = await setup.request()
    req, current_refs = await setup.request(history=retained_history(refs), file_ids=[TEXT_ID])
    payload = setup.build(req)
    assert current_refs[0]["file_id"] == TEXT_ID and len(current_refs) == 1
    assert payload["messages"][0]["content"][1]["type"] == "image_url"
    assert payload["messages"][2]["content"][1]["type"] == "text"
    assert openrouter.input_token_bound(req) > 5000
    assert setup.db.lookups == [(IMAGE_ID, OWNER), (IMAGE_ID, OWNER), (TEXT_ID, OWNER)]


@pytest.mark.asyncio
async def test_changed_retained_bytes_reject_before_any_new_upload_is_loaded(setup):
    _, refs = await setup.request()
    setup.db.put(IMAGE_ID, image_bytes("black"), "image/png", "picture.png")
    with pytest.raises(ContractError, match="changed"):
        await setup.request(history=retained_history(refs), file_ids=[TEXT_ID])
    assert setup.db.lookups == [(IMAGE_ID, OWNER), (IMAGE_ID, OWNER)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,status", [("expired", 410), ("pending", 409), ("deleted", 404), ("different_owner", 404)]
)
async def test_followup_rechecks_real_upload_lifecycle_and_owner(setup, state, status):
    _, refs = await setup.request()
    row = setup.db.rows[IMAGE_ID]
    if state == "expired":
        row["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    elif state == "pending":
        row["status"] = "pending"
    elif state == "deleted":
        del setup.db.rows[IMAGE_ID]
    else:
        row["user_id"] = OTHER
    with pytest.raises(AttachmentError) as caught:
        await setup.request(history=retained_history(refs), file_ids=[])
    assert caught.value.code == "attachment_unavailable"
    assert caught.value.status_code == status
    assert setup.db.lookups[-1] == (IMAGE_ID, OWNER)


@pytest.mark.asyncio
async def test_another_authenticated_user_cannot_replay_retained_refs(setup):
    _, refs = await setup.request()
    with pytest.raises(AttachmentError, match="attachment_unavailable"):
        await setup.request(history=retained_history(refs), file_ids=[], user_id=OTHER)
    assert setup.db.lookups[-1] == (IMAGE_ID, OTHER)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "user:" + OTHER),
        ("account_id", "agent:" + OWNER),
        ("model", "fixture/different-model"),
        ("provider", "anthropic"),
    ],
)
async def test_transport_rejects_context_scope_changes(setup, field, value):
    req, _ = await setup.request()
    setattr(req, field, value)
    with pytest.raises(REJECTION):
        setup.build(req)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["expired", "removed", "endpoint", "bound", "image_fee", "native_pdf_fee"]
)
async def test_dispatch_revalidates_fresh_operator_review(setup, change):
    req, _ = await setup.request()
    if change == "expired":
        setup.review["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif change == "removed":
        setup.review.pop("attachments")
    elif change == "endpoint":
        setup.review["attachments"]["provider"] = "different-reviewed-endpoint"
    elif change == "bound":
        setup.review["attachments"]["image_tokens"] += 100
    elif change == "image_fee":
        setup.review["observed_pricing"]["image"] = "0.001"
    else:
        setup.review["attachments"]["non_token_fees"]["native_pdf"] = "0.001"
    setup.install_review()
    with pytest.raises(REJECTION):
        setup.build(req)
    assert setup.db.lookups == [(IMAGE_ID, OWNER)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{"enabled": False}, {"cost_input_per_1m": "2"}, {"max_tokens": 32}]
)
async def test_dispatch_revalidates_catalog_config(setup, change):
    req, _ = await setup.request()
    with pytest.raises(REJECTION):
        setup.build(req, config={**setup.config, **change})


@pytest.mark.asyncio
async def test_expired_review_rejects_preparation_before_upload_lookup(setup):
    setup.review["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    setup.install_review()
    with pytest.raises(AttachmentError, match="attachment_review_required"):
        await setup.request()
    assert setup.db.lookups == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        lambda messages: messages[0]["content"][1]["image_url"].update(
            url="https://untrusted.invalid/image"
        ),
        lambda messages: messages[0]["content"][1]["image_url"].update(
            url="data:image/png;base64,AAAA"
        ),
        lambda messages: messages[0]["content"][1]["image_url"].update(detail="auto"),
        lambda messages: messages[0]["content"].pop(),
        lambda messages: messages[0]["content"].append(copy.deepcopy(messages[0]["content"][1])),
        lambda messages: messages[0].update(role="assistant"),
        lambda messages: messages[0].update(annotations=[{"type": "file"}]),
    ],
)
async def test_actual_transport_refuses_tampered_attachment_blocks(setup, mutation):
    req, _ = await setup.request()
    mutation(req.messages)
    with pytest.raises(REJECTION):
        setup.build(req)


@pytest.mark.asyncio
async def test_client_media_without_private_context_never_enters_transport(setup):
    req, _ = await setup.request()
    req._managed_attachment_context = None
    with pytest.raises(ContractError, match="caller-supplied"):
        setup.build(req)


def test_private_attachment_context_is_not_a_request_constructor_parameter():
    with pytest.raises(TypeError, match="_managed_attachment_context"):
        InferenceRequest(
            account_id="user:" + OWNER,
            provider="openrouter",
            model=MODEL,
            messages=[],
            max_tokens=64,
            _managed_attachment_context={},
        )


@pytest.mark.asyncio
async def test_context_guard_uses_full_verified_media_bound_not_base64_size(setup):
    req, _ = await setup.request()
    token_bound = openrouter.input_token_bound(req)
    assert token_bound > len(json.dumps(req.messages).encode()) + 256
    exact_caps = {"tools": True, "context_window_tokens": token_bound + req.max_tokens}
    assert setup.build(req, capabilities=exact_caps)["messages"] == req.messages
    exact_caps["context_window_tokens"] -= 1
    with pytest.raises(ContractError, match="context"):
        setup.build(req, capabilities=exact_caps)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["digest", "remote_id", "extra_url", "assistant_role"])
async def test_tampered_retained_references_fail_closed(setup, tamper):
    _, refs = await setup.request()
    history = retained_history(refs)
    if tamper == "digest":
        history[0]["attachment_refs"][0]["sha256"] = "0" * 64
    elif tamper == "remote_id":
        history[0]["attachment_refs"][0]["file_id"] = "https://untrusted.invalid/file"
    elif tamper == "extra_url":
        history[0]["attachment_refs"][0]["url"] = "https://untrusted.invalid/file"
    else:
        history[0]["role"] = "assistant"
    with pytest.raises(REJECTION):
        await setup.request(history=history, file_ids=[])


@pytest.mark.asyncio
async def test_history_cannot_spoof_current_marker_and_substitute_new_upload(setup):
    _, refs = await setup.request()
    history = retained_history(refs)
    history[0]["_current"] = True
    with pytest.raises(REJECTION):
        req, _ = await setup.request(history=history, file_ids=[TEXT_ID])
        setup.build(req)


@pytest.mark.asyncio
async def test_unowned_media_inside_other_history_is_rejected_by_real_transport(setup):
    history = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://untrusted.invalid/history"}}
            ],
        }
    ]
    with pytest.raises(REJECTION):
        req, _ = await setup.request(history=history)
        setup.build(req)


@pytest.mark.asyncio
async def test_excess_retained_request_files_reject_before_reloading(setup):
    _, refs = await setup.request()
    history = [retained_history(refs)[0] for _ in range(13)]
    setup.db.lookups.clear()
    with pytest.raises(AttachmentError, match="attachment_history_limit"):
        await setup.request(history=history, file_ids=[])
    assert setup.db.lookups == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        None,
        {},
        [None],
        ["invalid row"],
        [{}],
        [{"file_id": IMAGE_ID}],
        [{"file_id": IMAGE_ID, "content_bytes": None}],
        [{"file_id": IMAGE_ID, "content_bytes": "not binary data"}],
        [{"content_bytes": b"bytes without an upload ID"}],
        [],
        [{"file_id": IMAGE_ID, "content_bytes": b"missing required metadata"}],
    ],
)
async def test_invalid_resolver_rows_are_controlled_rejections_before_hashing(setup, rows):
    service = SimpleNamespace(resolve_uploaded_for_inference=AsyncMock(return_value=rows))
    with pytest.raises(REJECTION):
        await setup.request(upload_service=service)
    service.resolve_uploaded_for_inference.assert_awaited_once_with(
        user_id=OWNER, file_ids=[IMAGE_ID]
    )


def gateway_fixture(setup, stored_request=None):
    tx = SimpleNamespace(execute=AsyncMock(), fetchrow=AsyncMock(return_value=None))
    if stored_request is not None:
        tx.fetchrow.side_effect = [None, stored_request]

    @asynccontextmanager
    async def transaction():
        yield tx

    wallet = SimpleNamespace(get_balance=AsyncMock(return_value=Decimal(100)), reserve=AsyncMock())
    gateway = AIGateway(
        SimpleNamespace(transaction=transaction),
        wallet,
        SimpleNamespace(get_model=AsyncMock(return_value=setup.config)),
    )
    gateway._resolve_provider_api_key = AsyncMock(return_value="synthetic-integration-credential")
    gateway._estimate_grant_cover_v = AsyncMock(return_value=Decimal(0))
    return gateway, wallet, tx


@pytest.mark.asyncio
async def test_gateway_reserves_full_attachment_and_tool_bound_before_dispatch(setup):
    req, _ = await setup.request(file_ids=[IMAGE_ID, TEXT_ID], tools=True)
    req.idempotency_key = "synthetic-multimodal-reservation"
    gateway, wallet, _ = gateway_fixture(setup)
    gateway._begin_inference_request = AsyncMock(return_value=("synthetic-request", None))
    context, replay = await gateway._prepare_inference_execution(req)
    verified_bound = openrouter.input_token_bound(req)
    expected = ((Decimal(verified_bound) * 10 + Decimal(64) * 20) / 1_000_000).quantize(
        Decimal("0.000001"), rounding=ROUND_CEILING
    )
    assert replay is None and context.reserve_v == expected
    assert wallet.reserve.call_args.kwargs["amount"] == expected
    assert (
        gateway._estimate_grant_cover_v.call_args.kwargs["estimated_total_tokens"]
        == verified_bound + 64
    )
    assert gateway._begin_inference_request.call_args.kwargs[
        "payload_hash"
    ] == AIGateway._payload_hash(req)
    assert req._managed_attachment_context is not None


@pytest.mark.asyncio
async def test_gateway_rejects_file_tampering_before_credentials_or_wallet_reservation(setup):
    req, _ = await setup.request()
    req.messages[0]["content"][1]["image_url"]["url"] = "https://untrusted.invalid/substitution"
    gateway, wallet, tx = gateway_fixture(setup)
    with pytest.raises(REJECTION):
        await gateway._prepare_inference_execution(req)
    gateway._resolve_provider_api_key.assert_not_awaited()
    wallet.reserve.assert_not_awaited()
    tx.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_payload_hash_commits_to_file_bytes_and_is_repeatable(setup):
    first, _ = await setup.request()
    repeated, _ = await setup.request()
    setup.build(first)
    setup.build(repeated)
    assert AIGateway._payload_hash(first) == AIGateway._payload_hash(repeated)
    setup.db.put(IMAGE_ID, image_bytes("black"), "image/png", "picture.png")
    changed, _ = await setup.request()
    setup.build(changed)
    assert AIGateway._payload_hash(first) != AIGateway._payload_hash(changed)


def succeeded_request(req):
    result = InferenceResult("Completed attachment answer", 100, 5)
    return {
        "id": "00000000-0000-4000-8000-000000000010",
        "payload_hash": AIGateway._payload_hash(req),
        "status": "succeeded",
        "response_status": 200,
        "response_body_text": AIGateway._serialize_success_body(result),
    }


@pytest.mark.asyncio
async def test_gateway_exact_file_request_replay_uses_cached_result_without_new_hold(setup):
    first, _ = await setup.request()
    first.idempotency_key = "synthetic-file-replay"
    stored = succeeded_request(first)
    repeated, _ = await setup.request()
    repeated.idempotency_key = first.idempotency_key
    gateway, wallet, _ = gateway_fixture(setup, stored)
    _, replay = await gateway._prepare_inference_execution(repeated)
    assert replay.text == "Completed attachment answer"
    assert replay.inference_request_id == stored["id"]
    wallet.reserve.assert_not_awaited()
    wallet.get_balance.assert_not_awaited()


@pytest.mark.asyncio
async def test_rematerialized_file_history_conflicts_at_gateway_not_replayed(setup):
    first, refs = await setup.request()
    first.idempotency_key = "synthetic-file-turn-retry"
    stored = succeeded_request(first)
    # These are DIFFERENT gateway payloads. HTTP command replay must intercept
    # retries before loading this appended history; gateway identity stays strict.
    history = [
        {"role": "user", "content": "Inspect the upload.", "attachment_refs": refs},
        {"role": "assistant", "content": "Completed attachment answer"},
    ]
    repeated, _ = await setup.request(history=history)
    repeated.idempotency_key = first.idempotency_key
    gateway, wallet, _ = gateway_fixture(setup, stored)
    assert AIGateway._payload_hash(first) != AIGateway._payload_hash(repeated)
    with pytest.raises(ContractError) as error:
        await gateway._prepare_inference_execution(repeated)
    assert error.value.code == APIErrorCode.IDEMPOTENCY_CONFLICT
    wallet.reserve.assert_not_awaited()
    wallet.get_balance.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_metering_uses_dispatch_bound_if_review_expires_in_flight(
    setup, monkeypatch
):
    req, _ = await setup.request()
    setup.build(req)
    dispatch_bound = openrouter.input_token_bound(req)
    body = {
        "id": "gen-synthetic-file-response",
        "model": MODEL,
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": "Answer"}}
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "total_tokens": 105,
            "cost": "0.000110",
        },
    }

    class AfterReviewExpires(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(hours=2)

    monkeypatch.setattr(normalization, "datetime", AfterReviewExpires)
    result = openrouter.parse_response(body, req, setup.config, AIGateway._parse_local_tool_call)
    assert result["text"] == "Answer" and result["input_tokens"] <= dispatch_bound
