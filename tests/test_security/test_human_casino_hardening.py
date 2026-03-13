from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def test_human_casino_service_uses_row_locks_and_tx_persistence():
    src = _read("openvegas/casino/human_service.py")
    assert "FOR UPDATE" in src
    assert "async with self.db.transaction() as tx:" in src
    assert "await self._idem_persist(tx, row_id=idem.row_id, response=out)" in src


def test_human_casino_routes_return_structured_json_responses():
    src = _read("server/routes/casino_human.py")
    assert "return Response(content=body_text, status_code=status_code, media_type=\"application/json\")" in src
    assert "demo_autoplay_cap_exhausted" in _read("openvegas/casino/human_service.py")
