"""Versioned local Eval Lab runs. Only registered baseline policies execute here."""

import hashlib
import json
import random
import statistics
import time
import uuid
from copy import deepcopy
from pathlib import Path

from openvegas.arena.dataset import canonical
from openvegas.arena.engine import integer

from . import baselines, packs


def source_hashes():
    files = (
        Path(__file__),
        Path(baselines.__file__),
        Path(packs.__file__),
        Path(packs.engine.__file__),
    )
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def digest(data):
    return hashlib.sha256(canonical(data).encode()).hexdigest()


def run_episode(pack_id, scenario, policy, *, max_steps=64):
    integer(max_steps, 1, 1000)
    if policy not in baselines.NAMES:
        raise ValueError("registered_policy_required")
    pack = packs.PACKS[pack_id]
    state = pack.start(deepcopy(scenario))
    primary = integer(scenario.get("candidate_seat", 0), 0, packs.engine.MAX_SEATS - 1)
    if primary not in range(scenario.get("players", 1 if pack_id == "allocation" else 2)):
        raise ValueError("invalid_candidate_seat")
    record = {
        "schema_version": "openvegas.eval.episode.v1",
        "id": uuid.uuid4().hex,
        "collection_mode": "controlled_synthetic_baseline",
        "is_demo": True,
        "pack": deepcopy(pack.manifest),
        "scenario_id": scenario["id"],
        "family": scenario["family"],
        "split": packs.family_split(pack_id, scenario["family"]),
        "scenario_hash": digest(scenario),
        "group": digest(
            {k: v for k, v in scenario.items() if k not in {"candidate_seat", "button", "budget"}}
        ),
        "budget": scenario["budget"],
        "candidate_seat": primary,
        "policy": policy,
        "policy_version": baselines.VERSION,
        "opponent": "reserve-aware/check-call.v1",
        "track": "complete-agent-scripted",
        "model": None,
        "router": None,
        "memory": "reset_each_episode",
        "max_steps": max_steps,
        "events": [],
        "terminated": False,
        "truncated": False,
        "termination_reason": None,
        "failure_context": None,
        "usage": {
            "provider_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "inference_cost_usd": 0,
            "annotation_cost_usd": None,
            "review_cost_usd": None,
            "storage_cost_usd": None,
        },
    }
    failures = 0
    started = time.perf_counter()
    for index in range(max_steps):
        actors = pack.actors(state)
        if not actors:
            break
        before = {str(p): pack.observe(state, p) for p in actors}
        begin = time.perf_counter()
        try:
            submitted = {
                p: baselines.action(
                    policy if p == primary else "reserve-aware", pack_id, deepcopy(before[str(p)])
                )
                for p in actors
            }
        except TimeoutError:
            record["truncated"], record["termination_reason"] = True, "agent_timeout"
            record["failure_context"] = {"index": index, "observation": before}
            break
        except Exception:  # noqa: BLE001 - retain failures without copying exception secrets.
            record["truncated"], record["termination_reason"] = True, "agent_error"
            record["failure_context"] = {"index": index, "observation": before}
            break
        reason = None
        try:
            new_state = pack.apply(state, submitted)
        except ValueError as exc:
            reason = "policy_block" if str(exc).startswith("policy_block") else "invalid_action"
            failures += 1
            new_state = state
        except Exception:  # noqa: BLE001 - environment failures are not successful termination.
            record["truncated"], record["termination_reason"] = True, "environment_error"
            record["failure_context"] = {"index": index, "observation": before}
            break
        record["events"].append(
            {
                "index": index,
                "actors": actors,
                "observation": before,
                "actions": {str(p): submitted[p] for p in actors},
                "accepted": reason is None,
                "reason": reason,
                "resulting_observation": {str(p): pack.observe(new_state, p) for p in actors},
                "decision_ms": (time.perf_counter() - begin) * 1000,
                "authority": "scripted_agent",
                "intervention": None,
            }
        )
        state = new_state
        if failures >= 3:
            record["truncated"], record["termination_reason"] = True, "invalid_action_limit"
            break
        if not pack.actors(state):
            break
    grade = pack.grade(state, primary)
    if grade["terminated"]:
        record["terminated"], record["termination_reason"] = True, grade["reason"]
    elif not record["truncated"]:
        record["truncated"], record["termination_reason"] = True, "step_limit"
    record["usage"]["game_runtime_ms"] = (time.perf_counter() - started) * 1000
    record["final_state_hash"] = digest(state)
    # Scores are kept outside raw records; they can be regenerated without editing events.
    return (
        record,
        {"episode_id": record["id"], "grader": pack.manifest["grader_version"], **grade},
        {"episode_id": record["id"], "scenario": deepcopy(scenario)},
    )


