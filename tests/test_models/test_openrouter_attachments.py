"""Offline attachment contracts. Fixture reviews are NOT real model certification."""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from server.services import openrouter_attachments as oa
from server.services.file_uploads import FileUploadError, FileUploadService

MODEL = "fixture/exact-model-20260901"
OWNER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
IDS = [f"00000000-0000-4000-8000-00000000000{n}" for n in range(1, 5)]


def config():
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


def review():
    now = datetime.now(UTC)
    return {
        **{key: config()[key] for key in oa._PRICE_FIELDS},
        "account_access": True,
        "completion_chat": True,
        "max_tokens": 1000,
        "context_window_tokens": 500_000,
        "reviewed_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
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
            "token_bound_basis": "Synthetic bounds for tests only, not certification of any provider.",
            "request_overhead_tokens": 256,
            "message_overhead_tokens": 32,
            "part_overhead_tokens": 32,
            "image_tokens": 5000,
            "pdf_page_tokens": 10000,
            "pdf_file_overhead_tokens": 1000,
        },
    }


def row(payload=b"complete text", mime="text/plain", filename="note.txt", index=0):
    return {
        "file_id": IDS[index],
        "filename": filename,
        "mime_type": mime,
        "size_bytes": len(payload),
        "content_bytes": payload,
    }


def picture(fmt="PNG", size=(16, 16)):
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format=fmt)
    return buf.getvalue()


def pdf(*, pages=1, encrypted=False, javascript=False, stream=None):
    pypdf = pytest.importorskip("pypdf", reason="Optional native-PDF validator not installed")
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if stream is not None:
        from pypdf.generic import DecodedStreamObject, NameObject

        obj = DecodedStreamObject()
        obj.set_data(stream)
        writer.pages[0][NameObject("/Contents")] = writer._add_object(obj.flate_encode())
    if encrypted:
        writer.encrypt("synthetic-document-password")
    if javascript:
        writer.add_js("app.alert('synthetic test');")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def pdf_validator():
    try:
        oa.validate_attachment_content(
            content_bytes=pdf(), mime_type="application/pdf", filename="probe.pdf"
        )
    except oa.AttachmentError as exc:
        if exc.code == "pdf_validator_unavailable":
            pytest.skip("Platform cannot enforce PDF worker address-space limit")
        raise


async def prepare(rows=None, **overrides):
    rows = [row()] if rows is None else rows
    kwargs = {
        "user_id": OWNER,
        "file_ids": [r["file_id"] for r in rows],
        "model_id": MODEL,
        "model_config": config(),
        "model_review": review(),
        "upload_service": SimpleNamespace(
            resolve_uploaded_for_inference=AsyncMock(return_value=rows)
        ),
    }
    kwargs.update(overrides)
    return await oa.prepare_owned_attachment_blocks(**kwargs)


def messages(prepared):
    return [
        {"role": "user", "content": [{"type": "text", "text": "Inspect files"}, *prepared.blocks]}
    ]


