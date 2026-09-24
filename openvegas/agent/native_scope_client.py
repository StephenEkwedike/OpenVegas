"""Client-side guard for the opt-in native ownership verification path.

The server remains authoritative. This guard prevents the legacy text/tool loop
from silently issuing an unowned follow-up before native continuation is ready.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from uuid import UUID


@dataclass
class NativeGenerationSession:
    _key: str | None = field(default=None, init=False, repr=False)
    _scope: dict | None = field(default=None, init=False, repr=False)
    _history_requests: dict = field(default_factory=dict, init=False, repr=False)
    _history_options: dict | None = field(default=None, init=False, repr=False)
    _receipt: dict | None = field(default=None, init=False, repr=False)
    _history_mode: bool = field(default=False, init=False, repr=False)
    _user_text: str | None = field(default=None, init=False, repr=False)

    @property
    def history_active(self) -> bool:
        return self._history_mode

    @property
    def finalized(self) -> bool:
        return bool(self._receipt and self._receipt.get("continuation_supported") is False)

    def prepare(self, *, key: str, scope: dict, options: dict, history: bool = False,
                user_text: str | None = None) -> dict:
        """Freeze one command; only an acknowledged native result opens the next."""
        from openvegas.contracts.native_scope import NativeInferenceScope, validate_native_user_text

        if user_text is not None:
            validate_native_user_text(user_text)
            if not history:
                raise ValueError("Original input requires native history.")
        if self._key is not None and self._user_text != user_text:
            raise ValueError("Native continuation cannot change its original user input.")

        if not history:
            if self._history_mode:
                raise ValueError("Native history cannot fall back to an unowned request.")
            return {"native_scope": self.reserve(key=key, scope=scope)}
        validated = NativeInferenceScope.model_validate(scope).model_dump(mode="json")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Native generation requires an explicit command key.")
        if self._key is not None and not self._history_mode:
            raise ValueError("Native history cannot be enabled midway through a generation.")
        if self._scope is not None and any(validated[n] != self._scope[n]
                                           for n in ("run_id", "runtime_session_id")):
            raise ValueError("Native continuation cannot change its run or runtime session.")
        if self._history_options is not None and self._history_options != options:
            raise ValueError("Native continuation cannot change model, files, web or reasoning settings.")
        if key in self._history_requests:
            if key != self._key:
                raise ValueError("An older native command cannot replace the current generation.")
            return deepcopy(self._history_requests[key])
        context = {"native_scope": validated, "native_history": True}
        if self._key is None and user_text is not None:
            context["native_user_text"] = user_text
        if self._key is not None:
            if not self._receipt or self._receipt.get("continuation_supported") is not True:
                raise ValueError("Native result is final, incomplete or unconfirmed; no further inference was sent.")
            context["native_continuation"] = {
                "previous_inference_request_id": self._receipt["inference_request_id"],
                "expected_history_revision": self._receipt["history_revision"],
            }
        if len(self._history_requests) >= 64:
            raise ValueError("Native conversation reached its command bound; no request was sent.")
        self._history_requests[key] = deepcopy(context)
        self._history_options = deepcopy(options)
        self._user_text = user_text
        self._key, self._scope, self._receipt, self._history_mode = key, validated, None, True
        return deepcopy(context)

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
                or (not self._history_mode and receipt.get("continuation_supported") is not False)
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
        if self._history_mode:
            command = self._history_requests[self._key]
            prior = command.get("native_continuation")
            revision = prior["expected_history_revision"] + 1 if prior else 0
            ids = [call["provider_call_id"] for call in calls]
            if (calls and receipt.get("continuation_supported") is False
                    and receipt.get("continuation_block_reason") == "native_history_numeric_precision"):
                raise ValueError("This response contains vendor numeric state that cannot be continued without precision loss. No tool or follow-up request was executed.")
            if (type(receipt.get("history_revision")) is not int
                    or receipt["history_revision"] != revision
                    or receipt.get("continuation_supported") is not bool(calls)
                    or len(set(ids)) != len(ids)):
                raise ValueError("Native history receipt is incomplete or mismatched; no continuation was sent.")
            if self._receipt is not None and self._receipt != receipt:
                raise ValueError("Native replay changed its original generation receipt.")
            self._receipt = deepcopy(receipt)
        return result
