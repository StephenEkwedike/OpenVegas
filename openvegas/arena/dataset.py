"""Auditable local datasets. Outcomes, preferences and costs remain distinct."""

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import re

from .engine import VERSION, integer, new_hand, observation, step
from openvegas.casino.poker import DECK


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def preference(context, chosen, rejected, method, evidence_ref, judge_id):
    if (
        method not in {"human_review", "solver", "controlled_simulation"}
        or not evidence_ref
        or not judge_id
    ):
        raise ValueError("documented_judgment_required_not_outcome")
    if not all(
        isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,200}", x)
        for x in (evidence_ref, judge_id)
    ):
        raise ValueError("use_judgment_identifiers_not_free_text")
    public_keys = set(observation(new_hand("schema", [200, 200]), 0)) | {"showdown"}
    if not isinstance(context, dict) or context.keys() - public_keys:
        raise ValueError("only_player_observation_fields_allowed")

    # Canonical engine observations contain no free-form nested dictionaries.
    def private_keys(value):
        if isinstance(value, dict):
            return any(
                k in {"deck", "holes", "seed", "burned", "token", "password"} or private_keys(v)
                for k, v in value.items()
            )
        return isinstance(value, list) and any(private_keys(v) for v in value)

    if private_keys(context):
        raise ValueError("private_state_not_a_player_context")
    if chosen == rejected:
        raise ValueError("distinct_candidates_required")
    for candidate in (chosen, rejected):
        if set(candidate) - {"type", "amount_to"}:
            raise ValueError("invalid_candidate")
        action = candidate.get("type")
        options = context.get("legal_actions", {})
        if action not in options:
            raise ValueError("candidate_not_legal_in_same_context")
        if action == "raise":
            integer(
                candidate.get("amount_to"), options[action]["min_to"], options[action]["max_to"]
            )
        elif candidate.get("amount_to") is not None:
            raise ValueError("amount_only_for_raise")
    if any(k in context for k in ("deck", "holes", "seed", "burned")):
        raise ValueError("private_state_not_a_player_context")
    prompt = canonical(context)
    return {
        "prompt": prompt,
        "chosen": canonical(chosen),
        "rejected": canonical(rejected),
        "context_hash": hashlib.sha256(prompt.encode()).hexdigest(),
        "label_type": "human_preference" if method == "human_review" else "estimated_action_value",
        "judge": {"method": method, "evidence_ref": evidence_ref, "judge_id": judge_id},
        "not_observed_outcome": True,
    }


def replay(match):
    state = new_hand(match["seed"], match["state"]["initial"], match["state"]["button"])
    for index, event in enumerate(match["events"]):
        seat = event["actor"]["seat"]
        if event["event_index"] != index or event["observation"] != observation(state, seat):
            raise ValueError("replay_observation_mismatch")
        if event["accepted"]:
            state = step(state, seat, event["action"]["type"], event["action"]["amount_to"])
        if event["resulting_observation"] != observation(state, seat):
            raise ValueError("replay_result_mismatch")
    if state != match["state"]:
        raise ValueError("replay_final_state_mismatch")
    return True


def evaluation_track(match):
    if match.get("interventions") or any(
        e["actor"].get("authority") == "human_controller" for e in match["events"]
    ):
        return "assisted"
    if match["is_demo"]:
        return "synthetic"
    if all(p["source_type"] == "agent" for p in match["players"]) and all(
        e["actor"].get("authority") in {"agent", "server_timeout"} for e in match["events"]
    ):
        return "autonomous"
    return "human_or_unverified"