def bound(prepared, content=None, **kwargs):
    return oa.calculate_request_token_bound(
        messages(prepared) if content is None else content,
        prepared=prepared,
        max_output_tokens=100,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("Attachment normalization attempted network access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.asyncio
async def test_owned_text_exact_content_and_full_utf8_bound():
    text = "Hello\n" + "\u4f60\u597d \U0001f642" * 30
    service = SimpleNamespace(
        resolve_uploaded_for_inference=AsyncMock(return_value=[row(text.encode())])
    )
    prepared = await prepare(upload_service=service)
    service.resolve_uploaded_for_inference.assert_awaited_once_with(
        user_id=OWNER, file_ids=[IDS[0]]
    )
    assert prepared.blocks == [
        {"type": "text", "text": f"Attachment [note.txt] (text/plain)\n{text}"}
    ]
    assert prepared.user_id == OWNER and prepared.file_ids == (IDS[0],)
    assert prepared.media_tokens == 0
    assert bound(prepared) > len(text.encode()) + 256
    assert text not in repr(prepared)
    assert prepared.request_options["plugins"] == []
    assert prepared.request_options["transforms"] == []
    assert prepared.request_options["provider"]["only"] == ["fixture-provider"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
async def test_valid_images_are_real_parts_not_text(fmt, mime):
    payload = picture(fmt)
    prepared = await prepare([row(payload, mime, "photo.bin")])
    block = prepared.blocks[0]
    assert block == {
        "type": "image_url",
        "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(payload).decode()}",
            "detail": "high",
        },
    }
    assert prepared.media_tokens == 5000 and bound(prepared) > 5000
    assert base64.b64encode(payload).decode() not in repr(prepared)
    copied = prepared.blocks
    copied[0]["image_url"]["url"] = "https://untrusted.invalid/image"
    assert prepared.blocks[0] == block


@pytest.mark.asyncio
async def test_native_pdf_preserves_bytes_and_forces_no_ocr(pdf_validator):
    payload = pdf(pages=2, stream=b"BT (all contents remain native) Tj ET")
    prepared = await prepare([row(payload, "application/pdf", "document.pdf")])
    assert prepared.blocks == [
        {
            "type": "file",
            "file": {
                "filename": "document.pdf",
                "file_data": "data:application/pdf;base64," + base64.b64encode(payload).decode(),
            },
        }
    ]
    assert prepared.media_tokens == 21000 + len(b"BT (all contents remain native) Tj ET")
    assert bound(prepared) > prepared.media_tokens
    assert prepared.request_options["plugins"] == [
        {"id": "file-parser", "pdf": {"engine": "native"}}
    ]
    assert prepared.request_options["provider"]["allow_fallbacks"] is False
    assert prepared.request_options["provider"]["max_price"]["request"] == 0
    assert prepared.request_options["provider"]["max_price"]["image"] == 0


@pytest.mark.asyncio
async def test_mixed_request_bound_includes_history_tools_all_media_and_framing(pdf_validator):
    prepared = await prepare(
        [
            row(b"all text"),
            row(picture(), "image/png", "photo.png", 1),
            row(pdf(), "application/pdf", "doc.pdf", 2),
        ]
    )
    initial = bound(prepared)
    full = [
        {"role": "system", "content": "rules"},
        {"role": "assistant", "content": "old reply"},
        *messages(prepared),
    ]
    tools = [
        {"type": "function", "function": {"name": "fixture", "parameters": {"type": "object"}}}
    ]
    assert bound(prepared, full, tools=tools) > initial + 64 + len(json.dumps(tools))
    assert prepared.media_tokens == 16000
    assert [b["type"] for b in prepared.blocks] == ["text", "image_url", "file"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("attachments"),
        lambda r: r.update(account_access=False),
        lambda r: r.update(completion_chat=False),
        lambda r: r.update(expires_at="2000-01-01T00:00:00+00:00"),
        lambda r: r.update(reviewed_at="2000-01-01T00:00:00"),
        lambda r: r.update(reviewed_at=(datetime.now(UTC) + timedelta(days=1)).isoformat()),
        lambda r: r.update(expires_at=(datetime.now(UTC) + timedelta(days=31)).isoformat()),
        lambda r: r.update(cost_input_per_1m="999"),
        lambda r: r.update(v_price_input_per_1m="NaN"),
        lambda r: r.update(max_tokens=True),
        lambda r: r["attachments"].update(model_id="fixture/other"),
        lambda r: r["attachments"].update(provider="*"),
        lambda r: r["attachments"].update(schema_version=True),
        lambda r: r["attachments"].update(no_additional_fees=False),
        lambda r: r["attachments"]["non_token_fees"].pop("native_pdf"),
        lambda r: r["attachments"]["non_token_fees"].update(native_pdf="0.001"),
        lambda r: r["attachments"]["non_token_fees"].update(plugins="0.001"),
        lambda r: r["attachments"].update(pricing_policy="text_tokens_only"),
        lambda r: r["attachments"].update(token_bound_basis=""),
        lambda r: r["attachments"].update(token_bound_policy="bytes_divided_by_four"),
        lambda r: r["attachments"].update(request_overhead_tokens=0),
        lambda r: r["attachments"].update(image_tokens=0),
        lambda r: r["attachments"].update(pdf_page_tokens=True),
        lambda r: r["attachments"].update(pdf_file_overhead_tokens=-1),
        lambda r: r["attachments"].update(unreviewed_extra=True),
        lambda r: r["attachments"].update(input_modalities=["text", "audio"]),
        lambda r: r["observed_pricing"].pop("image"),
        lambda r: r["observed_pricing"].pop("request"),
        lambda r: r["observed_pricing"].update(image="0.001"),
        lambda r: r["observed_pricing"].update(request="0.001"),
        lambda r: r["observed_pricing"].update(overrides=[{"tier": 1}]),
        lambda r: r["observed_pricing"].update(unknown_fee="0"),
        lambda r: r["observed_pricing"].update(input_cache_write="0.001"),
        lambda r: r["observed_pricing"].update(internal_reasoning="0.001"),
        lambda r: r["observed_pricing"].update(prompt="NaN"),
    ],
)
@pytest.mark.asyncio
async def test_unreviewed_configuration_rejected_before_upload_lookup(mutate):
    settings = review()
    mutate(settings)
    service = SimpleNamespace(resolve_uploaded_for_inference=AsyncMock())
    with pytest.raises(oa.AttachmentError, match="attachment_review_required"):
        await prepare(model_review=settings, upload_service=service)
    service.resolve_uploaded_for_inference.assert_not_awaited()


@pytest.mark.parametrize(
    "model",
    ["openrouter/auto", "fixture/model:online", "fixture/model-latest", "https://host/model", "*"],
)
def test_only_exact_models(model):
    with pytest.raises(oa.AttachmentError, match="attachment_review_required"):
        oa.validate_attachment_review(model_id=model, model_config=config(), model_review=review())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ids",
    [
        ["https://host/file.pdf"],
        ["file:///etc/passwd"],
        ["data:image/png;base64,AAA="],
        ["../file"],
        [IDS[0], IDS[0]],
        [None],
        [IDS[0] + " "],
        [],
        IDS,
    ],
)
async def test_ids_strict_bounded_and_not_urls_before_resolver(ids):
    service = SimpleNamespace(resolve_uploaded_for_inference=AsyncMock())
    with pytest.raises(oa.AttachmentError):
        await prepare(file_ids=ids, upload_service=service)
    service.resolve_uploaded_for_inference.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code", [(404, "file_not_found"), (410, "file_expired"), (409, "file_not_uploaded")]
)
async def test_ownership_lifecycle_errors_are_safe_and_atomic(status, code):
    service = SimpleNamespace(
        resolve_uploaded_for_inference=AsyncMock(
            side_effect=FileUploadError(status, code, "sensitive detail")
        )
    )
    with pytest.raises(oa.AttachmentError) as caught:
        await prepare(upload_service=service)
    assert caught.value.status_code == status
    assert "sensitive" not in str(caught.value)
    assert caught.value.code == "attachment_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(size_bytes=0),
        lambda r: r.update(size_bytes=True),
        lambda r: r.update(content_bytes="https://host/file"),
        lambda r: r.update(user_id=OTHER),
        lambda r: r.update(file_id=IDS[1]),
        lambda r: r.update(mime_type=[]),
    ],
)
async def test_resolver_output_is_revalidated(mutation):
    item = row()
    mutation(item)
    with pytest.raises(oa.AttachmentError):
        await prepare([item], file_ids=[IDS[0]])


