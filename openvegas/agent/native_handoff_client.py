"""In-memory public handoff operation; no dispatch or durable restart recovery.

The CLI retains its source/selection until adopt(), and must honor
blocks_other_actions. A destination callback creates/registers an owned fresh
run without changing active CLI state. The server authenticates that scope.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from openvegas.agent.native_scope_client import NativeGenerationSession
from openvegas.contracts.native_handoff import (
    ConfirmNativeHandoff,
    NativeHandoffResponse,
    NativeHandoffSelection,
    PrepareNativeHandoff,
)
from openvegas.contracts.native_scope import NativeInferenceScope


class PendingHandoffError(ValueError):
    """Allowlisted diagnostics without submitted bodies or service errors."""


class PendingNativeHandoff:
    def __init__(self, *, source_session: NativeGenerationSession,
                 source_scope: NativeInferenceScope | dict,
                 selection: NativeHandoffSelection | dict,
                 old_selection: NativeHandoffSelection | dict,
                 prepare_key: str, confirm_key: str):
        checked_scope, ref = source_session.handoff_source(source_scope)
        request = PrepareNativeHandoff(source_scope=checked_scope, source_ref=ref,
                                       selection=selection, idempotency_key=prepare_key)
        old = NativeHandoffSelection.model_validate(old_selection)
        if type(confirm_key) is not str or re.fullmatch(r"[!-~]{1,200}", confirm_key) is None:
            raise PendingHandoffError("Invalid confirmation key.")
        self._source = source_session
        self._prepare_json = request.model_dump_json()
        self._old_json = old.model_dump_json()
        self._confirm_key = confirm_key
        self._preview_json = self._confirm_json = self._confirmed_json = None
        self._session = None
        self._state = "new"
        self._busy = False

    def __repr__(self):
        return f"<PendingNativeHandoff state={self._state}>"

    @property
    def state(self) -> str:
        return self._state

    @property
    def blocks_other_actions(self) -> bool:
        return self._state not in {"cancelled", "adopted"}

    @property
    def old_selection(self) -> NativeHandoffSelection:
        return NativeHandoffSelection.model_validate_json(self._old_json)

    @property
    def prepare_request(self) -> PrepareNativeHandoff:
        return PrepareNativeHandoff.model_validate_json(self._prepare_json)

    @property
    def confirm_request(self) -> ConfirmNativeHandoff | None:
        return ConfirmNativeHandoff.model_validate_json(self._confirm_json) if self._confirm_json else None

    @property
    def preview(self) -> NativeHandoffResponse | None:
        return NativeHandoffResponse.model_validate_json(self._preview_json) if self._preview_json else None

    @property
    def confirmed(self) -> NativeHandoffResponse | None:
        return NativeHandoffResponse.model_validate_json(self._confirmed_json) if self._confirmed_json else None

    def _allow(self, *states):
        if self._busy or self._state not in states:
            raise PendingHandoffError("Handoff action is blocked by the current operation state.")

    def _source_unchanged(self):
        request = self.prepare_request
        try:
            scope, ref = self._source.handoff_source(request.source_scope)
        except ValueError:
            raise PendingHandoffError("Handoff source is no longer verified; no local selection was adopted.") from None
        if scope != request.source_scope or ref != request.source_ref:
            raise PendingHandoffError("Handoff source changed; no local selection was adopted.")

    async def prepare(self, client: Any) -> NativeHandoffResponse:
        self._allow("new", "prepare_uncertain", "prepared", "staged")
        self._source_unchanged()
        if self._preview_json is not None:
            return self.preview
        self._state, self._busy = "preparing", True
        try:
            result = NativeHandoffResponse.model_validate(await client.native_handoff_prepare(self.prepare_request))
            if (self._state != "preparing" or result.selection != self.prepare_request.selection
                    or result.destination_scope is not None):
                raise ValueError
            self._source_unchanged()
            self._preview_json, self._state = result.model_dump_json(), "prepared"
            return self.preview
        except BaseException as exc:
            if self._state != "cancelled":
                self._state = "prepare_uncertain"
            if isinstance(exc, Exception):
                raise PendingHandoffError("Handoff preparation was not verified; retry only the same operation.") from None
            raise
        finally:
            self._busy = False

    async def stage_destination(self, register: Callable[[], Awaitable[NativeInferenceScope | dict]]) -> NativeInferenceScope:
        self._allow("prepared", "staged")
        self._source_unchanged()
        if self._confirm_json is not None:
            return self.confirm_request.destination_scope
        self._state, self._busy = "staging", True
        try:
            raw = await register()
            scope = NativeInferenceScope.model_validate(raw.model_dump() if isinstance(raw, NativeInferenceScope) else raw)
            if self._state != "staging" or scope.run_id == self.prepare_request.source_scope.run_id:
                raise ValueError
            self._source_unchanged()
            request = ConfirmNativeHandoff(handoff_id=self.preview.handoff_id,
                handoff_sha256=self.preview.handoff_sha256, destination_scope=scope, idempotency_key=self._confirm_key)
            self._confirm_json, self._state = request.model_dump_json(), "staged"
            return self.confirm_request.destination_scope
        except BaseException as exc:
            if self._state != "cancelled":
                self._state = "prepared"
            if isinstance(exc, Exception):
                raise PendingHandoffError("Destination registration was not verified; the source is retained.") from None
            raise
        finally:
            self._busy = False

    async def confirm(self, client: Any) -> NativeHandoffResponse:
        self._allow("staged", "confirm_uncertain", "confirmed", "adopted")
        if self._confirmed_json is not None:
            return self.confirmed
        self._source_unchanged()
        self._state, self._busy = "confirming", True
        try:
            result = NativeHandoffResponse.model_validate(await client.native_handoff_confirm(self.confirm_request))
            expected = self.preview.model_dump()
            expected["destination_scope"] = self.confirm_request.destination_scope.model_dump()
            if result != NativeHandoffResponse.model_validate(expected):
                raise ValueError
            self._source_unchanged()
            self._confirmed_json, self._state = result.model_dump_json(), "confirmed"
            return self.confirmed
        except BaseException as exc:
            self._state = "confirm_uncertain"
            if isinstance(exc, Exception):
                raise PendingHandoffError("Confirmation outcome is uncertain; retry only the identical confirmation.") from None
            raise
        finally:
            self._busy = False

    def cancel(self) -> None:
        if self._state in {"confirming", "confirm_uncertain", "confirmed", "adopted"}:
            raise PendingHandoffError("Confirmation may be committed; it cannot be cancelled locally.")
        self._state = "cancelled"

    def adopt(self) -> NativeGenerationSession:
        self._allow("confirmed", "adopted")
        if self._session is None:
            self._source_unchanged()
            self._session = NativeGenerationSession.from_confirmed_handoff(self.confirmed)
            self._state = "adopted"
        return self._session
