"""Strict shared native-mutation wire schemas; no server or runtime imports."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openvegas.contracts.native_scope import NativeInferenceScope


class ObservedSource(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    exists: bool
    content_utf8: str | None = Field(default=None, max_length=32768, repr=False)

    @model_validator(mode="after")
    def source_shape(self):
        if self.exists != (self.content_utf8 is not None):
            raise ValueError("Missing and empty source files are distinct.")
        return self


class MutationFence(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    runtime_session_id: str
    expected_run_version: int = Field(ge=0, lt=2**63)
    expected_valid_actions_signature: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", min_length=71, max_length=71)
    idempotency_key: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")

    @field_validator("runtime_session_id")
    @classmethod
    def canonical_uuid(cls, value):
        return NativeInferenceScope.canonical_uuid(value)


class PrepareNativeMutation(MutationFence):
    native_inference_request_id: str
    native_provider_call_id: str = Field(min_length=1, max_length=256)
    observed_source: ObservedSource = Field(repr=False)
    plan_mode: bool = False

    @field_validator("native_inference_request_id")
    @classmethod
    def canonical_request_id(cls, value):
        return NativeInferenceScope.canonical_uuid(value)


class ApproveNativeMutation(MutationFence):
    tool_call_id: str
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)

    @field_validator("tool_call_id")
    @classmethod
    def canonical_tool_id(cls, value):
        return NativeInferenceScope.canonical_uuid(value)
