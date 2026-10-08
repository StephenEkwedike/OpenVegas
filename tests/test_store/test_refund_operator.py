from types import SimpleNamespace
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from scripts.refund_emote import validate_target


def args(**kw):
    order = str(uuid4())
    return SimpleNamespace(
        **(
            {
                "apply": False,
                "user": str(uuid4()),
                "order": order,
                "operator": None,
                "confirm_order": None,
                "allow_remote": False,
                "confirm_host": None,
            }
            | kw
        )
    )


def test_default_is_inspection_only():
    value = args()
    assert validate_target(value, "postgresql://postgres@127.0.0.1/ov_test_refund")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "sqlite:///db",
        "postgresql://host/db?host=other",
        "postgresql://host/db#extra",
        "postgresql://host:bad/db",
    ],
)
def test_invalid_or_host_override_refused(url):
    with pytest.raises(ValueError):
        validate_target(args(), url)


def test_remote_write_requires_all_confirmations():
    value = args(apply=True, operator=str(uuid4()))
    with pytest.raises(ValueError):
        validate_target(value, "postgresql://db.example.invalid/db")
    value.confirm_order = value.order
    with pytest.raises(ValueError):
        validate_target(value, "postgresql://db.example.invalid/db")
    value.allow_remote = True
    value.confirm_host = "different.invalid"
    with pytest.raises(ValueError):
        validate_target(value, "postgresql://db.example.invalid/db")
    value.confirm_host = "db.example.invalid"
    assert validate_target(value, "postgresql://db.example.invalid/db")


@pytest.mark.parametrize("field", ["user", "order", "operator"])
def test_apply_requires_canonical_auditable_ids(field):
    value = args(apply=True, operator=str(uuid4()))
    value.confirm_order = value.order
    setattr(value, field, "not-an-id")
    with pytest.raises(ValueError):
        validate_target(value, "postgresql://localhost/db")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,state,needs_review,reason",
    [
        ("refund", "succeeded", False, "partial_refund_allocation_unknown"),
        ("refund", "pending", False, "pending_cash_refund"),
        ("refund", "requires_action", False, "pending_cash_refund"),
        ("refund", "failed", False, None),
        ("refund", "canceled", False, None),
        ("dispute", "needs_response", False, "open_dispute"),
        ("dispute", "under_review", False, "open_dispute"),
        ("dispute", "lost", False, "partial_lost_dispute_requires_review"),
        ("dispute", "won", False, None),
        ("dispute", "won", True, "conflicting_provider_facts"),
    ],
)
@pytest.mark.parametrize("attributed", [True, False])
async def test_inspection_exposes_current_cash_review_without_writes(
    kind, state, needs_review, reason, attributed
):
    from openvegas.store.refunds import inspect_refund

    user, order = str(uuid4()), str(uuid4())
    db = SimpleNamespace(
        fetchrow=AsyncMock(return_value={
            "id": order, "item_id": "local.fixture", "cost_v": Decimal(500),
            "status": "fulfilled", "entitlement_status": "active",
        }),
        fetchval=AsyncMock(return_value=attributed),
        fetch=AsyncMock(return_value=[{
            "topup_id": str(uuid4()), "amount_usd": Decimal(10),
            "kind": kind, "state": state, "needs_review": needs_review, "amount_minor": 300,
        }]),
    )
    result = await inspect_refund(db, user_id=user, order_id=order)
    assert result["cash_adjustment_review"] == {
        "required": reason is not None,
        "funding_attributed": attributed,
        "funding_review_required": reason is not None and not attributed,
        "reasons": [reason] if reason else [],
    }
    assert db.fetchval.call_args.args[1:] == (user, order)
    assert db.fetch.call_args.args[1:] == (user,)
    for method in (db.fetchrow, db.fetchval, db.fetch):
        assert method.call_args.args[0].lstrip().startswith("SELECT")