@pytest.mark.asyncio
async def test_no_missing_files_silently_ignored():
    service = SimpleNamespace(resolve_uploaded_for_inference=AsyncMock(return_value=[]))
    with pytest.raises(oa.AttachmentError):
        await prepare(upload_service=service)


@pytest.mark.asyncio
async def test_real_upload_service_enforces_owner_status_and_expiry():
    calls = []

    class DB:
        @asynccontextmanager
        async def transaction(self):
            yield self

        async def execute(self, *args):
            pass

        async def fetchrow(self, query, file_id, user_id):
            calls.append((query, file_id, user_id))
            if user_id != OWNER:
                return None
            return {
                **row(),
                "id": IDS[0],
                "user_id": OWNER,
                "status": "uploaded",
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
            }

    service = FileUploadService(DB())
    assert (await prepare(upload_service=service)).blocks
    with pytest.raises(oa.AttachmentError, match="attachment_unavailable"):
        await prepare(user_id=OTHER, upload_service=service)
    assert all("id = $1::uuid AND user_id = $2::uuid" in q for q, *_ in calls)


@pytest.mark.parametrize(
    "payload,mime",
    [
        (b"audio", "audio/wav"),
        (b"video", "video/mp4"),
        (b"zip", "application/zip"),
        (b"<svg/>", "image/svg+xml"),
        (b"GIF89a", "image/gif"),
        (b"binary", "application/octet-stream"),
        (b"text", "text/unknown"),
        (b"valid", "text/plain; charset=latin-1"),
    ],
)
def test_unsupported_types_rejected(payload, mime):
    with pytest.raises(oa.AttachmentError, match="unsupported_attachment_type"):
        oa.validate_attachment_content(content_bytes=payload, mime_type=mime, filename="file")