def verify_episode(record, grade, private):
    pack = packs.PACKS[record["pack"]["id"]]
    scenario = private["scenario"]
    if record["pack"] != pack.manifest or record["scenario_hash"] != digest(scenario):
        raise ValueError("version_or_scenario_mismatch")
    if (
        record["family"] != scenario["family"]
        or record["scenario_id"] != scenario["id"]
        or record["budget"] != scenario["budget"]
        or record["candidate_seat"] != scenario.get("candidate_seat", 0)
        or record["group"]
        != digest(
            {k: v for k, v in scenario.items() if k not in {"candidate_seat", "button", "budget"}}
        )
        or record["split"] != packs.family_split(record["pack"]["id"], record["family"])
    ):
        raise ValueError("family_split_mismatch")
    if (
        record["policy"] not in baselines.NAMES
        or record["policy_version"] != baselines.VERSION
        or record["collection_mode"] != "controlled_synthetic_baseline"
        or record["usage"]["provider_calls"] != 0
    ):
        raise ValueError("provenance_mismatch")
    integer(record["max_steps"], 1, 1000)
    if len(record["events"]) > record["max_steps"]:
        raise ValueError("step_limit_mismatch")
    state = pack.start(scenario)
    for i, event in enumerate(record["events"]):
        actors = pack.actors(state)
        if (
            event["index"] != i
            or event["actors"] != actors
            or event["observation"] != {str(p): pack.observe(state, p) for p in actors}
        ):
            raise ValueError("observation_replay_mismatch")
        try:
            after = pack.apply(state, {int(p): a for p, a in event["actions"].items()})
        except ValueError as exc:
            reason = "policy_block" if str(exc).startswith("policy_block") else "invalid_action"
            if event["accepted"] or event["reason"] != reason:
                raise ValueError("invalid_action_marked_accepted") from None
            after = state
        else:
            if not event["accepted"] or event["reason"] is not None:
                raise ValueError("valid_action_marked_rejected")
        if event["resulting_observation"] != {str(p): pack.observe(after, p) for p in actors}:
            raise ValueError("result_replay_mismatch")
        state = after
    expected = {
        "episode_id": record["id"],
        "grader": pack.manifest["grader_version"],
        **pack.grade(state, record["candidate_seat"]),
    }
    if expected != grade or digest(state) != record["final_state_hash"]:
        raise ValueError("grader_or_state_mismatch")
    if (
        record["terminated"] != expected["terminated"]
        or record["terminated"] == record["truncated"]
    ):
        raise ValueError("termination_truncation_mismatch")
    if record["terminated"] and record["termination_reason"] != expected["reason"]:
        raise ValueError("termination_reason_mismatch")
    if record["truncated"]:
        reason = record["termination_reason"]
        if reason not in {
            "step_limit",
            "invalid_action_limit",
            "agent_error",
            "agent_timeout",
            "environment_error",
        }:
            raise ValueError("unknown_truncation")
        if reason == "step_limit" and len(record["events"]) != record["max_steps"]:
            raise ValueError("premature_step_limit")
        if (
            reason == "invalid_action_limit"
            and sum(not e["accepted"] for e in record["events"]) != 3
        ):
            raise ValueError("invalid_action_limit_mismatch")
        if reason in {"agent_error", "agent_timeout", "environment_error"} and record[
            "failure_context"
        ] != {
            "index": len(record["events"]),
            "observation": {str(p): pack.observe(state, p) for p in pack.actors(state)},
        }:
            raise ValueError("failure_context_mismatch")
    return True


def interval(groups):
    if not groups:
        return {"mean": None, "ci95": None, "independent_groups": 0}
    values = list(groups.values())
    mean = statistics.mean(values)
    if len(values) < 10:
        return {"mean": mean, "ci95": None, "independent_groups": len(values)}
    rng = random.Random(1701)
    boot = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(1000))
    return {"mean": mean, "ci95": [boot[24], boot[974]], "independent_groups": len(values)}