def validate_interventions(interventions, events, episode_id):
    seen = set()
    for row in interventions:
        if (
            row["id"] in seen
            or row["episode_id"] != episode_id
            or row["actor"] != "human_controller"
            or row["command"] not in {"pause", "resume", "stop", "override"}
            or row["revision_after"] != row["revision_before"] + 1
            or not 0 <= row["decision_index"] <= len(events)
        ):
            raise ValueError("invalid_intervention_record")
        seen.add(row["id"])
        linked = [e for e in events if e.get("intervention_id") == row["id"]]
        if linked:
            if (
                len(linked) != 1
                or row["command"] != "override"
                or linked[0]["event_index"] != row["decision_index"]
                or linked[0]["actor"].get("authority") != "human_controller"
                or linked[0]["actor"]["seat"] != row["seat"]
                or linked[0]["action"] != row["action"]
                or linked[0]["accepted"] != row["accepted"]
                or linked[0]["submitted_version"] != row["version"]
            ):
                raise ValueError("intervention_decision_mismatch")
        elif row["command"] == "override" and row["accepted"]:
            raise ValueError("missing_override_decision")
    for e in events:
        if (
            e["actor"].get("authority") == "human_controller"
            and e.get("intervention_id") not in seen
        ):
            raise ValueError("unattributed_human_override")
        if e.get("intervention_id") and e["intervention_id"] not in seen:
            raise ValueError("orphan_intervention")


def metrics(matches):
    events = [e for m in matches for e in m["events"]]
    accepted = sum(e["accepted"] for e in events)
    timeouts = sum(e.get("reason") == "timeout" for e in events)
    latencies = sorted(
        e["usage"]["latency_ms"] for e in events if e["usage"]["latency_ms"] is not None
    )
    return {
        "episodes": len(matches),
        "attempts": len(events),
        "accepted": accepted,
        "rejected": len(events) - accepted,
        "timeouts": timeouts,
        "invalid_action_rate": (len(events) - accepted) / len(events) if events else None,
        "turn_response_ms_median": statistics.median(latencies) if latencies else None,
        "turn_response_ms_p95": latencies[math.ceil(0.95 * len(latencies)) - 1]
        if latencies
        else None,
        "latency_definition": "server turn start to receipt; not measured model-only latency",
        "input_tokens": None,
        "output_tokens": None,
        "inference_cost_usd": None,
        "cost_measurement": "unavailable_not_zero",
        "wallet_settlement": "none_test_chips_only",
    }


def evaluation(matches):
    return {
        "metrics": metrics(matches),
        "cohorts": {
            track: metrics([m for m in matches if evaluation_track(m) == track])
            for track in ("autonomous", "assisted", "synthetic", "human_or_unverified")
        },
        "interventions": sum(len(m.get("interventions", [])) for m in matches),
        "rule": "Any recorded intervention affects the entire episode, including opponents. Only autonomous cohort belongs in unassisted evaluations.",
    }


def paired_report(values):
    n = len(values)
    mean = statistics.mean(values) if n else None
    margin = 1.96 * statistics.stdev(values) / math.sqrt(n) if n >= 30 else None
    return {
        "paired_seeds": n,
        "mean_chips_per_hand": mean,
        "approx_95pct_ci": [mean - margin, mean + margin] if margin is not None else None,
        "method": "Normal approximation over independent seed-pair means; unavailable below 30 pairs",
        "not_general_reasoning_rank": True,
        "is_demo": True,
    }


