"""Pure presentation choices from server-reviewed native billing options.

These helpers neither establish model access nor authorize spending. Callers
must use fresh authenticated options, obtain a matching quote and explicit
consent, and leave wallet reservations/model enforcement to the backend.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Context, Decimal, InvalidOperation, localcontext

from .credentials import IntegrationError

MAX_CEILING_V = Decimal(100000)
_MICRO_V = Decimal("0.000001")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{2,159}\Z")
_NATIVE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}\Z")
_PROVIDERS = {"codex": "openai", "claude": "anthropic"}


def _amount(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise IntegrationError("Invalid wallet amount; billing unchanged.")
    try:
        raw = str(value)
        if len(raw) > 64:
            raise ValueError
        amount = Decimal(raw)
        if (not amount.is_finite() or amount < 0
                or abs(amount.as_tuple().exponent) > 100):
            raise ValueError
        return amount.copy_abs() if amount.is_zero() else amount
    except (ValueError, InvalidOperation):
        raise IntegrationError("Invalid wallet amount; billing unchanged.") from None


def wallet_ceiling(balance_v: object, backend_max_v: object = MAX_CEILING_V) -> Decimal:
    """Return a wallet-backed ceiling, capped at 100000 V, rounded DOWN to micros.

Zero (including a sub-micro balance) means no session may be opened. The balance
must be spendable wallet funds, not credit or a prospective top-up. This snapshot
is not a reservation: concurrent spending must still be checked by the ledger.
"""
    balance, maximum = _amount(balance_v), _amount(backend_max_v)
    if maximum <= 0:
        raise IntegrationError("Invalid backend spending maximum; billing unchanged.")
    # Never inherit a caller's rounding mode/low precision and round funds up.
    with localcontext(Context(prec=32, rounding=ROUND_DOWN)):
        return min(balance, maximum, MAX_CEILING_V).quantize(_MICRO_V)


@dataclass(frozen=True)
class ModelChoices:
    options: tuple[str, ...]
    selected_model: str | None
    unavailable_model: str | None


def model_choices(
    host: str,
    reviewed_models: Sequence[Mapping[str, object]],
    current_model: str | None = None,
) -> ModelChoices:
    """Preserve an exact reviewed model or require an explicit fallback choice.

Pass ``options['models']`` from the authenticated native-options endpoint. Order
is preserved, but even a single alternative is never selected automatically.
No aliases, provider prefixes, dated versions, subscription models or reasoning
efforts are inferred. Returned IDs are safe to display, not evidence of access.
"""
    if not isinstance(host, str) or host not in _PROVIDERS:
        raise IntegrationError("Unsupported native host; billing unchanged.")
    if (not isinstance(reviewed_models, (list, tuple)) or len(reviewed_models) > 128
            or (current_model is not None and (
                not isinstance(current_model, str) or not _NATIVE_MODEL.fullmatch(current_model)))):
        raise IntegrationError("Invalid native model options; billing unchanged.")
    models = []
    for item in reviewed_models:
        if not isinstance(item, Mapping):
            raise IntegrationError("Invalid native model options; billing unchanged.")
        model, tokens = item.get("model"), item.get("max_tokens")
        if (not isinstance(model, str) or not _MODEL.fullmatch(model)
                or type(tokens) is not int or not 1 <= tokens <= 128000
                or item.get("host", host) != host
                or item.get("provider", _PROVIDERS[host]) != _PROVIDERS[host]
                or model in models):
            raise IntegrationError("Invalid native model options; billing unchanged.")
        models.append(model)
    selected = current_model if current_model in models else None
    return ModelChoices(tuple(models), selected, current_model if selected is None else None)
