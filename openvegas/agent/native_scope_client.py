"""Client-side guard for the opt-in native ownership verification path.

The server remains authoritative. This guard prevents the legacy text/tool loop
from silently issuing an unowned follow-up before native continuation is ready.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from uuid import UUID

from openvegas.contracts.native_handoff import (
    NativeHandoffRef,
    NativeHandoffResponse,
    NativeHandoffSelection,
)
from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope


@dataclass
class NativeGenerationSession:
    _key: str | None = field(default=None, init=False, repr=False)
    _scope: dict | None = field(default=None, init=False, repr=False)
    _history_requests: dict = field(default_factory=dict, init=False, repr=False)
    _history_options: dict | None = field(default=None, init=False, repr=False)
    _receipt: dict | None = field(default=None, init=False, repr=False)
    _history_mode: bool = field(default=False, init=False, repr=False)
    _user_text: str | None = field(default=None, init=False, repr=False)
    _handoff_json: str | None = field(default=None, init=False, repr=False)
    _result_unverified: bool = field(default=False, init=False, repr=False)

    @property
    def history_active(self) -> bool:
        return self._history_mode

    @property
    def finalized(self) -> bool:
        return bool(not self._result_unverified and self._receipt
                    and self._receipt.get("continuation_supported") is False)

    @property
    def awaiting_first_dispatch(self) -> bool:
        return self._handoff_json is not None and (self._key is None or (
            self._receipt is None and not self._history_requests[self._key].get("native_continuation")))

    @property
    def prepared_handoff(self) -> bool:
        return self._handoff_json is not None

    @property
    def confirmed_selection(self) -> NativeHandoffSelection | None:
        return NativeHandoffResponse.model_validate_json(self._handoff_json).selection if self._handoff_json else None

    @property
    def handoff_ref(self) -> NativeHandoffRef | None:
        if self._handoff_json is None:
            return None
        confirmed = NativeHandoffResponse.model_validate_json(self._handoff_json)
        return NativeHandoffRef(handoff_id=confirmed.handoff_id, handoff_sha256=confirmed.handoff_sha256)

    @property
    def max_tokens(self) -> int | None:
        return self.confirmed_selection.max_tokens if self._handoff_json else None

    @classmethod
    def from_confirmed_handoff(cls, response: NativeHandoffResponse) -> NativeGenerationSession:
        """Create a distinct session, never reset the source or grant authority."""
        confirmed = NativeHandoffResponse.model_validate(deepcopy(response))
        if confirmed.destination_scope is None:
            raise ValueError("A confirmed destination scope is required.")
        session = cls()
        session._handoff_json = confirmed.model_dump_json()
        session._scope = confirmed.destination_scope.model_dump(mode="json")
        return session

    def handoff_source(self, scope: dict | NativeInferenceScope) -> tuple[NativeInferenceScope, NativeContinuationRef]:
        """Export only the latest final identity with a refreshed projection.

        Projection authenticity/freshness and accepted tool receipts remain
        server checks. This guard can reject regressions, not authenticate SQL.
        """
        from openvegas.contracts.native_scope import validate_native_user_text

        try:
            validate_native_user_text(self._user_text)
            checked = NativeInferenceScope.model_validate(
                scope.model_dump() if isinstance(scope, NativeInferenceScope) else scope)
            if (not self._history_mode or not self.finalized or self._scope is None
                    or any(getattr(checked, name) != self._scope[name]
                           for name in ("run_id", "runtime_session_id"))
                    or checked.expected_run_version < self._scope["expected_run_version"]):
                raise ValueError
            ref = NativeContinuationRef(previous_inference_request_id=self._receipt["inference_request_id"],
                                        expected_history_revision=self._receipt["history_revision"])
            return checked, ref
        except (ValueError, TypeError, KeyError):
            raise ValueError("A verified final native task and matching fresh scope are required.") from None

    def prepare(self, *, key: str, scope: dict, options: dict, history: bool = False,
                user_text: str | None = None) -> dict:
        """Freeze one command; only an acknowledged native result opens the next."""
        from openvegas.contracts.native_scope import NativeInferenceScope, validate_native_user_text

        confirmed = NativeHandoffResponse.model_validate_json(self._handoff_json) if self._handoff_json else None
        if confirmed is not None:
            if history is not True or user_text is None or type(options) is not dict:
                raise ValueError("Confirmed handoff requires native history and exact destination settings.")
            options = deepcopy(options)
            selected = confirmed.selection.model_dump()
            options.setdefault("max_tokens", selected["max_tokens"])
            if (set(options) != set(selected) | {"attachments"}
                    or any(type(options[name]) is not type(value) or options[name] != value
                           for name, value in selected.items())
                    or type(options["attachments"]) is not list or len(options["attachments"]) > 8):
                raise ValueError("Confirmed handoff settings cannot change.")
            try:
                for ident in options["attachments"]:
                    NativeInferenceScope.canonical_uuid(ident)
            except (ValueError, TypeError, AttributeError):
                raise ValueError("Confirmed handoff requires exact owned-file identities.") from None
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
        if confirmed is not None and self._key is None and validated != self._scope:
            raise ValueError("First dispatch must retain the exact confirmed destination scope.")
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
        if confirmed is not None:
            context.update(native_handoff={"handoff_id": confirmed.handoff_id,
                "handoff_sha256": confirmed.handoff_sha256}, max_tokens=confirmed.selection.max_tokens)
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
        self._result_unverified = True
        return deepcopy(context)

    def reserve(self, *, key: str, scope: dict) -> dict:
        from openvegas.contracts.native_scope import NativeInferenceScope

        if self._handoff_json is not None:
            raise ValueError("Confirmed handoff cannot fall back to an unbound generation.")
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
        self._result_unverified = True
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
        self._result_unverified = False
        return result