def simulation_comparison(context, samples=256):
    """Restricted preflop demonstration, not a poker solver or an optimality label.

    Sample UNKNOWN cards uniformly from the visible information alone. Both
    candidates use identical samples and a fixed check/call continuation policy.
    """
    integer(samples, 64, 10000)
    if (
        context.get("street") != "preflop"
        or context.get("history")
        or context.get("seat") != 0
        or context.get("board")
        or len(context.get("stacks", [])) != 2
        or not {"call", "fold"}.issubset(context.get("legal_actions", {}))
    ):
        raise ValueError("comparison_requires_initial_heads_up_small_blind_context")
    initial = [a + b for a, b in zip(context["stacks"], context["committed"])]
    visible = context["cards"]
    deck = [r + suit for r, suit in DECK]
    if len(visible) != 2 or len(set(visible)) != 2 or not set(visible).issubset(deck):
        raise ValueError("invalid_visible_cards")
    signature = hashlib.sha256(canonical(context).encode()).hexdigest()
    rng = random.Random("comparison-v1:" + signature)
    differences = []
    for _ in range(samples):
        unknown = [card for card in deck if card not in visible]
        rng.shuffle(unknown)
        state = new_hand("unused-hidden-state", initial)
        state["holes"] = [visible[:], [unknown.pop(), unknown.pop()]]
        state["deck"] = unknown
        rewards = []
        for candidate in ("call", "fold"):
            result = step(state, 0, candidate)
            while not result["resolved"]:
                seat = result["actor"]
                actions = observation(result, seat)["legal_actions"]
                result = step(result, seat, "check" if "check" in actions else "call")
            rewards.append(result["stacks"][0] - initial[0])
        differences.append(rewards[0] - rewards[1])
    estimate = paired_report(differences)
    low, high = estimate["approx_95pct_ci"]
    winner = "call" if low > 0 else "fold" if high < 0 else None
    return {
        "id": "simulation-" + signature[:20],
        "context_hash": signature,
        "method": "controlled_simulation",
        "samples": samples,
        "hidden_prior": "uniform unseen cards; no observed opponent/deck input",
        "continuation_policy": "both seats check/call; not equilibrium or optimal play",
        "compared": ["call", "fold"],
        "mean_call_minus_fold": estimate["mean_chips_per_hand"],
        "approx_95pct_ci": [low, high],
        "chosen": winner,
        "abstained": winner is None,
        "is_demo": True,
    }


SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "OpenVegas decision v1 (local no-stakes pilot)",
    "type": "object",
    "required": [
        "schema_version",
        "episode_id",
        "decision_id",
        "event_index",
        "actor",
        "observation",
        "legal_actions",
        "action",
        "accepted",
        "resulting_observation",
        "episode_ended",
        "usage",
        "provenance",
    ],
    "properties": {
        "schema_version": {"const": "openvegas.decision.v1"},
        "episode_id": {"type": "string"},
        "decision_id": {"type": "string"},
        "event_index": {"type": "integer", "minimum": 0},
        "actor": {"type": "object", "required": ["seat", "source_type"]},
        "observation": {"type": "object"},
        "resulting_observation": {"type": "object"},
        "legal_actions": {"type": "object"},
        "action": {"type": "object"},
        "accepted": {"type": "boolean"},
        "episode_ended": {"type": "boolean"},
        "usage": {
            "type": "object",
            "required": ["latency_ms", "input_tokens", "output_tokens", "inference_cost_usd"],
        },
        "provenance": {"type": "object", "required": ["is_demo"]},
    },
}


