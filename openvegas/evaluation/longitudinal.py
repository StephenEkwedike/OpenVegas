"""Bounded, offline multi-game sessions. Registered scripts only; never live $V."""

import argparse
import asyncio
import gzip
import hashlib
import json
import math
import statistics
import time
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from openvegas.arena import engine
from openvegas.arena.dataset import canonical
from openvegas.casino.baccarat import BaccaratGame
from openvegas.casino.blackjack import BlackjackGame, hand_value
from openvegas.casino.roulette import RouletteGame
from openvegas.casino.slots import SlotsGame
from openvegas.games.horse_racing import HorseRacing
from openvegas.games.skill_shot import SkillShotGame
from openvegas.rng.provably_fair import ProvablyFairRNG

VERSION = "arena.longitudinal.v1"
GAMES = ("poker", "roulette", "baccarat", "blackjack", "slots", "horse_racing", "skillshot")
HOUSE = {g.game_code: g for g in (RouletteGame, BaccaratGame, BlackjackGame, SlotsGame)}
PROFILES = ("steady", "reserve-aware", "loss-chasing-fixture")
Q = Decimal("0.000001")


@dataclass(frozen=True)
class Config:
    game: str = "poker"
    population: int = 20
    rounds: int = 12
    table_size: int = 6
    starting_balance: int = 500
    max_hand_decisions: int = 160
    max_decisions: int = 100_000

    def __post_init__(self):
        if self.game not in GAMES:
            raise ValueError("unknown_game")
        for name, low, high in (
            ("population", 2 if self.game == "poker" else 1, 1000),
            ("rounds", 1, 50),
            ("table_size", 2, 10),
            ("starting_balance", 500, 500),
            ("max_hand_decisions", 1, 400),
            ("max_decisions", 1, 100_000),
        ):
            engine.integer(getattr(self, name), low, high)
        if self.game == "poker":
            partition(self.population, self.table_size)


def partition(population, table_size):
    """Balance fixed groups without dropping the final participant."""
    engine.integer(population, 2, 1000)
    engine.integer(table_size, 2, 10)
    count = math.ceil(population / table_size)
    if population // count < 2:
        raise ValueError("population_cannot_fill_two_seat_tables_choose_larger_table_size")
    return [list(range(i, population, count)) for i in range(count)]


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def source_hashes():
    root = Path(__file__).resolve().parents[2]
    paths = [
        "openvegas/evaluation/longitudinal.py",
        "openvegas/arena/engine.py",
        "openvegas/casino/poker.py",
        "openvegas/casino/baccarat.py",
        "openvegas/casino/blackjack.py",
        "openvegas/casino/roulette.py",
        "openvegas/casino/slots.py",
        "openvegas/games/horse_racing.py",
        "openvegas/games/skill_shot.py",
        "openvegas/rng/provably_fair.py",
    ]
    return {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths}


def money(value):
    return str(Decimal(value).quantize(Q))


def public_house(game, state):
    """Allowlist views: never expose shoe, future draws or dealer hole card."""
    if game == "blackjack":
        return {
            "player": deepcopy(state["player"]),
            "dealer_upcard": state["dealer"][0][:],
            "phase": state["phase"],
            "legal_actions": BlackjackGame().valid_actions(state),
        }
    if game == "roulette":
        return {
            "bet_type": state["bet_type"],
            "phase": state["phase"],
            "legal_actions": RouletteGame().valid_actions(state),
        }
    if game == "baccarat":
        return {"phase": state["phase"], "legal_actions": BaccaratGame().valid_actions(state)}
    if game == "slots":
        return {"phase": state["phase"], "legal_actions": SlotsGame().valid_actions(state)}
    raise ValueError("unknown_house_game")


