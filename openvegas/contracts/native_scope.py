"""Public scope identifies a native generation; it never authorizes dispatch."""
from __future__ import annotations

import unicodedata
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def validate_native_user_text(value: object) -> str:
    """Original current input only, not a client-supplied history document."""
    from openvegas.gateway.conversation import _SECRET

    try:
        if (type(value) is not str or not value.strip() or len(value) > 64_000
                or len(value.encode("utf-8")) > 64_000 or _SECRET.search(value)
                or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} and c not in "\n\r\t"
                       for c in value)):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("Native user input must be bounded public text without secrets or controls.") from None
    return value


class NativeInferenceScope(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    run_id: str
    runtime_session_id: str
    expected_run_version: int = Field(ge=0, lt=2**63)
    expected_valid_actions_signature: str = Field(min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("run_id", "runtime_session_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        parsed = UUID(value)
        if len(value) != 36 or not parsed.int or str(parsed) != value:
            raise ValueError("Scope requires a canonical nonzero UUID.")
        return value


def validate_native_scope(value: object) -> NativeInferenceScope:
    return NativeInferenceScope.model_validate(value)


class NativeContinuationRef(BaseModel):
    """A comparison fence, never caller-supplied conversation history."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    previous_inference_request_id: str
    expected_history_revision: int = Field(ge=0, lt=2**63 - 1)

    @field_validator("previous_inference_request_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        return NativeInferenceScope.canonical_uuid(value)