def write_bundle(destination, matches, purpose, report=None, *, demo_comparisons=False):
    """Only local transfer. Real records must come from current purpose-gated service export."""
    if purpose not in {"internal_evaluation", "training", "evaluation_delivery"} or not matches:
        raise ValueError("purpose_and_matches_required")
    if len({m["id"] for m in matches}) != len(matches):
        raise ValueError("duplicate_episodes_not_allowed")
    if demo_comparisons and not all(m["is_demo"] for m in matches):
        raise ValueError("demo_comparisons_require_synthetic_data")
    for match in matches:
        replay(match)
        validate_interventions(match.get("interventions", []), match["events"], match["id"])
        if not match["state"]["resolved"]:
            raise ValueError("completed_match_required")
        if any(
            p["rights"].get("purpose") != purpose or not p["rights"].get("record_id")
            for p in match["players"]
        ):
            raise PermissionError("missing_purpose_rights")
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)

    def write(name, value, lines=False):
        path = destination / name
        text = (
            "".join(canonical(v) + "\n" for v in value)
            if lines
            else json.dumps(value, indent=2, allow_nan=False) + "\n"
        )
        path.write_text(text)
        path.chmod(0o600)

    episodes = []
    decisions = []
    audits = []
    agents = []
    for match in matches:
        s = match["state"]
        group = hashlib.sha256(str(match["seed"]).encode()).hexdigest()
        split = "evaluation" if int(group[:8], 16) % 5 == 0 else "training"
        # Entire seed group, including swapped seats, belongs to one split.
        episodes.append(
            {
                "episode_id": match["id"],
                "split_group": group,
                "split": split,
                "engine_version": VERSION,
                "initial": s["initial"],
                "final": s["stacks"],
                "payouts": s["payouts"],
                "pots": s["pots"],
                "is_demo": match["is_demo"],
                "rewards": [a - b for a, b in zip(s["stacks"], s["initial"])],
                "unit": "test_chips",
                "evaluation_track": evaluation_track(match),
            }
        )
        for e in match["events"]:
            row = deepcopy(e)
            row["provenance"]["rights_record_id"] = match["players"][e["actor"]["seat"]]["rights"][
                "record_id"
            ]
            decisions.append(row)
        agents.extend(
            {"episode_id": match["id"], "seat": i, **p} for i, p in enumerate(match["players"])
        )
        audits.append(
            {
                "episode_id": match["id"],
                "seed": match["seed"],
                "initial": s["initial"],
                "button": s["button"],
            }
        )
    write("episodes.jsonl", episodes, True)
    write("decisions.jsonl", decisions, True)
    write("agents.jsonl", agents, True)
    write("private-replay.jsonl", audits, True)
    write("interventions.jsonl", [i for m in matches for i in m.get("interventions", [])], True)
    preferences = []
    judgments = []
    if demo_comparisons:
        for match in matches[:4]:
            event = match["events"][0]
            context = event["observation"]
            judgment = simulation_comparison(context)
            judgments.append(
                {**judgment, "episode_id": match["id"], "decision_id": event["decision_id"]}
            )
            if judgment["abstained"]:
                continue
            chosen = judgment["chosen"]
            label = preference(
                context,
                {"type": chosen},
                {"type": "fold" if chosen == "call" else "call"},
                "controlled_simulation",
                "judgments.jsonl:" + judgment["id"],
                "check-call-simulation-v1",
            )
            preferences.append(
                {
                    **label,
                    "episode_id": match["id"],
                    "decision_id": event["decision_id"],
                    "is_demo": True,
                }
            )
    write("preferences.jsonl", preferences, True)
    write("judgments.jsonl", judgments, True)
    write("schema.json", SCHEMA)
    write("evaluation_report.json", {**evaluation(matches), "controlled_demo": report})
    card = destination / "dataset_card.md"
    card.write_text(
        "# OpenVegas Arena local pilot\n\n"
        "No real wagering or inference funding. Test chips are not redeemable.\n"
        "Observations contain only the acting player's information at that time.\n"
        "observation is the canonical game state at submission, excluding transport metadata.\n"
        "For stale submissions/timeouts client_observation is null: no claim of a known client input.\n"
        "private-replay.jsonl is privileged audit material: never supply it to agents or training prompts.\n"
        "Seed groups, including swapped seats, stay together across training/evaluation splits.\n"
        "This sample is not a licensed commercial delivery. Participants' grants do not establish third-party model-output resale rights.\n"
        "No private model reasoning, emails, wallet/payment data or credential fields are exported.\n"
        "Demo labels are explicit; missing usage is null. Simulation preferences use a uniform hidden-card prior and check/call opponents, not optimal play.\n"
        "Empty preferences means no comparisons; uncertainty-crossing-zero judgments abstain. Each label links to its episode and judging evidence.\n"
        "Human interventions are authenticated controller events, not verified human identity. They mark the entire hand assisted; both seats are excluded from autonomous metrics.\n"
        "Pauses do not stop game clocks. Timeouts remain server actions, not model decisions. No private reasoning is collected.\n"
        "Buyer acceptance and rights review remain required. JSONL is used; no Parquet output is claimed.\n"
    )
    card.chmod(0o600)
    files = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in destination.iterdir()
        if p.is_file()
    }
    write(
        "manifest.json",
        {
            "schema_version": "openvegas.bundle.v2",
            "purpose": purpose,
            "episodes": len(episodes),
            "decisions": len(decisions),
            "files": files,
            "commercial_release": False,
            "all_demo": all(m["is_demo"] for m in matches),
        },
    )