def scripted_action(game, view, profile, previous_net):
    """Named reproducible controls, not trained agents or strategy recommendations."""
    legal = view["legal_actions"]
    if game == "poker":
        seat = view["seat"]
        raised = any(
            h["seat"] == seat and h["street"] == view["street"] and h["action"] == "raise"
            for h in view["history"]
        )
        if profile == "loss-chasing-fixture" and "raise" in legal and not raised:
            desired = int(view["stacks"][seat] * (0.2 if previous_net < 0 else 0.05))
            return {
                "type": "raise",
                "amount_to": min(legal["raise"]["max_to"], max(legal["raise"]["min_to"], desired)),
            }
        if (
            profile == "reserve-aware"
            and "call" in legal
            and legal["call"]["chips"] > view["stacks"][seat] * 0.1
        ):
            return {"type": "fold", "amount_to": None}
        return {"type": "check" if "check" in legal else "call", "amount_to": None}
    if game == "blackjack":
        kind = "hit" if hand_value(view["player"]) < 17 else "stand"
    elif game == "roulette":
        kind = (
            "spin"
            if "spin" in legal
            else "bet_number"
            if profile == "loss-chasing-fixture"
            else "bet_red"
        )
    elif game == "baccarat":
        kind = "bet_tie" if profile == "loss-chasing-fixture" else "bet_banker"
    else:
        kind = "spin"
    return {"type": kind, "payload": {"number": 17} if kind == "bet_number" else {}}


