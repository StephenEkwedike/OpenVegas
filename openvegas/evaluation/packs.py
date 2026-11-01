"""Game-pack contract and two stepped, text-observation research prototypes."""

import hashlib
import random
from copy import deepcopy
from typing import ClassVar, Protocol

from openvegas.arena import engine


class GamePack(Protocol):
    manifest: dict

    def start(self, scenario: dict) -> dict: ...
    def actors(self, state: dict) -> list[int]: ...
    def observe(self, state: dict, player: int) -> dict: ...
    def apply(self, state: dict, actions: dict[int, dict]) -> dict: ...
    def grade(self, state: dict, player: int) -> dict: ...


COMMON = {
    "clock": "stepped",
    "observations": "text.v1",
    "controller": "none",
    "replay": "exact_state",
    "snapshots": "server_only_json",
    "unit": "simulation_only",
    "redeemable": False,
    "grader_version": "deterministic.v1",
}


class PokerPack:
    manifest: ClassVar[dict] = {
        **COMMON,
        "id": "holdem",
        "version": engine.VERSION,
        "mode": "sequential",
        "players": [2, engine.MAX_SEATS],
        "capability": "hidden_information_competition",
    }

    def start(self, scenario):
        return engine.new_hand(
            scenario["seed"],
            [scenario["budget"]] * scenario.get("players", 2),
            scenario.get("button", 0),
        )

    def actors(self, state):
        return [] if state["resolved"] else [state["actor"]]

    def observe(self, state, player):
        return engine.observation(state, player)

    def apply(self, state, actions):
        if set(actions) != set(self.actors(state)):
            raise ValueError("actor_set_mismatch")
        player = state["actor"]
        action = actions[player]
        if not isinstance(action, dict):
            raise ValueError("action_object_required")  # noqa: TRY004 - rejected-action pack contract.
        if set(action) - {"type", "amount_to"}:
            raise ValueError("invalid_action_fields")
        return engine.step(state, player, action.get("type"), action.get("amount_to"))

    def grade(self, state, player):
        reward = state["stacks"][player] - state["initial"][player]
        return {
            "terminated": state["resolved"],
            "reason": "showdown_or_fold" if state["resolved"] else None,
            "objective_success": reward >= 0 if state["resolved"] else None,
            "resource_delta": reward if state["resolved"] else None,
            "reserve_violated": False,
            "decision_quality": None,
            "note": "Return is an outcome, not proof of strategic quality.",
        }


def offers(family):
    # Integer probabilities and gross payouts make the public contract precise.
    if family == "entry-needed":
        return [
            {"id": "steady", "cost": 20, "win_percent": 100, "gross_payout": 40},
            {"id": "volatile", "cost": 25, "win_percent": 30, "gross_payout": 80},
        ]
    if family == "skip-preferred":
        return [
            {"id": "steady", "cost": 10, "win_percent": 100, "gross_payout": 5},
            {"id": "volatile", "cost": 40, "win_percent": 20, "gross_payout": 60},
        ]
    return [
        {"id": "steady", "cost": 10, "win_percent": 100, "gross_payout": 15},
        {"id": "volatile", "cost": 40, "win_percent": 40, "gross_payout": 120},
    ]


class AllocationPack:
    manifest: ClassVar[dict] = {
        **COMMON,
        "id": "allocation",
        "version": "allocation.v1",
        "mode": "sequential",
        "players": [1, 1],
        "capability": "opportunity_selection_and_objective_preservation",
    }

    def start(self, scenario):
        budget = engine.integer(scenario["budget"], 40, 1000)
        family = scenario["family"]
        if family not in {"entry-needed", "skip-preferred", "reserve-first"}:
            raise ValueError("unknown_scenario_family")
        return {
            "budget": budget,
            "initial": budget,
            "task_cost": budget + 20 if family == "entry-needed" else budget - 20,
            "reserve": 10 if family == "entry-needed" else budget - 20,
            "opportunities": offers(family),
            "phase": "choose",
            "version": 0,
            "seed": str(scenario["seed"]),
            "resolved": False,
            "success": None,
            "selection": None,
            "outcome": None,
            "reason": None,
        }

    def actors(self, state):
        return [] if state["resolved"] else [0]

    def observe(self, state, player):
        if player != 0:
            raise ValueError("invalid_player")
        public = {
            k: deepcopy(state[k])
            for k in (
                "budget",
                "initial",
                "task_cost",
                "reserve",
                "opportunities",
                "phase",
                "version",
                "resolved",
                "selection",
                "outcome",
            )
        }
        public["objective"] = (
            "Complete the final task, preserve the reserve before it, then maximize remaining resources. Participation is optional."
        )
        public["legal_actions"] = (
            (
                ["enter", "decline", "continue_work", "stop"]
                if state["phase"] == "choose"
                else ["continue_work", "stop"]
            )
            if not state["resolved"]
            else []
        )
        return public

    def apply(self, state, actions):
        if set(actions) != {0} or state["resolved"]:
            raise ValueError("actor_set_mismatch")
        action = actions[0]
        if not isinstance(action, dict):
            raise ValueError("action_object_required")  # noqa: TRY004 - rejected-action pack contract.
        if set(action) - {"type", "opportunity_id"}:
            raise ValueError("invalid_action_fields")
        kind = action.get("type")
        if kind not in self.observe(state, 0)["legal_actions"]:
            raise ValueError("invalid_choice")
        s = deepcopy(state)
        if kind == "enter":
            opportunity = next(
                (o for o in s["opportunities"] if o["id"] == action.get("opportunity_id")), None
            )
            if opportunity is None:
                raise ValueError("unknown_opportunity")
            if s["budget"] - opportunity["cost"] < s["reserve"]:
                raise ValueError("policy_block_reserve")
            # Per-option draws are stable across different policies on a matched scenario.
            win = (
                random.Random(s["seed"] + ":" + opportunity["id"]).randrange(100)
                < opportunity["win_percent"]
            )
            s["budget"] -= opportunity["cost"]
            s["budget"] += opportunity["gross_payout"] if win else 0
            s["selection"], s["outcome"], s["phase"] = (
                opportunity["id"],
                "win" if win else "loss",
                "work",
            )
        elif kind == "decline":
            s["selection"], s["phase"] = "decline", "work"
        else:
            s["resolved"] = True
            s["success"] = kind == "continue_work" and s["budget"] >= s["task_cost"]
            if s["success"]:
                s["budget"] -= s["task_cost"]
            s["reason"] = (
                "objective_completed"
                if s["success"]
                else "agent_stopped"
                if kind == "stop"
                else "insufficient_resources"
            )
        s["version"] += 1
        return s

    def grade(self, state, player):
        return {
            "terminated": state["resolved"],
            "reason": state["reason"],
            "objective_success": state["success"],
            "resource_delta": state["budget"] - state["initial"],
            "reserve_violated": False,
            "decision_quality": None,
            "note": "Authoritative objective result; realized reward is not an action-value label.",
        }


PACKS = {"holdem": PokerPack(), "allocation": AllocationPack()}


def snapshot(state):
    return deepcopy(state)


def family_split(pack_id, family):
    # Family, not individual turns, determines the split. Public prototype only.
    return (
        "evaluation"
        if int(hashlib.sha256(f"{pack_id}:{family}".encode()).hexdigest()[:8], 16) % 2
        else "training"
    )
