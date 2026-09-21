from types import SimpleNamespace
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
