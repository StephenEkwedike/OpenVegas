"""One gateway boundary for two distinct, immutable handoff authorizations.

First consumption and later continuation never substitute for each other. The
gateway latches one binding before awaiting and passes it to every later guard.
"""
from __future__ import annotations

import json

from openvegas.contracts.errors import APIErrorCode, ContractError


def _fail():
    raise ContractError(APIErrorCode.HANDOFF_BLOCKED,
                        "The model handoff authorization changed; no unverified request was sent.") from None


def binding_for(request):
    from server.services.native_handoff_continuation import HandoffContinuationBinding
    from server.services.native_handoff_dispatch import HandoffDispatchBinding

    first = getattr(request, "_native_handoff_binding", None)
    continuation = getattr(request, "_native_handoff_continuation_binding", None)
    if first is not None:
        if type(first) is not HandoffDispatchBinding or continuation is not None:
            _fail()
        return first
    if continuation is not None:
        if type(continuation) is not HandoffContinuationBinding:
            _fail()
        return continuation
    return None


def validate_bound_request(request, *, expected=None, payload=None):
    from server.services.native_handoff_continuation import (
        HandoffContinuationBinding,
        validate_continuation,
    )
    from server.services.native_handoff_dispatch import validate_bound_request as validate_first

    actual = binding_for(request)
    if actual is None or (expected is not None and actual is not expected):
        _fail()
    if type(actual) is HandoffContinuationBinding:
        return validate_continuation(request, expected=actual, payload=payload)
    return validate_first(request, expected=actual, payload=payload)


def validate_dispatch_deadline(request, *, expected=None):
    from server.services.native_handoff_continuation import (
        HandoffContinuationBinding,
        validate_continuation_deadline,
    )
    from server.services.native_handoff_dispatch import (
        validate_dispatch_deadline as validate_first_deadline,
    )

    actual = validate_bound_request(request, expected=expected)
    if type(actual) is HandoffContinuationBinding:
        return validate_continuation_deadline(request, expected=actual)
    return validate_first_deadline(request, expected=actual)


async def verify_dispatch_tx(tx, request, *, expected):
    from server.services.native_handoff_continuation import (
        HandoffContinuationBinding,
        verify_continuation_tx,
    )
    from server.services.native_handoff_dispatch import verify_first_dispatch_tx

    actual = validate_bound_request(request, expected=expected)
    if type(actual) is HandoffContinuationBinding:
        return await verify_continuation_tx(tx, request, expected=actual)
    return await verify_first_dispatch_tx(tx, request, expected=actual)


async def consume_dispatch_tx(tx, request, request_id, *, expected):
    from server.services.native_handoff_continuation import HandoffContinuationBinding
    from server.services.native_handoff_dispatch import consume_first_dispatch_tx

    actual = validate_bound_request(request, expected=expected)
    if type(actual) is not HandoffContinuationBinding:
        return await consume_first_dispatch_tx(tx, request, request_id, expected=actual)
    # Continuation links its own route/gateway claim, never consumes the original
    # first-generation commitment again or extends the original handoff TTL.
    return None


def attachment_file_ids(request):
    from server.services.native_handoff_continuation import HandoffContinuationBinding

    actual = validate_bound_request(request)
    current = tuple(ref["file_id"] for ref in request._native_history_inputs.values()["attachment_refs"])
    if type(actual) is HandoffContinuationBinding:
        if current and actual.file_ids[-len(current):] != current:
            _fail()
        return actual.file_ids
    return actual.inherited_file_ids + current


def payload_sha256(binding):
    from server.services.native_handoff_attachments import _hash
    from server.services.native_handoff_continuation import HandoffContinuationBinding
    from server.services.native_handoff_dispatch import HandoffDispatchBinding

    if type(binding) is HandoffContinuationBinding:
        return _hash(json.loads(binding.payload_json))
    if type(binding) is HandoffDispatchBinding:
        return binding.payload_sha256
    _fail()
