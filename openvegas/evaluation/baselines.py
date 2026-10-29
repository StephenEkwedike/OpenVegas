"""Versioned cheap baselines; no model requests or embedded access to private state."""

VERSION = "baselines.v1"
NAMES = ("never-enter", "expected-return", "reserve-aware")


def action(name, pack_id, view):
    if name not in NAMES:
        raise ValueError("unknown_baseline")
    if pack_id == "holdem":
        legal = view["legal_actions"]
        if name == "never-enter":
            return {"type": "fold"}
        return {"type": "check" if "check" in legal else "call"}
    if view["phase"] != "choose":
        return {"type": "continue_work"}
    if name == "never-enter":
        return {"type": "decline"}
    candidates = list(view["opportunities"])
    if name == "reserve-aware":
        candidates = [o for o in candidates if view["budget"] - o["cost"] >= view["reserve"]]
    candidates.sort(
        key=lambda o: o["gross_payout"] * o["win_percent"] / 100 - o["cost"], reverse=True
    )
    if (
        not candidates
        or candidates[0]["gross_payout"] * candidates[0]["win_percent"] / 100
        <= candidates[0]["cost"]
    ):
        return {"type": "decline"}
    return {"type": "enter", "opportunity_id": candidates[0]["id"]}