@pytest.mark.parametrize(
    "payload,mime",
    [
        (b"\xffinvalid", "text/plain"),
        (b"hello\x00", "application/json"),
        (b"%PDF-1.7\n%%EOF", "text/plain"),
        (b"plain text", "image/png"),
        (b"%PDF-1.7\ntruncated", "application/pdf"),
        (b"", "text/plain"),
    ],
)
def test_invalid_bytes_rejected_not_lossily_decoded(payload, mime):
    with pytest.raises(oa.AttachmentError, match="invalid_attachment_content"):
        oa.validate_attachment_content(content_bytes=payload, mime_type=mime, filename="file")


@pytest.mark.parametrize(
    "filename",
    ["../private.txt", "dir/file.txt", "dir\\file.txt", "line\nfile", "\x00", "x" * 256, ".."],
)
def test_filenames_are_not_paths_or_control_channels(filename):
    with pytest.raises(oa.AttachmentError):
        oa.validate_attachment_content(
            content_bytes=b"x", mime_type="text/plain", filename=filename
        )


def test_image_mime_dimensions_corruption_and_animation():
    with pytest.raises(oa.AttachmentError):
        oa.validate_attachment_content(
            content_bytes=picture("PNG"), mime_type="image/jpeg", filename="x.jpg"
        )
    with pytest.raises(oa.AttachmentError, match="attachment_too_large"):
        oa.validate_attachment_content(
            content_bytes=picture(size=(2049, 1)), mime_type="image/png", filename="x.png"
        )
    with pytest.raises(oa.AttachmentError):
        oa.validate_attachment_content(
            content_bytes=picture()[:-8], mime_type="image/png", filename="x.png"
        )
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(
        buf, format="PNG", save_all=True, append_images=[Image.new("RGB", (2, 2), "black")]
    )
    with pytest.raises(oa.AttachmentError):
        oa.validate_attachment_content(
            content_bytes=buf.getvalue(), mime_type="image/png", filename="x.png"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mime", ["image/png", " IMAGE/PNG ", "application/pdf"])
async def test_modality_review_not_inferred_from_model_or_mime_case(mime):
    settings = review()
    settings["attachments"].update(
        input_modalities=["text"], image_tokens=0, pdf_page_tokens=0, pdf_file_overhead_tokens=0
    )
    with pytest.raises(oa.AttachmentError, match="attachment_capability_unreviewed"):
        await prepare([row(picture(), mime)], model_review=settings)


def test_text_limits_do_not_truncate():
    exact = b"x" * oa.MAX_TEXT_BYTES
    assert (
        oa.validate_attachment_content(
            content_bytes=exact, mime_type="text/plain", filename="x"
        ).text
        == exact.decode()
    )
    with pytest.raises(oa.AttachmentError, match="attachment_too_large"):
        oa.validate_attachment_content(
            content_bytes=exact + b"x", mime_type="text/plain", filename="x"
        )


@pytest.mark.asyncio
async def test_aggregate_upload_bytes_bounded_before_decoding(monkeypatch):
    monkeypatch.setattr(oa, "MAX_TOTAL_BYTES", 3)
    with pytest.raises(oa.AttachmentError, match="attachment_too_large"):
        await prepare([row(b"xx"), row(b"xx", index=1)])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pages": 9},
        {"encrypted": True},
        {"javascript": True},
        {"stream": b"X" * (oa.MAX_PDF_DECODED_BYTES + 1)},
    ],
)
def test_pdf_page_encryption_active_content_and_decompression_limits(kwargs, pdf_validator):
    with pytest.raises(oa.AttachmentError, match="invalid_attachment_content"):
        oa.validate_attachment_content(
            content_bytes=pdf(**kwargs), mime_type="application/pdf", filename="x.pdf"
        )