def export_matches(arena, match_ids, destination, purpose):
    write_bundle(destination, [arena.authorized_export(i, purpose) for i in match_ids], purpose)


def verify_bundle(directory):
    """Verify local artifacts without executing data or contacting any service."""
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    required = {
        "episodes.jsonl",
        "decisions.jsonl",
        "agents.jsonl",
        "private-replay.jsonl",
        "preferences.jsonl",
        "judgments.jsonl",
        "schema.json",
        "dataset_card.md",
        "evaluation_report.json",
    }
    if manifest.get("schema_version") == "openvegas.bundle.v2":
        required.add("interventions.jsonl")
    elif manifest.get("schema_version") != "openvegas.bundle.v1":
        raise ValueError("unsupported_bundle_version")
    if set(manifest.get("files", {})) != required:
        raise ValueError("unexpected_bundle_files")
    for name, expected in manifest["files"].items():
        path = directory / name
        if path.is_symlink() or path.stat().st_size > 128 * 1024 * 1024:
            raise ValueError("unsafe_or_oversized_bundle_file")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("bundle_checksum_mismatch")

    def lines(name):
        return [json.loads(row) for row in (directory / name).read_text().splitlines()]

    episodes = lines("episodes.jsonl")
    decisions = lines("decisions.jsonl")
    interventions = lines("interventions.jsonl") if "interventions.jsonl" in required else []
    agents = lines("agents.jsonl")
    seeds = {r["episode_id"]: r for r in lines("private-replay.jsonl")}
    by_id = {e["episode_id"]: e for e in episodes}
    if len(by_id) != len(episodes) or set(seeds) != set(by_id):
        raise ValueError("episode_identity_mismatch")
    if len(episodes) != manifest["episodes"] or len(decisions) != manifest["decisions"]:
        raise ValueError("bundle_counts_mismatch")
    if any(d["episode_id"] not in by_id for d in decisions):
        raise ValueError("orphan_decision")
    if any(i["episode_id"] not in by_id for i in interventions):
        raise ValueError("orphan_intervention")
    if any(p["episode_id"] not in by_id for p in agents):
        raise ValueError("orphan_agent")
    decision_ids = {d["decision_id"]: d for d in decisions}
    if len(decision_ids) != len(decisions):
        raise ValueError("duplicate_decision")
    reconstructed = []
    for episode in episodes:
        seed = seeds[episode["episode_id"]]
        state = new_hand(seed["seed"], seed["initial"], seed["button"])
        events = [d for d in decisions if d["episode_id"] == episode["episode_id"]]
        controls = [i for i in interventions if i["episode_id"] == episode["episode_id"]]
        validate_interventions(controls, events, episode["episode_id"])
        participants = [p for p in agents if p["episode_id"] == episode["episode_id"]]
        if (
            len(participants) != len(episode["initial"])
            or {p["seat"] for p in participants} != set(range(len(episode["initial"])))
            or any(p["source_type"] not in {"human", "agent"} for p in participants)
        ):
            raise ValueError("participant_identity_mismatch")
        match = {
            "events": events,
            "interventions": controls,
            "is_demo": episode["is_demo"],
            "players": participants,
        }
        reconstructed.append(match)
        if "interventions.jsonl" in required and episode.get(
            "evaluation_track"
        ) != evaluation_track(match):
            raise ValueError("evaluation_track_mismatch")
        for event in events:
            if event["accepted"]:
                state = step(
                    state,
                    event["actor"]["seat"],
                    event["action"]["type"],
                    event["action"]["amount_to"],
                )
        replay({"seed": seed["seed"], "state": state, "events": events})
        group = hashlib.sha256(str(seed["seed"]).encode()).hexdigest()
        split = "evaluation" if int(group[:8], 16) % 5 == 0 else "training"
        if (
            not state["resolved"]
            or episode["initial"] != state["initial"]
            or episode["final"] != state["stacks"]
            or episode["payouts"] != state["payouts"]
            or episode["pots"] != state["pots"]
            or episode["split_group"] != group
            or episode["split"] != split
        ):
            raise ValueError("episode_replay_mismatch")
    if "interventions.jsonl" in required:
        report = json.loads((directory / "evaluation_report.json").read_text())
        if any(report.get(k) != v for k, v in evaluation(reconstructed).items()):
            raise ValueError("evaluation_cohort_mismatch")
    for label in lines("preferences.jsonl"):
        event = decision_ids[label["decision_id"]]
        if (
            label["episode_id"] != event["episode_id"]
            or json.loads(label["prompt"]) != event["observation"]
        ):
            raise ValueError("preference_context_mismatch")
        judge = label["judge"]
        checked = preference(
            event["observation"],
            json.loads(label["chosen"]),
            json.loads(label["rejected"]),
            judge["method"],
            judge["evidence_ref"],
            judge["judge_id"],
        )
        if any(label[k] != v for k, v in checked.items()):
            raise ValueError("preference_validation_failed")
    return {
        "episodes": len(episodes),
        "decisions": len(decisions),
        "checksums": True,
        "replay": True,
        "commercial_rights_verified": False,
        "native_ux_verified": False,
    }