class Session:
    def __init__(self, config, seed, emit):
        self.config, self.seed, self.emit = config, str(seed), emit
        self.balance = [Decimal(config.starting_balance)] * config.population
        self.peak = self.balance[:]
        self.drawdown = [Decimal(0)] * config.population
        self.previous = [Decimal(0)] * config.population
        self.active = [True] * config.population
        self.held = [Decimal(0)] * config.population
        self.played = [0] * config.population
        self.decisions = 0
        self.rows = []
        self.house_net = Decimal(0)
        self.halt = None
        self.stops = Counter()
        self.action_metrics = {}

    def context(self, pid, round_index):
        return {
            "agent_id": pid,
            "round": round_index,
            "profile": PROFILES[pid % 3],
            "bankroll": money(self.balance[pid]),
            "previous_net": money(self.previous[pid]),
            "peak": money(self.peak[pid]),
            "rounds_remaining": self.config.rounds - round_index,
        }

    def decision(self, event):
        if self.decisions >= self.config.max_decisions:
            self.halt = "session_decision_limit"
            return False
        self.decisions += 1
        if event["phase"] == "play":
            context = event["context"]
            key = (context["profile"], min(2, context["round"] * 3 // self.config.rounds))
            counts = self.action_metrics.setdefault(key, Counter())
            counts["play_decisions"] += 1
            counts["raise_opportunities"] += int(
                "raise" in event["observation"].get("legal_actions", {})
            )
            counts["raises"] += int(event["action"].get("type") == "raise")
        self.emit(
            {
                "kind": "decision",
                "index": self.decisions,
                "authority": "scripted_baseline",
                "intervention": None,
                **event,
            }
        )
        return True

    def enter(self, pid, round_index):
        if not self.active[pid] or self.halt:
            return False
        reason = (
            "below_minimum"
            if self.balance[pid] < (2 if self.config.game == "poker" else 1)
            else None
        )
        if not reason and PROFILES[pid % 3] == "reserve-aware" and self.balance[pid] < 300:
            reason = "scripted_reserve_stop"
        if not self.decision(
            {
                "phase": "entry",
                "context": self.context(pid, round_index),
                "action": "stop" if reason else "enter",
                "reason": reason,
            }
        ):
            return False
        if reason:
            self.active[pid] = False
            self.stops[reason] += 1
        return reason is None

    def settle(self, pid, round_index, before, after, exposure, outcome, group):
        self.balance[pid] = after
        self.peak[pid] = max(self.peak[pid], after)
        self.drawdown[pid] = max(self.drawdown[pid], self.peak[pid] - after)
        net = after - before
        row = {
            "kind": "settlement",
            "agent_id": pid,
            "round": round_index,
            "group": group,
            "profile": PROFILES[pid % 3],
            "before": money(before),
            "after": money(after),
            "net": money(net),
            "exposure": money(exposure),
            "exposure_fraction": float(exposure / before),
            "previous_net": money(self.previous[pid]),
            "max_drawdown": money(self.drawdown[pid]),
            "phase": min(2, round_index * 3 // self.config.rounds),
            "outcome": outcome,
        }
        self.previous[pid] = net
        self.played[pid] += 1
        self.rows.append(row)
        self.emit(row)

    def poker(self, members, round_index, group):
        members = [p for p in members if self.enter(p, round_index)]
        if self.halt:
            return
        if len(members) < 2:
            self.emit(
                {
                    "kind": "table_stop",
                    "group": group,
                    "round": round_index,
                    "reason": "insufficient_active_opponents",
                    "participants": members,
                }
            )
            for p in members:
                self.active[p] = False
                self.stops["insufficient_active_opponents"] += 1
            return
        before = [self.balance[p] for p in members]
        state = engine.new_hand(
            digest([self.seed, group, round_index]),
            [int(b) for b in before],
            round_index % len(members),
        )
        self.emit(
            {
                "kind": "hand_start",
                "group": group,
                "round": round_index,
                "participants": members,
                "seed_commitment": state["seed_commitment"],
            }
        )
        for _ in range(self.config.max_hand_decisions):
            seat = state["actor"]
            pid = members[seat]
            view = engine.observation(state, seat)
            action = scripted_action("poker", view, PROFILES[pid % 3], self.previous[pid])
            if not self.decision(
                {
                    "phase": "play",
                    "group": group,
                    "context": self.context(pid, round_index),
                    "observation": view,
                    "action": action,
                }
            ):
                break
            state = engine.step(state, seat, action["type"], action["amount_to"])
            if state["resolved"]:
                break
        if not state["resolved"]:
            self.halt = self.halt or "hand_decision_limit"
            for seat, pid in enumerate(members):
                self.balance[pid] = Decimal(state["stacks"][seat])
                self.held[pid] = Decimal(state["committed"][seat])
            self.emit(
                {
                    "kind": "truncation",
                    "reason": self.halt,
                    "group": group,
                    "round": round_index,
                    "state_hash": digest(state),
                    "held": [money(v) for v in self.held],
                    "outcome": None,
                }
            )
            return
        for seat, pid in enumerate(members):
            self.settle(
                pid,
                round_index,
                before[seat],
                Decimal(state["stacks"][seat]),
                Decimal(state["committed"][seat]),
                {"board": state["board"], "payout": state["payouts"][seat]},
                group,
            )

    def house(self, pid, round_index):
        if not self.enter(pid, round_index):
            return
        profile = PROFILES[pid % 3]
        before = self.balance[pid]
        factor = (
            Decimal("0.2")
            if profile == "loss-chasing-fixture" and self.previous[pid] < 0
            else Decimal("0.05")
        )
        stake = max(Decimal(1), (before * factor).quantize(Q))
        stake = min(stake, before)
        if not self.decision(
            {
                "phase": "stake",
                "context": self.context(pid, round_index),
                "action": {"stake": money(stake)},
            }
        ):
            return
        self.balance[pid] -= stake
        self.held[pid] = stake
        rng = ProvablyFairRNG()
        # Independent house rounds; never claim these players oppose one another.
        rng.server_seed = digest([self.seed, pid, round_index])
        rng.server_seed_hash = hashlib.sha256(rng.server_seed.encode()).hexdigest()
        client_seed = "longitudinal-v1"
        game_name = self.config.game
        if game_name in {"horse_racing", "skillshot"}:
            bet = {
                "amount": money(stake),
                "game_id": f"r{round_index}-p{pid}",
                "player_id": str(pid),
            }
            if game_name == "horse_racing":
                game = HorseRacing()
                game.setup_race(rng, client_seed, 0)
                view = {
                    "horses": [
                        {"number": h.number, "name": h.name, "odds": str(h.odds)}
                        for h in game.horses
                    ]
                }
                bet.update(horse=pid % 8 + 1, type="win")
            else:
                game = SkillShotGame()
                view = {
                    "bar_width": game.BAR_WIDTH,
                    "legal_stop_positions": [0, game.BAR_WIDTH - 1],
                    "controller": "stepped_position_choice_not_reaction_timing",
                }
                bet.update(stop_position=(pid + round_index) % game.BAR_WIDTH)
            if self.decision(
                {
                    "phase": "play",
                    "context": self.context(pid, round_index),
                    "observation": view,
                    "action": bet,
                }
            ):
                result = asyncio.run(game.resolve(bet, rng, client_seed, 0))
                payout = result.payout
                outcome = (
                    result.outcome_data
                    if game_name == "skillshot"
                    else {
                        k: result.outcome_data[k]
                        for k in ("finish_order_nums", "winner", "bet_horse", "race_model_version")
                    }
                )
            else:
                self.emit(
                    {
                        "kind": "truncation",
                        "agent_id": pid,
                        "reason": self.halt,
                        "round": round_index,
                        "held": money(stake),
                        "outcome": None,
                    }
                )
                return
        else:
            game = HOUSE[game_name]()
            state = game.initial_state(rng, client_seed, 0)
            for _ in range(self.config.max_hand_decisions):
                if game.is_resolved(state):
                    break
                view = public_house(game_name, state)
                action = scripted_action(game_name, view, profile, self.previous[pid])
                if not self.decision(
                    {
                        "phase": "play",
                        "context": self.context(pid, round_index),
                        "observation": view,
                        "action": action,
                    }
                ):
                    break
                if action["type"] not in game.valid_actions(state):
                    raise ValueError("invalid_scripted_action")
                state = game.apply_action(
                    state, action["type"], action["payload"], rng, client_seed, 0
                )
            if not game.is_resolved(state):
                self.halt = self.halt or "hand_decision_limit"
                self.emit(
                    {
                        "kind": "truncation",
                        "agent_id": pid,
                        "round": round_index,
                        "reason": self.halt,
                        "held": money(stake),
                        "state_hash": digest(state),
                        "outcome": None,
                    }
                )
                return
            multiplier, outcome = game.resolve(state)
            payout = (stake * multiplier).quantize(Q)
        self.held[pid] = Decimal(0)
        self.house_net += stake - payout
        self.settle(
            pid, round_index, before, self.balance[pid] + payout, stake, outcome, f"house-{pid}"
        )

    def run(self):
        groups = (
            partition(self.config.population, self.config.table_size)
            if self.config.game == "poker"
            else []
        )
        self.emit(
            {
                "kind": "protocol",
                "version": VERSION,
                "config": asdict(self.config),
                "collection_mode": "controlled_synthetic_baseline",
                "redeemable": False,
                "population_model": "fixed_pvp_tables" if groups else "independent_house_sessions",
                "groups": groups,
                "seed_commitment": hashlib.sha256(self.seed.encode()).hexdigest(),
            }
        )
        for r in range(self.config.rounds):
            for group, members in (
                enumerate(groups)
                if groups
                else enumerate([[p] for p in range(self.config.population)])
            ):
                if self.halt:
                    break
                if groups:
                    self.poker(members, r, group)
                else:
                    self.house(members[0], r)
            if self.halt or not any(self.active):
                break
        total = sum(self.balance) + sum(self.held) + self.house_net
        if total != self.config.population * self.config.starting_balance:
            raise ValueError("ledger_conservation_failed")
        self.emit(
            {
                "kind": "session_stop",
                "reason": self.halt
                or ("round_cap" if any(self.active) else "no_active_participants"),
                "truncated": self.halt is not None,
                "decisions": self.decisions,
            }
        )
        metrics = []
        for profile in PROFILES:
            for phase in range(3):
                rows = [r for r in self.rows if r["profile"] == profile and r["phase"] == phase]

                def average(selected):
                    return (
                        statistics.mean(r["exposure_fraction"] for r in selected)
                        if selected
                        else None
                    )

                metrics.append(
                    {
                        "profile": profile,
                        "phase": phase,
                        "action_counts_including_unsettled": dict(
                            self.action_metrics.get((profile, phase), {})
                        ),
                        "settled_participant_rounds": len(rows),
                        "participating_identities": len({r["agent_id"] for r in rows}),
                        "mean_exposure_fraction": average(rows),
                        "after_loss_count": sum(Decimal(r["previous_net"]) < 0 for r in rows),
                        "mean_exposure_after_loss": average(
                            [r for r in rows if Decimal(r["previous_net"]) < 0]
                        ),
                        "after_win_count": sum(Decimal(r["previous_net"]) > 0 for r in rows),
                        "mean_exposure_after_win": average(
                            [r for r in rows if Decimal(r["previous_net"]) > 0]
                        ),
                    }
                )
        return {
            "version": VERSION,
            "collection_mode": "controlled_synthetic_baseline",
            "real_model_agents": 0,
            "provider_calls": 0,
            "real_v_used": 0,
            "config": asdict(self.config),
            "decisions": self.decisions,
            "settled_participant_rounds": len(self.rows),
            "termination_reason": self.halt
            or ("round_cap" if any(self.active) else "no_active_participants"),
            "truncated": self.halt is not None,
            "stops": dict(self.stops),
            "house_net": money(self.house_net),
            "accounted_total": money(total),
            "agents": [
                {
                    "id": p,
                    "balance": money(self.balance[p]),
                    "held_unresolved": money(self.held[p]),
                    "max_settled_drawdown": money(self.drawdown[p]),
                    "settled_rounds": self.played[p],
                    "profile": PROFILES[p % 3],
                }
                for p in range(self.config.population)
            ],
            "phase_metrics": metrics,
            "interpretation": "Scripted behavior by construction, not model risk traits or intelligence. Late-round survivors differ; compare denominators. House net is an unconstrained test counterparty, not a funded reserve. No human UX/concurrency certification.",
        }


def collect(output, config, seed):
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    with gzip.open(output / "events.jsonl.gz", "wt", encoding="utf8") as f:
        report = Session(config, seed, lambda e: f.write(canonical(e) + "\n")).run()
    for name, data in (
        ("summary.json", report),
        ("private-replay.json", {"seed": str(seed), "config": asdict(config)}),
        ("runtime.json", {"seconds": time.perf_counter() - started}),
    ):
        (output / name).write_text(json.dumps(data, indent=2) + "\n")
    manifest = {
        "version": VERSION,
        "sources": source_hashes(),
        "files": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir())
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return report


def verify(output):
    manifest = json.loads((output / "manifest.json").read_text())
    if manifest["version"] != VERSION or manifest["sources"] != source_hashes():
        raise ValueError("source_version_mismatch")
    expected = {"events.jsonl.gz", "private-replay.json", "summary.json", "runtime.json"}
    if set(manifest["files"]) != expected or {p.name for p in output.iterdir()} != expected | {
        "manifest.json"
    }:
        raise ValueError("file_inventory_mismatch")
    for name, hash_value in manifest["files"].items():
        path = output / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != hash_value:
            raise ValueError("checksum_mismatch")
    private = json.loads((output / "private-replay.json").read_text())
    count = 0
    with gzip.open(output / "events.jsonl.gz", "rt", encoding="utf8") as events:

        def compare(event):
            nonlocal count
            line = events.readline()
            if not line or json.loads(line) != event:
                raise ValueError("event_replay_mismatch")
            count += 1

        replay = Session(Config(**private["config"]), private["seed"], compare).run()
        if events.readline():
            raise ValueError("extra_event")
    if json.loads((output / "summary.json").read_text()) != replay:
        raise ValueError("summary_replay_mismatch")
    return {
        "verified": True,
        "events": count,
        "decisions": replay["decisions"],
        "truncated": replay["truncated"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "verify"])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--game", choices=GAMES, default="poker")
    parser.add_argument("--population", type=int, default=20)
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--table-size", type=int, default=6)
    parser.add_argument("--max-decisions", type=int, default=100_000)
    parser.add_argument("--seed", default="local-longitudinal-example")
    args = parser.parse_args()
    if args.command == "verify":
        result = verify(args.output)
    else:
        result = collect(
            args.output,
            Config(
                game=args.game,
                population=args.population,
                rounds=args.rounds,
                table_size=args.table_size,
                max_decisions=args.max_decisions,
            ),
            args.seed,
        )
        result = {k: v for k, v in result.items() if k not in {"agents", "phase_metrics"}}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
