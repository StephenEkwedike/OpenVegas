"""Public scope identifies a native generation; it never authorizes dispatch."""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
