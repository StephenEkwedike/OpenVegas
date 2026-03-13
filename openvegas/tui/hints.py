"""Shared CLI/UI hint helpers."""

from __future__ import annotations


def verify_hint_for_result(game_id: str, is_demo: bool) -> str:
    if is_demo:
        return f"openvegas verify {game_id} --demo"
    return f"openvegas verify {game_id}"
