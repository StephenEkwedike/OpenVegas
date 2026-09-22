"""Owned-upload normalization, not an attachment feature flag or a transport.

Integration (all review/config/service arguments MUST be server-owned):
1. Keep catalog/account/capability preflight; get a fresh exact-model review.
2. Call prepare_owned_attachment_blocks with authenticated user_id and upload IDs.
3. Append result.blocks after the prompt in a user content array. Never use the
   old attachment preview/fallback, and never accept client-supplied blocks.
4. Apply result.request_options without weakening provider restrictions or
   adding plugins. Calculate the FULL request bound with
   calculate_request_token_bound(messages, prepared=result, tools=actual_tools).
   Use that bound for context preflight, reservation AND response metering.
5. Reauthorize upload IDs and revalidate the review on every replay. This module
   does not certify persistence, tool history, provider availability or billing.

The operator review extends the existing model review with an ``attachments``
object; see validate_attachment_review for its closed schema. No model IDs,
prices or media token estimates are bundled. A review must establish upper
bounds, including the endpoint's preprocessing, not average observed usage.
In particular pdf_page_tokens includes ALL extracted text and page rendering;
image_tokens covers the largest permitted image at detail=high. If these cannot
be established, do not approve that modality. Decoded PDF stream bytes are also
charged conservatively in the local bound, not mistaken for visual tokens.

Limits are deliberately smaller than the upload service's generic limits.
Images need Pillow (an existing dependency). PDFs additionally need pypdf and a
POSIX resource-limited subprocess; missing support rejects PDF explicitly.
Linux supports this worker; macOS currently refuses its address-space limit and
must not advertise native-PDF availability merely because pypdf is installed.
Nothing reads credentials, performs HTTP, extracts a preview, or enables models.

Official contracts checked 2026-09-21:
https://openrouter.ai/docs/guides/overview/multimodal/image-understanding
https://openrouter.ai/docs/guides/overview/multimodal/pdfs
PDF defaults may invoke paid OCR. Always send engine=native, with one reviewed
endpoint, no fallbacks, and a review of token-only pricing and zero extra fees.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import math
import re
import subprocess
import sys
import warnings
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

MAX_ATTACHMENTS = 3
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 512 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_IMAGE_SIDE = 2048
MAX_IMAGE_PIXELS = 4_000_000
MAX_PDF_PAGES = 8
MAX_PDF_SIDE_POINTS = 2000
MAX_PDF_DECODED_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 1_000_000
MAX_TOKENS = 10_000_000
PDF_TIMEOUT_SECONDS = 5
TEXT_MIMES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/tab-separated-values",
        "text/x-python",
        "text/javascript",
        "text/html",
        "text/css",
        "text/xml",
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-yaml",
        "text/yaml",
    }
)
IMAGE_MIMES = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
_MODEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_PRICE_FIELDS = (
    "cost_input_per_1m",
    "cost_output_per_1m",
    "v_price_input_per_1m",
    "v_price_output_per_1m",
)
_FEE_FIELDS = frozenset(
    {
        "request",
        "image",
        "web_search",
        "audio",
        "input_audio",
        "output_audio",
        "input_audio_cache",
        "internal_reasoning",
        "input_cache_read",
        "input_cache_write",
        "input_cache_write_1h",
    }
)


@dataclass
class AttachmentError(ValueError):
    """Fixed customer-safe diagnostics; no file contents or parser errors."""

    code: str
    detail: str
    status_code: int = 400

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


class OwnedUploadResolver(Protocol):
    async def resolve_uploaded_for_inference(
        self, *, user_id: str, file_ids: list[str]
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class AttachmentReview:
    model_id: str
    provider: str
    modalities: frozenset[str]
    context_window_tokens: int
    max_output_tokens: int
    request_overhead_tokens: int
    message_overhead_tokens: int
    part_overhead_tokens: int
    image_tokens: int
    pdf_page_tokens: int
    pdf_file_overhead_tokens: int
    input_price_per_1m: Decimal
    output_price_per_1m: Decimal
    expires_at: datetime


@dataclass(frozen=True)
class ValidatedAttachment:
    """Content validation alone is NOT ownership authorization."""

    filename: str
    mime_type: str
    content_bytes: bytes = field(repr=False)
    text: str | None = field(default=None, repr=False)
    pages: int = 0
    decoded_pdf_bytes: int = 0


@dataclass(frozen=True)
class PreparedAttachments:
    user_id: str
    file_ids: tuple[str, ...]
    review: AttachmentReview
    media_tokens: int
    _blocks_json: str = field(repr=False)

    @property
    def blocks(self) -> list[dict[str, Any]]:
        """Return a defensive copy, not mutable authorization/bound state."""
        return json.loads(self._blocks_json)

    @property
    def request_options(self) -> dict[str, Any]:
        has_pdf = any(block["type"] == "file" for block in self.blocks)
        return {
            "transforms": [],
            "plugins": [{"id": "file-parser", "pdf": {"engine": "native"}}] if has_pdf else [],
            "provider": {
                "only": [self.review.provider],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "zdr": True,
                "max_price": {
                    "prompt": float(self.review.input_price_per_1m),
                    "completion": float(self.review.output_price_per_1m),
                    "request": 0,
                    "image": 0,
                },
            },
        }


def _reject_review() -> None:
    raise AttachmentError(
        "attachment_review_required",
        "Attachments require a current exact-model, endpoint, capability and token-only pricing review.",
        503,
    )


def _integer(value: object, minimum: int = 1, maximum: int = MAX_TOKENS) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _reject_review()
    return value


def _price(value: object) -> Decimal:
    if not isinstance(value, (str, Decimal)) or len(str(value)) > 80:
        _reject_review()
    try:
        result = Decimal(value)
        if not result.is_finite() or not 0 <= result <= 1_000_000:
            _reject_review()
        return result
    except InvalidOperation:
        _reject_review()


def validate_attachment_review(
    *, model_id: str, model_config: dict, model_review: dict, now: datetime | None = None
) -> AttachmentReview:
    """Validate explicit operator settings. Old text-only reviews cannot opt in.

    Required attachments keys: schema_version=1, model_id, provider (one exact
    endpoint slug), input_modalities (text plus optional image/file),
    pricing_policy='input_tokens_only', no_additional_fees=True,
    non_token_fees={image/native_pdf/plugins/request: '0'},
    token_bound_policy='utf8_bytes_plus_reviewed_media_v1', token_bound_basis
    (nonempty audit reference/explanation), and all six *_tokens fields below.
    image/pdf overhead must be positive iff that modality is reviewed; disabled
    modalities use explicit zero. The parent owns review creation/installation.

    Top-level observed_pricing uses USD/token (as in the public API), must
    explicitly include prompt/completion/request/image, and may only contain
    known, bounded token fees. Missing image/request prices are NOT zero.
    no_additional_fees is the endpoint-specific attestation for fees the public
    model listing may omit, including plugins and automatic cache writes.
    """
    if not isinstance(model_config, dict) or not isinstance(model_review, dict):
        _reject_review()
    if (
        not isinstance(model_id, str)
        or not _MODEL.fullmatch(model_id)
        or model_id.startswith("openrouter/")
        or re.search(
            r"(^|[-_.])(auto|latest|router)([-_.]|$)", model_id.split("/")[1], re.IGNORECASE
        )
        or model_config.get("provider") != "openrouter"
        or model_config.get("model_id") != model_id
        or model_config.get("enabled") is not True
        or model_review.get("account_access") is not True
        or model_review.get("completion_chat") is not True
    ):
        _reject_review()
    current = now if now is not None else datetime.now(UTC)
    try:
        reviewed = datetime.fromisoformat(model_review["reviewed_at"])
        expires = datetime.fromisoformat(model_review["expires_at"])
        if (
            current.utcoffset() is None
            or reviewed.utcoffset() is None
            or expires.utcoffset() is None
            or not reviewed <= current < expires
            or expires - reviewed > timedelta(days=30)
        ):
            _reject_review()
    except (KeyError, TypeError, ValueError, AttributeError):
        _reject_review()
    for name in _PRICE_FIELDS:
        if _price(model_config.get(name)) != _price(model_review.get(name)):
            _reject_review()
    context = _integer(model_review.get("context_window_tokens"))
    output = _integer(model_config.get("max_tokens"))
    if output > _integer(model_review.get("max_tokens")) or output >= context:
        _reject_review()
    policy = model_review.get("attachments")
    fields = {
        "schema_version",
        "model_id",
        "provider",
        "input_modalities",
        "pricing_policy",
        "no_additional_fees",
        "non_token_fees",
        "token_bound_policy",
        "token_bound_basis",
        "request_overhead_tokens",
        "message_overhead_tokens",
        "part_overhead_tokens",
        "image_tokens",
        "pdf_page_tokens",
        "pdf_file_overhead_tokens",
    }
    if not isinstance(policy, dict) or set(policy) != fields:
        _reject_review()
    if (
        type(policy["schema_version"]) is not int
        or policy["schema_version"] != 1
        or policy["model_id"] != model_id
        or policy["pricing_policy"] != "input_tokens_only"
        or policy["no_additional_fees"] is not True
        or policy["token_bound_policy"] != "utf8_bytes_plus_reviewed_media_v1"
        or not isinstance(policy["token_bound_basis"], str)
        or not 20 <= len(policy["token_bound_basis"].strip()) <= 2000
        or not isinstance(policy["provider"], str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._/-]{0,127}", policy["provider"])
    ):
        _reject_review()
    fees = policy["non_token_fees"]
    if not isinstance(fees, dict) or set(fees) != {"image", "native_pdf", "plugins", "request"}:
        _reject_review()
    if any(_price(amount) != 0 for amount in fees.values()):
        _reject_review()
    modalities = policy["input_modalities"]
    if (
        not isinstance(modalities, list)
        or not 1 <= len(modalities) <= 3
        or any(not isinstance(v, str) for v in modalities)
        or len(set(modalities)) != len(modalities)
        or "text" not in modalities
        or set(modalities) - {"text", "image", "file"}
    ):
        _reject_review()
    image_tokens = _integer(policy["image_tokens"], 1 if "image" in modalities else 0)
    pdf_page = _integer(policy["pdf_page_tokens"], 1 if "file" in modalities else 0)
    pdf_file = _integer(policy["pdf_file_overhead_tokens"], 1 if "file" in modalities else 0)
    if ("image" not in modalities and image_tokens) or (
        "file" not in modalities and (pdf_page or pdf_file)
    ):
        _reject_review()
    observed = model_review.get("observed_pricing")
    if (
        not isinstance(observed, dict)
        or not {"prompt", "completion", "request", "image"} <= observed.keys()
        or observed.keys() - (_FEE_FIELDS | {"prompt", "completion", "overrides"})
        or ("overrides" in observed and observed["overrides"] != [])
    ):
        _reject_review()
    for source, stored in (("prompt", "cost_input_per_1m"), ("completion", "cost_output_per_1m")):
        if _price(observed[source]) * 1_000_000 != _price(model_config[stored]):
            _reject_review()
    for fee in observed.keys() & _FEE_FIELDS:
        value = _price(observed[fee])
        ceiling = (
            _price(observed["prompt"])
            if fee == "input_cache_read"
            else _price(observed["completion"])
            if fee == "internal_reasoning"
            else Decimal(0)
        )
        if value > ceiling:
            _reject_review()
    return AttachmentReview(
        model_id=model_id,
        provider=policy["provider"],
        modalities=frozenset(modalities),
        context_window_tokens=context,
        max_output_tokens=output,
        request_overhead_tokens=_integer(policy["request_overhead_tokens"], 256),
        message_overhead_tokens=_integer(policy["message_overhead_tokens"], 32),
        part_overhead_tokens=_integer(policy["part_overhead_tokens"], 32),
        image_tokens=image_tokens,
        pdf_page_tokens=pdf_page,
        pdf_file_overhead_tokens=pdf_file,
        input_price_per_1m=_price(model_config["cost_input_per_1m"]),
        output_price_per_1m=_price(model_config["cost_output_per_1m"]),
        expires_at=expires,
    )


def _invalid_content() -> None:
    raise AttachmentError("invalid_attachment_content", "Attachment content is invalid or unsafe.")


def _too_large() -> None:
    raise AttachmentError(
        "attachment_too_large", "Attachment exceeds the supported content limits.", 413
    )


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        _invalid_content()


def _utf8(value: str) -> bytes:
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeError:
        _invalid_content()


def validate_attachment_content(
    *, content_bytes: bytes, mime_type: str, filename: str
) -> ValidatedAttachment:
    """Validate all bytes, without truncation, MIME guessing, paths or fetching."""
    if (
        not isinstance(content_bytes, bytes)
        or not content_bytes
        or not isinstance(filename, str)
        or not filename
        or not filename.isprintable()
        or len(_utf8(filename)) > 255
        or "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
        or not isinstance(mime_type, str)
    ):
        _invalid_content()
    if len(content_bytes) > MAX_FILE_BYTES:
        _too_large()
    mime = mime_type.lower().strip()
    if mime in TEXT_MIMES:
        if len(content_bytes) > MAX_TEXT_BYTES:
            _too_large()
        # An ASCII PDF must not be laundered into a text attachment.
        if content_bytes.lstrip().startswith((b"%PDF-", b"%!PS", b"PK\x03\x04")):
            _invalid_content()
        try:
            text = content_bytes.decode("utf-8", errors="strict")
        except UnicodeError:
            _invalid_content()
        if any((ord(ch) < 32 and ch not in "\t\r\n") or 0x7F <= ord(ch) < 0xA0 for ch in text):
            _invalid_content()
        return ValidatedAttachment(filename, mime, content_bytes, text=text)
    if mime in IMAGE_MIMES:
        try:
            from PIL import Image
        except ImportError:
            raise AttachmentError(
                "image_validator_unavailable", "Image validation is unavailable.", 503
            ) from None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content_bytes)) as image:
                    if image.format != IMAGE_MIMES[mime] or getattr(image, "n_frames", 1) != 1:
                        _invalid_content()
                    width, height = image.size
                    if max(width, height) > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
                        _too_large()
                    image.verify()
                with Image.open(io.BytesIO(content_bytes)) as image:
                    image.load()
        except AttachmentError:
            raise
        except Exception:  # noqa: BLE001 - Untrusted decoder diagnostics must not leak.
            _invalid_content()
        return ValidatedAttachment(filename, mime, content_bytes)
    if mime == "application/pdf":
        if not re.match(
            rb"%PDF-(1\.[0-7]|2\.0)[\r\n]", content_bytes
        ) or not content_bytes.rstrip().endswith(b"%%EOF"):
            _invalid_content()
        pages, decoded = _inspect_pdf(content_bytes)
        return ValidatedAttachment(
            filename, mime, content_bytes, pages=pages, decoded_pdf_bytes=decoded
        )
    raise AttachmentError(
        "unsupported_attachment_type",
        "Supported attachments are bounded UTF-8 text, static PNG/JPEG/WebP, and reviewed native PDFs.",
        415,
    )


def _inspect_pdf(payload: bytes) -> tuple[int, int]:
    """Parse untrusted PDFs outside the server with CPU, memory and wall limits."""
    spec = importlib.util.find_spec("pypdf")
    if spec is None or spec.origin is None or sys.platform == "win32":
        raise AttachmentError(
            "pdf_validator_unavailable", "Native PDF validation is unavailable.", 503
        )
    script = (
        "import sys,resource,runpy; sys.dont_write_bytecode=True\n"
        "try:\n"
        " resource.setrlimit(resource.RLIMIT_CPU,(2,2))\n"
        " resource.setrlimit(resource.RLIMIT_AS,(536870912,536870912))\n"
        " resource.setrlimit(resource.RLIMIT_FSIZE,(0,0))\n"
        "except (OSError,ValueError): sys.exit(3)\n"
        "sys.path.insert(0,sys.argv[1]); "
        "runpy.run_path(sys.argv[2])['_pdf_worker']()"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, str(Path(spec.origin).parent.parent), __file__],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=PDF_TIMEOUT_SECONDS,
            check=False,
            env={},
        )
        if result.returncode == 3:
            raise AttachmentError(
                "pdf_validator_unavailable", "Native PDF resource isolation is unavailable.", 503
            )
        if result.returncode != 0 or len(result.stdout) > 128:
            _invalid_content()
        pages, decoded = json.loads(result.stdout)
        if type(pages) is not int or type(decoded) is not int:
            _invalid_content()
        if not 1 <= pages <= MAX_PDF_PAGES or not 0 <= decoded <= MAX_PDF_DECODED_BYTES:
            _too_large()
        return pages, decoded
    except AttachmentError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        _invalid_content()


def _pdf_worker() -> None:
    """Only invoked by the isolated local validator. No secret-bearing env."""
    try:
        from pypdf import PdfReader
        from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, StreamObject

        payload = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
        if len(payload) > MAX_FILE_BYTES:
            raise ValueError
        reader = PdfReader(io.BytesIO(payload), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
            raise ValueError
        for page in reader.pages:
            if page.get("/UserUnit", 1) != 1:
                raise ValueError
            for box in (page.mediabox, page.cropbox):
                dimensions = [float(box.width), float(box.height)]
                if any(
                    not math.isfinite(n) or not 0 < n <= MAX_PDF_SIDE_POINTS for n in dimensions
                ):
                    raise ValueError
        forbidden = {
            "/AA",
            "/OpenAction",
            "/JS",
            "/JavaScript",
            "/Launch",
            "/EmbeddedFiles",
            "/EmbeddedFile",
            "/XFA",
            "/RichMedia",
            "/AcroForm",
            "/GoToR",
            "/URI",
            "/SubmitForm",
            "/ImportData",
            "/Sound",
            "/Movie",
            "/EF",
            "/Ref",
        }
        pending = [(reader.trailer, 0)]
        seen = set()
        decoded = visited = 0
        while pending:
            obj, depth = pending.pop()
            visited += 1
            if visited > 20_000 or depth > 64:
                raise ValueError
            if isinstance(obj, IndirectObject):
                key = (obj.idnum, obj.generation)
                if key in seen:
                    continue
                seen.add(key)
                obj = obj.get_object()
            if isinstance(obj, StreamObject):
                # External stream references must never become outbound fetches.
                if "/F" in obj or "/FFilter" in obj or "/FDecodeParms" in obj:
                    raise ValueError
                decoded += len(obj.get_data())
                if decoded > MAX_PDF_DECODED_BYTES:
                    raise ValueError
            if isinstance(obj, DictionaryObject):
                if forbidden.intersection(obj) or str(obj.get("/S", "")) in forbidden:
                    raise ValueError
                if str(obj.get("/Type", "")) == "/EmbeddedFile":
                    raise ValueError
                pending.extend((v, depth + 1) for v in obj.values())
            elif isinstance(obj, ArrayObject):
                pending.extend((v, depth + 1) for v in obj)
        print(json.dumps([len(reader.pages), decoded]))
    except Exception:  # noqa: BLE001 - The child returns no untrusted parser diagnostics.
        raise SystemExit(1) from None


def _uuid(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise AttachmentError(
            "invalid_upload_id", "Use canonical user-owned upload IDs, not URLs or paths."
        )
    try:
        if str(UUID(value)) != value or UUID(value).int == 0:
            raise ValueError
    except ValueError:
        raise AttachmentError(
            "invalid_upload_id", "Use canonical user-owned upload IDs, not URLs or paths."
        ) from None
    return value


async def prepare_owned_attachment_blocks(
    *,
    user_id: str,
    file_ids: list[str],
    model_id: str,
    model_config: dict,
    model_review: dict,
    upload_service: OwnedUploadResolver,
) -> PreparedAttachments:
    """The sole authorization entry point: never accepts raw files/URLs from a request.

    Inject the real FileUploadService, not a client-provided resolver. Its query
    enforces ownership, uploaded status and expiry. All-or-nothing: no skipped
    files, deduplication, previews or partial provider payloads on failure.
    """
    review = validate_attachment_review(
        model_id=model_id, model_config=model_config, model_review=model_review
    )
    owner = _uuid(user_id)
    if not isinstance(file_ids, list) or not 1 <= len(file_ids) <= MAX_ATTACHMENTS:
        raise AttachmentError("invalid_attachment_count", "Provide one to three unique upload IDs.")
    ids = [_uuid(value) for value in file_ids]
    if len(set(ids)) != len(ids):
        raise AttachmentError("duplicate_attachment", "Duplicate upload IDs are not accepted.")
    from server.services.file_uploads import FileUploadError

    try:
        rows = await upload_service.resolve_uploaded_for_inference(user_id=owner, file_ids=ids)
    except FileUploadError as exc:
        raise AttachmentError(
            "attachment_unavailable",
            "An upload is unavailable, expired, incomplete or not owned by you.",
            exc.status_code if exc.status_code in {400, 404, 409, 410, 413} else 400,
        ) from None
    if (
        not isinstance(rows, list)
        or len(rows) != len(ids)
        or any(not isinstance(row, dict) for row in rows)
        or [row.get("file_id") for row in rows] != ids
    ):
        _invalid_content()
    total = 0
    for row in rows:
        payload = row.get("content_bytes")
        if (
            not isinstance(payload, (bytes, bytearray, memoryview))
            or type(row.get("size_bytes")) is not int
            or row["size_bytes"]
            != (payload.nbytes if isinstance(payload, memoryview) else len(payload))
            or ("user_id" in row and row["user_id"] != owner)
        ):
            _invalid_content()
        total += row["size_bytes"]
    if total > MAX_TOTAL_BYTES:
        _too_large()
    blocks: list[dict[str, Any]] = []
    media_tokens = 0
    for row in rows:
        mime = row.get("mime_type")
        if not isinstance(mime, str):
            _invalid_content()
        mime = mime.strip().lower()
        modality = (
            "image" if mime in IMAGE_MIMES else "file" if mime == "application/pdf" else "text"
        )
        if modality not in review.modalities:
            raise AttachmentError(
                "attachment_capability_unreviewed",
                "This attachment modality is not reviewed for the selected endpoint.",
            )
        item = await asyncio.to_thread(
            validate_attachment_content,
            content_bytes=bytes(row["content_bytes"]),
            mime_type=mime,
            filename=row.get("filename"),
        )
        if item.text is not None:
            blocks.append(
                {
                    "type": "text",
                    "text": f"Attachment [{item.filename}] ({item.mime_type})\n{item.text}",
                }
            )
        else:
            data = f"data:{item.mime_type};base64,{base64.b64encode(item.content_bytes).decode('ascii')}"
            if item.pages:
                blocks.append(
                    {"type": "file", "file": {"filename": item.filename, "file_data": data}}
                )
                media_tokens += (
                    review.pdf_file_overhead_tokens
                    + item.pages * review.pdf_page_tokens
                    + item.decoded_pdf_bytes
                )
            else:
                blocks.append({"type": "image_url", "image_url": {"url": data, "detail": "high"}})
                media_tokens += review.image_tokens
    return PreparedAttachments(owner, tuple(ids), review, media_tokens, _json(blocks))


def calculate_request_token_bound(
    messages: list[dict[str, Any]],
    *,
    prepared: PreparedAttachments,
    max_output_tokens: int,
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Bound full history, schemas, filenames, UTF-8 text and reviewed media.

    Only exact prepared blocks are accepted as nontext content, each exactly
    once. Caller must preserve this payload and options until dispatch. This is
    a reservation ceiling, not a tokenizer estimate; never substitute it for
    actual usage at settlement. Repeated/new history attachments require fresh
    owned preparation, not cached authorization or client-supplied data URLs.
    """
    review = prepared.review
    if datetime.now(UTC) >= review.expires_at:
        _reject_review()
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= review.max_output_tokens:
        raise AttachmentError(
            "attachment_context_exceeded", "Output budget exceeds its reviewed limit."
        )
    if not isinstance(messages, list) or not 1 <= len(messages) <= 200:
        _invalid_content()
    expected = Counter(_json(block) for block in prepared.blocks)
    actual = Counter()
    text_messages = []
    parts = 0
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or not isinstance(message["role"], str)
            or message["role"] not in {"system", "user", "assistant"}
        ):
            _invalid_content()
        content = message["content"]
        if isinstance(content, str):
            text_messages.append(message)
            continue
        if (
            message["role"] != "user"
            or not isinstance(content, list)
            or not 1 <= len(content) <= 16
        ):
            _invalid_content()
        stripped = []
        for block in content:
            if not isinstance(block, dict):
                _invalid_content()
            key = _json(block)
            if key in expected:
                actual[key] += 1
            kind = block.get("type")
            if kind == "text" and set(block) == {"type", "text"} and isinstance(block["text"], str):
                stripped.append(block)
            elif key in expected and kind == "image_url":
                stripped.append({"type": "image_url", "image_url": {"url": "", "detail": "high"}})
            elif key in expected and kind == "file":
                stripped.append(
                    {
                        "type": "file",
                        "file": {"filename": block["file"]["filename"], "file_data": ""},
                    }
                )
            else:
                raise AttachmentError(
                    "unowned_attachment_block",
                    "Only freshly authorized upload content may be sent.",
                )
            parts += 1
        text_messages.append({"role": message["role"], "content": stripped})
    if actual != expected:
        raise AttachmentError(
            "attachment_blocks_changed", "Prepared attachment blocks must be included exactly once."
        )
    if tools is not None and (
        not isinstance(tools, list)
        or len(tools) > 16
        or any(not isinstance(t, dict) for t in tools)
    ):
        _invalid_content()
    actual_request = {
        "messages": messages,
        "model": review.model_id,
        "max_tokens": max_output_tokens,
        "stream": False,
        **prepared.request_options,
    }
    accounted_request = {**actual_request, "messages": text_messages}
    if tools:
        actual_request.update(tools=tools, tool_choice="auto")
        accounted_request.update(tools=tools, tool_choice="auto")
    try:
        if len(_utf8(_json(actual_request))) > MAX_REQUEST_BYTES:
            _too_large()
        bound = (
            len(_utf8(_json(accounted_request)))
            + prepared.media_tokens
            + review.request_overhead_tokens
            + len(messages) * review.message_overhead_tokens
            + parts * review.part_overhead_tokens
        )
    except AttachmentError:
        raise
    except (ValueError, TypeError, RecursionError):
        _invalid_content()
    if bound + max_output_tokens > review.context_window_tokens:
        raise AttachmentError(
            "attachment_context_exceeded",
            "Full multimodal request exceeds the reviewed context; nothing was truncated.",
            413,
        )
    return bound
