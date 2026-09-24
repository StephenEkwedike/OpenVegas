"""Public handoff identities and summaries, never history or dispatch authority.

No service, storage, or provider imports. Availability, ownership, freshness and
confirmation remain server checks. HTTP callers must still use the existing
private validation route wrapper for malformed HTTP/JSON before DTO validation.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope

_INVALID = "Invalid native handoff data. Check the selected options and input limits."
# Wire syntax mirrors managed OpenRouter IDs; it does not imply model approval.
_MODEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ALIAS = re.compile(r"(^|[-_.])(latest|auto|router)([-_.]|$)", re.IGNORECASE)
_ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def _validation_error(title: str) -> ValidationError:
    # hide_input_in_errors alone still exposes inputs through errors()/json(),
    # and unknown field names can themselves contain private customer data.
    return ValidationError.from_exception_data(title, [{
        "type": "value_error", "loc": (), "input": None,
        "ctx": {"error": ValueError(_INVALID)},
    }], hide_input=True)


class _PublicHandoff(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, hide_input_in_errors=True,
        revalidate_instances="always", validate_default=True,
    )

    @model_validator(mode="wrap")
    @classmethod
    def sanitize_validation(cls, value, handler):
        try:
            return handler(value)
        except (ValidationError, ValueError, TypeError, OverflowError):
            raise _validation_error(cls.__name__) from None

    @classmethod
    def model_validate_json(cls, json_data, **kwargs):
        try:
            return super().model_validate_json(json_data, **kwargs)
        except (ValidationError, ValueError, TypeError, OverflowError):
            # JSON syntax failures happen before model validators run.
            raise _validation_error(cls.__name__) from None

    @field_validator("source_scope", "destination_scope", mode="before", check_fields=False)
    @classmethod
    def revalidate_scope(cls, value):
        if value is None:
            return None
        if isinstance(value, NativeInferenceScope):
            value = value.model_dump()
        return NativeInferenceScope.model_validate(value)

    @field_validator("source_ref", mode="before", check_fields=False)
    @classmethod
    def revalidate_source_ref(cls, value):
        if isinstance(value, NativeContinuationRef):
            value = value.model_dump()
        return NativeContinuationRef.model_validate(value)

    @field_validator("idempotency_key", check_fields=False)
    @classmethod
    def bounded_key(cls, value):
        if re.fullmatch(r"[!-~]{1,200}", value) is None:
            raise ValueError(_INVALID)
        return value


class NativeHandoffSelection(_PublicHandoff):
    """Exact requested settings, not a server-reviewed HandoffTarget."""

    provider: Literal["openrouter"] = "openrouter"
    model: str = Field(min_length=1, max_length=256)
    enable_tools: bool = True
    enable_web_search: bool = False
    reasoning_effort: _ReasoningEffort | None = None
    max_tokens: int = Field(ge=1, le=1_000_000)

    @field_validator("provider", mode="before")
    @classmethod
    def known_provider(cls, value):
        if type(value) is not str or value != "openrouter":
            raise ValueError(_INVALID)
        return value

    @field_validator("model")
    @classmethod
    def exact_model(cls, value):
        if (_MODEL.fullmatch(value) is None or value.startswith("openrouter/")
                or _ALIAS.search(value.split("/", 1)[1])):
            raise ValueError(_INVALID)
        return value

    @field_validator("enable_tools")
    @classmethod
    def tools_required(cls, value):
        if value is not True:
            raise ValueError(_INVALID)
        return value


class PrepareNativeHandoff(_PublicHandoff):
    source_scope: NativeInferenceScope
    source_ref: NativeContinuationRef
    selection: NativeHandoffSelection
    idempotency_key: str = Field(min_length=1, max_length=200, repr=False)


class NativeHandoffRef(_PublicHandoff):
    """Inference comparison reference; possession never authorizes a dispatch."""

    handoff_id: str
    handoff_sha256: str = Field(min_length=64, max_length=64)

    @field_validator("handoff_id")
    @classmethod
    def canonical_uuid(cls, value):
        return NativeInferenceScope.canonical_uuid(value)

    @field_validator("handoff_sha256")
    @classmethod
    def canonical_digest(cls, value):
        if re.fullmatch(r"[0-9a-f]{64}", value) is None or value == "0" * 64:
            raise ValueError(_INVALID)
        return value


class ConfirmNativeHandoff(NativeHandoffRef):
    destination_scope: NativeInferenceScope
    idempotency_key: str = Field(min_length=1, max_length=200, repr=False)


class NativeHandoffResponse(NativeHandoffRef):
    """Allowlisted preview/confirmation summary; expired replays stay expired."""

    selection: NativeHandoffSelection
    expires_at: datetime
    task_count: int = Field(ge=1, le=32)
    file_count: int = Field(ge=0, le=12)
    unique_file_count: int = Field(ge=0, le=8)
    observation_count: int = Field(ge=0, le=128)
    destination_scope: NativeInferenceScope | None = None

    @field_validator("expires_at", mode="before")
    @classmethod
    def aware_expiry(cls, value):
        if type(value) is str and len(value) <= 40:
            value = datetime.fromisoformat(value)
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(_INVALID)
        return value.astimezone(UTC)

    @field_validator("unique_file_count")
    @classmethod
    def consistent_file_counts(cls, value, info: ValidationInfo):
        files, tasks = info.data.get("file_count"), info.data.get("task_count")
        if files is not None and tasks is not None and (value > files or files > tasks * value):
            raise ValueError(_INVALID)
        return value
