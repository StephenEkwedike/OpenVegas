from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def test_wrapper_reward_reference_and_entry_type_are_locked():
    src = _read("openvegas/wallet/ledger.py")
    assert 'entry_type="wrapper_reward"' in src
    assert 'reference_id=f"wrapper_reward:{inference_usage_id}"' in src


def test_wrapper_reward_requires_settled_hold_and_occurs_post_settlement():
    src = _read("openvegas/gateway/inference.py")
    assert "SELECT status FROM inference_preauthorizations WHERE id = $1 FOR UPDATE" in src
    assert 'str(preauth["status"]) != "settled"' in src
    settle_idx = src.index("await self._settle_preauth(")
    reward_idx = src.index("INSERT INTO wrapper_reward_events")
    assert settle_idx < reward_idx

