"""Validate inline native media using the existing bounded upload decoders.

The native host already selected these local bytes. No upload-ID substitution,
URL fetch, format translation, truncation, or OCR fallback is performed here.
"""
import base64
import binascii

from server.services.openrouter_attachments import (
    MAX_ATTACHMENTS,
    MAX_TOTAL_BYTES,
    AttachmentError,
    validate_attachment_content,
)


def validate(body, allowed):
    count = total = 0
    pdf = False

    def visit(value):
        nonlocal count, total, pdf
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            kind = value.get("type")
            if isinstance(kind, str) and kind in {"image", "document", "input_image", "input_file"}:
                modality = "image" if kind in {"image", "input_image"} else "pdf"
                if modality not in allowed:
                    raise ValueError("Media modality not reviewed")
                if kind in {"image", "document"}:
                    source = value.get("source", {})
                    if source.get("type") != "base64":
                        raise ValueError("Use inline media, not remote URLs or stored file IDs")
                    mime, data = source.get("media_type"), source.get("data")
                else:
                    url = value.get("image_url" if kind == "input_image" else "file_data", "")
                    if not isinstance(url, str) or not url.startswith("data:") or ";base64," not in url:
                        raise ValueError("Use inline media, not remote URLs or stored file IDs")
                    mime, data = url[5:].split(";base64,", 1)
                if not isinstance(data, str):
                    raise ValueError("Invalid inline media")
                raw = base64.b64decode(data, validate=True)
                count += 1
                total += len(raw)
                if count > MAX_ATTACHMENTS or total > MAX_TOTAL_BYTES:
                    raise ValueError("Inline media exceeds the bounded request limit")
                if (modality == "pdf" and mime != "application/pdf") or (
                    modality == "image" and mime not in {"image/png", "image/jpeg", "image/webp"}
                ):
                    raise ValueError("Inline media type mismatch")
                validate_attachment_content(content_bytes=raw, mime_type=mime, filename="native-input")
                pdf = pdf or modality == "pdf"
                return
            for item in value.values():
                visit(item)
    try:
        if isinstance(body, dict):
            for key in ("messages", "input"):
                visit(body.get(key))
        else:
            visit(body)
    except (AttachmentError, binascii.Error, TypeError, AttributeError) as exc:
        raise ValueError("Native inline media could not be safely validated") from exc
    return pdf