def report(records, grades):
    scores = {g["episode_id"]: g for g in grades}
    output = {}
    keys = sorted({(r["pack"]["id"], r["family"], r["budget"], r["policy"]) for r in records})
    for pack_id, family, budget, policy in keys:
        rows = [
            r
            for r in records
            if (r["pack"]["id"], r["family"], r["budget"], r["policy"])
            == (pack_id, family, budget, policy)
        ]
        grouped = {}
        for r in rows:
            success = scores[r["id"]]["objective_success"]
            if success is not None:
                grouped.setdefault(r["group"], []).append(float(success))
        deltas = [
            scores[r["id"]]["resource_delta"]
            for r in rows
            if scores[r["id"]]["resource_delta"] is not None
        ]
        output[f"{pack_id}/{family}/{budget}/{policy}"] = {
            "episodes": len(rows),
            "success": interval({k: statistics.mean(v) for k, v in grouped.items()}),
            "truncated": sum(r["truncated"] for r in rows),
            "policy_blocks": sum(e["reason"] == "policy_block" for r in rows for e in r["events"]),
            "invalid_actions": sum(
                e["reason"] == "invalid_action" for r in rows for e in r["events"]
            ),
            "participations": sum(
                e["actions"].get("0", {}).get("type") == "enter" and e["accepted"]
                for r in rows
                for e in r["events"]
            ),
            "declines": sum(
                e["actions"].get("0", {}).get("type") == "decline"
                for r in rows
                for e in r["events"]
            ),
            "unknown_outcomes": sum(scores[r["id"]]["objective_success"] is None for r in rows),
            "mean_resource_delta": statistics.mean(deltas) if deltas else None,
        }
    return {
        "capabilities": output,
        "universal_intelligence_score": None,
        "risk_appetite_score": None,
        "method": "Matched scenarios, fresh memory, swapped poker seats. Bootstrap scenario-group means, not correlated turns. CI withheld below 10 groups; prototype, no power claim.",
        "exclusions": [],
        "model_rankings": False,
        "provider_calls": sum(r["usage"]["provider_calls"] for r in records),
    }


def suite(destination, repeats=10):
    integer(repeats, 1, 100)
    records, grades, private = [], [], []
    for i in range(repeats):
        for pack_id, families in {
            "holdem": ["fixed-opponent"],
            "allocation": ["entry-needed", "skip-preferred", "reserve-first"],
        }.items():
            for family in families:
                for budget in (60, 100):
                    for seat in range(2) if pack_id == "holdem" else [0]:
                        scenario = {
                            "id": f"{pack_id}:{family}:{i}",
                            "family": family,
                            "seed": f"public-prototype:{pack_id}:{family}:{i}",
                            "budget": budget,
                            "candidate_seat": seat,
                        }
                        for policy in baselines.NAMES:
                            r, g, p = run_episode(pack_id, scenario, policy)
                            records.append(r)
                            grades.append(g)
                            private.append(p)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)

    def write(name, value, lines=False):
        path = destination / name
        path.write_text(
            "".join(canonical(v) + "\n" for v in value)
            if lines
            else json.dumps(value, indent=2) + "\n"
        )
        path.chmod(0o600)

    write("episodes.jsonl", records, True)
    write("grades.jsonl", grades, True)
    write("private-replay.jsonl", private, True)
    write("report.json", report(records, grades))
    write(
        "manifest.json",
        {
            "schema_version": "openvegas.eval.bundle.v1",
            "episodes": len(records),
            "collection_mode": "controlled_synthetic_baseline",
            "commercial_release": False,
            "files": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in destination.iterdir()
            },
            "source_hashes": source_hashes(),
            "limitations": [
                "public prototype seeds are not secret held-out benchmarks",
                "only trusted in-process scripted policies",
                "no training benefit demonstrated",
            ],
        },
    )
    return verify_bundle(destination)


def verify_bundle(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["source_hashes"] != source_hashes():
        raise ValueError("source_version_mismatch")
    if set(manifest["files"]) != {
        "episodes.jsonl",
        "grades.jsonl",
        "private-replay.jsonl",
        "report.json",
    }:
        raise ValueError("unexpected_files")
    for name, expected in manifest["files"].items():
        path = directory / name
        if (
            path.is_symlink()
            or path.stat().st_size > 128_000_000
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("unsafe_file_or_checksum")

    def lines(name):
        return [json.loads(s) for s in (directory / name).read_text().splitlines()]

    records, grades, private = (
        lines("episodes.jsonl"),
        lines("grades.jsonl"),
        lines("private-replay.jsonl"),
    )
    if len(records) != manifest["episodes"] or len({r["id"] for r in records}) != len(records):
        raise ValueError("episode_count_or_identity")
    gs, ps = {g["episode_id"]: g for g in grades}, {p["episode_id"]: p for p in private}
    if (
        len(gs) != len(records)
        or len(ps) != len(records)
        or set(gs) != set(ps)
        or set(gs) != {r["id"] for r in records}
    ):
        raise ValueError("linked_records_mismatch")
    for r in records:
        verify_episode(r, gs[r["id"]], ps[r["id"]])
    if json.loads((directory / "report.json").read_text()) != report(records, grades):
        raise ValueError("report_mismatch")
    return {
        "episodes": len(records),
        "decisions": sum(len(r["events"]) for r in records),
        "verified": True,
        "provider_calls": 0,
        "native_ux_verified": False,
        "commercial_rights_verified": False,
    }