def test_pdf_missing_validator_fails_closed(monkeypatch):
    monkeypatch.setattr(oa.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(oa.AttachmentError, match="pdf_validator_unavailable"):
        oa.validate_attachment_content(
            content_bytes=b"%PDF-1.7\n%%EOF", mime_type="application/pdf", filename="x.pdf"
        )


def test_pdf_timeout_sanitized_and_no_environment_secrets(monkeypatch):
    monkeypatch.setattr(
        oa.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin="/trusted/pypdf/__init__.py"),
    )
    calls = []

    def timed_out(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, 5)

    monkeypatch.setattr(oa.subprocess, "run", timed_out)
    with pytest.raises(oa.AttachmentError, match="invalid_attachment_content"):
        oa.validate_attachment_content(
            content_bytes=b"%PDF-1.7\n%%EOF", mime_type="application/pdf", filename="x.pdf"
        )
    assert calls[0][1]["env"] == {} and calls[0][1]["timeout"] == 5
    assert "-I" in calls[0][0] and "RLIMIT_AS" in calls[0][0][3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c[0]["content"][1]["image_url"].update(url="https://untrusted.invalid/x"),
        lambda c: c[0]["content"][1]["image_url"].update(detail="auto"),
        lambda c: c[0]["content"].append(copy.deepcopy(c[0]["content"][1])),
        lambda c: c[0]["content"].pop(),
        lambda c: c[0].update(role="assistant"),
    ],
)
async def test_bound_rejects_unowned_changed_missing_or_duplicated_media(mutation):
    prepared = await prepare([row(picture(), "image/png", "photo.png")])
    content = messages(prepared)
    mutation(content)
    with pytest.raises(oa.AttachmentError):
        bound(prepared, content)


@pytest.mark.asyncio
async def test_bound_is_exact_at_context_boundary_and_request_size(monkeypatch):
    prepared = await prepare()
    current = bound(prepared)
    settings = review()
    settings["context_window_tokens"] = current + 100
    small_config = {**config(), "max_tokens": 100}
    prepared = await prepare(model_review=settings, model_config=small_config)
    assert bound(prepared) == current
    settings["context_window_tokens"] -= 1
    prepared = await prepare(model_review=settings, model_config=small_config)
    with pytest.raises(oa.AttachmentError, match="attachment_context_exceeded"):
        bound(prepared)
    prepared = await prepare()
    monkeypatch.setattr(oa, "MAX_REQUEST_BYTES", 10)
    with pytest.raises(oa.AttachmentError, match="attachment_too_large"):
        bound(prepared)


@pytest.mark.asyncio
async def test_pdf_file_is_not_accepted_without_owned_preparation():
    prepared = await prepare()
    content = messages(prepared)
    content[0]["content"].append(
        {"type": "file", "file": {"filename": "x.pdf", "file_data": "https://untrusted.invalid/x"}}
    )
    with pytest.raises(oa.AttachmentError, match="unowned_attachment_block"):
        bound(prepared, content)


def test_helpers_can_be_used_without_event_loop():
    item = oa.validate_attachment_content(
        content_bytes=b"all content", mime_type="text/markdown", filename="README.md"
    )
    assert item.text == "all content" and item.pages == 0
    assert asyncio.run(prepare()).blocks
