"""Client-side guard for the opt-in native ownership verification path.

The server remains authoritative. This guard prevents the legacy text/tool loop
from silently issuing an unowned follow-up before native continuation is ready.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID


@dataclass
class NativeGenerationSession:
    _key: str | None = field(default=None, init=False, repr=False)
    _scope: dict | None = field(default=None, init=False, repr=False)

    def reserve(self, *, key: str, scope: dict) -> dict:
        from openvegas.contracts.native_scope import NativeInferenceScope

        validated = NativeInferenceScope.model_validate(scope).model_dump(mode="json")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Native generation requires an explicit command key.")
        if self._key is not None:
            if self._key != key:
                raise ValueError(
                    "Native continuation is not available in this verification mode. "
                    "No further inference was sent."
                )
            if any(validated[name] != self._scope[name]
                   for name in ("run_id", "runtime_session_id")):
                raise ValueError("Native replay cannot change its run or runtime session.")
            # A metadata retry must use the original projection, not a newer one.
            return dict(self._scope)
        self._key, self._scope = key, validated
        return dict(validated)

    def validate_result(self, result: dict) -> dict:
        receipt = result.get("native_generation") if isinstance(result, dict) else None
        if (self._scope is None or not isinstance(receipt, dict)
                or type(receipt.get("scope_version")) is not int or receipt["scope_version"] != 1
                or receipt.get("original_turn_scope_verified") is not True
                or receipt.get("continuation_supported") is not False
                or any(receipt.get(name) != self._scope[name] for name in ("run_id", "runtime_session_id"))):
            raise ValueError("Native generation receipt is missing or mismatched; no follow-up was sent.")
        request_id = receipt.get("inference_request_id")
        try:
            parsed = UUID(request_id)
            if not parsed.int or str(parsed) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Native generation receipt has no valid request identity.") from None
        calls = result.get("tool_calls") or []
        if not isinstance(calls, list) or any(
            not isinstance(call, dict) or call.get("native_inference_request_id") != request_id
            or not isinstance(call.get("provider_call_id"), str) or not call["provider_call_id"]
            for call in calls
        ):
            raise ValueError("Native tool result lost its original generation references.")
        status = result.get("completion_status")
        if (not calls and status != "complete") or (calls and status != "incomplete"):
            raise ValueError("Native response is incomplete or has an invalid completion state; no follow-up was sent.")
        return result
