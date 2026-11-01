"""Shared show_diff decision helpers for local bridge adapters."""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Any

from openvegas.ide.bridge_types import DiffHunkDecision, ShowDiffResult


def _hunk_count(old_text: str, new_text: str, path: str) -> int:
    lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=path,
            tofile=path,
            lineterm="",
        )
    )
    return sum(1 for line in lines if line.startswith("@@"))


def _parse_partial_accept_indexes(raw: str, max_hunks: int) -> set[int]:
    out: set[int] = set()
    for token in [x.strip() for x in raw.split(",") if x.strip()]:
        try:
            idx = int(token)
        except Exception:
            continue
        if 0 <= idx < max_hunks:
            out.add(idx)
    return out


def build_show_diff_result(
    *,
    path: str,
    current_contents: str,
    new_contents: str,
    allow_partial_accept: bool = True,
) -> ShowDiffResult:
    hunks_total = _hunk_count(current_contents, new_contents, path)
    if hunks_total <= 0:
        return {
            "file_path": path,
            "hunks_total": 0,
            "decisions": [],
            "all_accepted": True,
            "timed_out": False,
        }

    mode = str(os.getenv("OPENVEGAS_SHOW_DIFF_DECISION", "timeout")).strip().lower()
    if mode not in {"accept_all", "reject_all", "partial", "timeout"}:
        mode = "timeout"
    if mode == "partial" and not allow_partial_accept:
        mode = "reject_all"

    decisions: list[DiffHunkDecision] = []
    timed_out = False
    accepted: set[int] = set()

    if mode == "accept_all":
        accepted = set(range(hunks_total))
    elif mode == "partial":
        accepted = _parse_partial_accept_indexes(
            os.getenv("OPENVEGAS_SHOW_DIFF_ACCEPT_HUNKS", ""),
            hunks_total,
        )
    elif mode == "timeout":
        timed_out = True

    for idx in range(hunks_total):
        decisions.append(
            {
                "hunk_index": idx,
                "decision": "accepted" if idx in accepted else "rejected",
            }
        )
    return {
        "file_path": path,
        "hunks_total": hunks_total,
        "decisions": decisions,
        "all_accepted": len(accepted) == hunks_total,
        "timed_out": timed_out,
    }


def normalize_show_diff_result(
    raw: dict[str, Any] | ShowDiffResult | None,
    *,
    default_path: str = "",
) -> ShowDiffResult:
    payload = raw if isinstance(raw, dict) else {}
    file_path = str(payload.get("file_path") or default_path or "")

    raw_decisions = payload.get("decisions", [])
    decisions: list[DiffHunkDecision] = []
    max_seen_index = -1
    if isinstance(raw_decisions, list):
        for item in raw_decisions:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("hunk_index"))
            except Exception:
                continue
            if idx < 0:
                continue
            decision = str(item.get("decision") or "").strip().lower()
            if decision not in {"accepted", "rejected"}:
                continue
            decisions.append({"hunk_index": idx, "decision": decision})  # type: ignore[typeddict-item]
            max_seen_index = max(max_seen_index, idx)

    try:
        hunks_total = int(payload.get("hunks_total", len(decisions)))
    except Exception:
        hunks_total = len(decisions)
    hunks_total = max(hunks_total, max_seen_index + 1, 0)

    by_idx: dict[int, str] = {}
    for d in decisions:
        by_idx[int(d["hunk_index"])] = str(d["decision"])
    normalized_decisions: list[DiffHunkDecision] = []
    for idx in range(hunks_total):
        normalized_decisions.append(
            {
                "hunk_index": idx,
                "decision": "accepted" if by_idx.get(idx) == "accepted" else "rejected",
            }
        )

    timed_out = bool(payload.get("timed_out", False))
    accepted_count = sum(1 for d in normalized_decisions if d["decision"] == "accepted")
    all_accepted = bool(payload.get("all_accepted", False))
    if hunks_total > 0:
        all_accepted = accepted_count == hunks_total
    elif not normalized_decisions:
        all_accepted = True

    return {
        "file_path": file_path,
        "hunks_total": hunks_total,
        "decisions": normalized_decisions,
        "all_accepted": bool(all_accepted),
        "timed_out": timed_out,
    }


def is_valid_show_diff_payload(raw: dict[str, Any] | None) -> bool:
    payload = raw if isinstance(raw, dict) else {}
    try:
        hunks_total = int(payload.get("hunks_total", 0))
    except Exception:
        return False
    if hunks_total < 0:
        return False
    timed_out = bool(payload.get("timed_out", False))
    all_accepted = bool(payload.get("all_accepted", False))
    decisions = payload.get("decisions", [])
    if not isinstance(decisions, list):
        return False

    if hunks_total == 0:
        # Zero-hunk payload is valid only as explicit no-op shape.
        return len(decisions) == 0 and timed_out is False and all_accepted is True

    if len(decisions) != hunks_total:
        return False

    seen: set[int] = set()
    accepted = 0
    for idx, d in enumerate(decisions):
        if not isinstance(d, dict):
            return False
        try:
            hunk_index = int(d.get("hunk_index"))
        except Exception:
            return False
        if hunk_index != idx:
            return False
        if hunk_index in seen:
            return False
        seen.add(hunk_index)
        decision = str(d.get("decision") or "").strip().lower()
        if decision not in {"accepted", "rejected"}:
            return False
        if decision == "accepted":
            accepted += 1

    if all_accepted != (accepted == hunks_total):
        return False
    return True


def redact_show_diff_payload_shape(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {"type": type(raw).__name__}
    out: dict[str, object] = {}
    for k in ("file_path", "hunks_total", "all_accepted", "timed_out"):
        if k in raw:
            out[k] = raw.get(k)
    decisions = raw.get("decisions")
    if isinstance(decisions, list):
        out["decisions_count"] = len(decisions)
        indexes: list[object] = []
        values: list[object] = []
        for item in decisions[:64]:
            if isinstance(item, dict):
                indexes.append(item.get("hunk_index"))
                values.append(item.get("decision"))
            else:
                indexes.append(type(item).__name__)
        out["decision_indexes"] = indexes
        out["decision_values"] = values
    for blocked in ("new_contents", "current_contents", "content", "patch", "diff_text"):
        if blocked in raw:
            out[f"{blocked}_stripped"] = True
    return out


def read_text_best_effort(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""