def demo_bundle(destination, pairs=30):
    integer(pairs, 1, 1000)
    matches = []
    deltas = []
    for pair in range(pairs):
        pair_deltas = []
        for swap in (0, 1):
            seed = f"arena-demo-seed-{pair}"
            s = new_hand(seed, [200, 200])
            events = []
            rng = random.Random(f"policy-{pair}-{swap}")
            while not s["resolved"]:
                seat = s["actor"]
                before = observation(s, seat)
                options = before["legal_actions"]
                action = (
                    ("check" if "check" in options else "call")
                    if seat == swap
                    else rng.choice(list(options))
                )
                amount = options["raise"]["min_to"] if action == "raise" else None
                next_state = step(s, seat, action, amount)
                events.append(
                    {
                        "schema_version": "openvegas.decision.v1",
                        "episode_id": f"demo-{pair}-{swap}",
                        "decision_id": f"demo-{pair}-{swap}:{len(events)}",
                        "event_index": len(events),
                        "actor": {"seat": seat, "source_type": "agent"},
                        "observation": before,
                        "legal_actions": options,
                        "action": {"type": action, "amount_to": amount},
                        "accepted": True,
                        "reason": None,
                        "resulting_observation": observation(next_state, seat),
                        "episode_ended": next_state["resolved"],
                        "usage": {
                            "latency_ms": None,
                            "input_tokens": None,
                            "output_tokens": None,
                            "inference_cost_usd": None,
                        },
                        "provenance": {"is_demo": True, "collection": "scripted_policy_not_llm"},
                    }
                )
                s = next_state
            players = [
                {
                    "source_type": "agent",
                    "configuration": {
                        "values": {
                            "code_version": "check-call-v1" if i == swap else "random-legal-v1"
                        },
                        "verification": "local_script",
                    },
                    "rights": {"purpose": "internal_evaluation", "record_id": "synthetic-demo"},
                }
                for i in range(2)
            ]
            matches.append(
                {
                    "id": f"demo-{pair}-{swap}",
                    "seed": seed,
                    "state": s,
                    "events": events,
                    "players": players,
                    "is_demo": True,
                }
            )
            pair_deltas.append(s["stacks"][swap] - 200)
        deltas.append(statistics.mean(pair_deltas))
    write_bundle(
        destination, matches, "internal_evaluation", paired_report(deltas), demo_comparisons=True
    )
    return matches
