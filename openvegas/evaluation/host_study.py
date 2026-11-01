"""Headless model study on the existing Arena service. No live-credit imports."""

import argparse
import csv
import hashlib
import json
import os
import statistics
import time
import zipfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from openvegas.arena import dataset
from openvegas.arena.autonomy import Autonomy
from openvegas.arena.service import Arena

from .host_agent import (
    ACTION_SCHEMA,
    SYSTEM,
    HostAgent,
    HostFailure,
    preflight,
    prompt_for,
    schema_for,
)

VERSION = "arena.host-study.v2"
MODELS = {"claude": "haiku", "codex": "gpt-6-luna"}
ROOT = Path(__file__).resolve().parents[2]
SOURCES = [
    "openvegas/evaluation/host_agent.py",
    "openvegas/evaluation/host_study.py",
    "openvegas/evaluation/gateway_agent.py",
    "openvegas/evaluation/gateway_study.py",
    "openvegas/arena/engine.py",
    "openvegas/arena/service.py",
    "openvegas/arena/autonomy.py",
    "openvegas/arena/dataset.py",
    "openvegas/casino/poker.py",
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def append(path, data):
    with path.open("a", encoding="utf8") as f:
        f.write(dataset.canonical(data) + "\n")
        f.flush()
        os.fsync(f.fileno())
    path.chmod(0o600)


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@contextmanager
def locked(home):
    path = home / "run.lock"
    with path.open("x") as f:
        f.write(str(os.getpid()))
    try:
        yield
    finally:
        path.unlink()


def prepare(home, output, *, models=None, inference=None):
    if home.exists() or output.exists():
        raise ValueError("new_study_directories_required")
    home.mkdir(parents=True, mode=0o700)
    output.mkdir(parents=True, mode=0o700)
    (home / "work").mkdir(mode=0o700)
    agents = [
        {
            "id": f"{host}-{i:02d}",
            "host": host,
            "requested_model": (models or MODELS)[host],
            "starting_balance": 500,
            "unit": "simulated_V",
            "engine_unit": "test_chips",
            "redeemable": False,
            "status": "not_run",
            "final_balance": None,
            "available_simulated_v": 500,
            "settled_hands": 0,
            "matches": [],
            "table": f"{host}-table-{(i + 1) // 2:02d}",
            "seat": (i - 1) % 2,
        }
        for host in ("claude", "codex")
        for i in range(1, 11)
    ]
    save(output / "roster.json", agents)
    save(
        output / "protocol.json",
        {
            "version": VERSION,
            "created_at": time.time(),
            "agents": 20,
            "initial_total_simulated_v": 10000,
            "real_v_issued": 0,
            "max_decisions_per_table": 64,
            "max_model_calls": 1280,
            "hands_per_agent": 2,
            "buy_in_per_hand": 250,
            "blinds": [1, 2],
            "max_call_seconds": 90,
            "models": models or MODELS,
            "inference": inference or {"source": "existing_host_subscription"},
            "memory": "fresh invocation with full public hand history per decision",
            "pairing": "five self-play pairs per host, two fresh hands with swapped seats; no cross-host strength comparison",
            "participation": "assigned poker hand, not a voluntary-entry experiment",
            "source_hashes": {p: sha((ROOT / p).read_bytes()) for p in SOURCES},
            "system_instruction": SYSTEM,
            "action_schema": ACTION_SCHEMA,
            "structured_output": "Nested action union generated from legal actions, with every integer legal raise available. Invalid representations are constrained, strategy is not.",
            "commercial_release": False,
            "external_submission": False,
        },
    )
    save(home / "study.json", {"output": str(output.resolve()), "tables": {}, "grants": {}})
    (output / "calls.jsonl").touch(mode=0o600)
    (output / "run-events.jsonl").touch(mode=0o600)
    refresh(output)


def run_host(home, host, *, adapter=None, readiness=None, max_tables=5):
    with locked(home):
        private = json.loads((home / "study.json").read_text())
        output = Path(private["output"])
        protocol = json.loads((output / "protocol.json").read_text())
        current = {p: sha((ROOT / p).read_bytes()) for p in SOURCES}
        if current != protocol["source_hashes"]:
            raise ValueError("study_sources_changed_create_new_study")
        work = home / "work" / host
        work.mkdir(parents=True, exist_ok=True, mode=0o700)
        status = readiness if readiness is not None else preflight(host, work)
        append(
            output / "run-events.jsonl", {"type": "preflight", "recorded_at": time.time(), **status}
        )
        if not status.get("ready", status.get("subscription_ready", False)):
            refresh(output)
            return {"host": host, "status": "blocked", "reason": status["reason"]}
        roster = json.loads((output / "roster.json").read_text())
        arena = Arena(home / "arena.sqlite", turn_seconds=300)
        auto = Autonomy(arena)
        adapter = adapter or HostAgent(host, protocol["models"][host], work)
        from .gateway_agent import GatewayAgent
        collection_mode = ('native_cli_model_decision' if type(adapter) is HostAgent else
            'native_gateway_model_decision' if type(adapter) is GatewayAgent and not adapter.study.synthetic else
            'scripted_test_fixture')
        completed = []
        groups = sorted({a["table"] for a in roster if a["host"] == host})[:max_tables]
        schedule = [(group, hand) for group in groups for hand in range(2)]
        for group, hand in schedule:
            table = f"{group}-hand-{hand + 1}"
            if table in private["tables"]:
                continue  # A partial or failed table is never silently retried/refilled.
            participants = [a for a in roster if a["table"] == group][:: 1 if hand == 0 else -1]
            if any(p["status"] == "incomplete" for p in participants):
                continue
            for p in participants:
                if p["id"] not in private["grants"]:
                    private["grants"][p["id"]] = auto.issue(
                        chips=250,
                        max_matches=2,
                        entry_budget=500,
                        lifetime_seconds=14400,
                        config={
                            "model": p["requested_model"],
                            "code_version": VERSION,
                            "prompt_hash": sha(SYSTEM.encode()),
                            "budget_label": "500-simulated-V",
                        },
                    )
            grants = [private["grants"][p["id"]] for p in participants]
            views = [
                auto.find(g["agent_token"], f"entry-{p['id']}-{hand}", expected_revision=0)
                for p, g in zip(participants, grants)
            ]
            match_id = views[-1]["observation"]["match_id"]
            private["tables"][table] = {"match_id": match_id}
            save(home / "study.json", private)
            for p in participants:
                if p["available_simulated_v"] < 250:
                    raise ValueError("study_bankroll_exhausted")
                p["available_simulated_v"] -= 250
                p["matches"].append(match_id)
                p.update(status="running", cli_version=status["version"])
            save(output / "roster.json", roster)
            failure = None
            for index in range(protocol["max_decisions_per_table"]):
                views = [auto.observe(g["agent_token"])["observation"] for g in grants]
                if views[0]["resolved"]:
                    break
                seat = views[0]["actor"]
                observation = views[seat]
                # The model sees the same canonical public observation exported by Arena.
                public = {
                    k: observation[k]
                    for k in dataset.observation(dataset.new_hand("schema-only", [250, 250]), seat)
                    if k in observation
                }
                agent = participants[seat]
                record = {
                    "id": f"{table}:{index}",
                    "table": table,
                    "match_id": match_id,
                    "agent_id": agent["id"],
                    "host": host,
                    "cli_version": status["version"],
                    "requested_model": agent["requested_model"],
                    "recorded_at": time.time(),
                    "collection_mode": collection_mode,
                    "prompt": prompt_for(agent["id"], public),
                    "observation": public,
                    "action_schema": schema_for(public),
                    "action": None,
                    "accepted": False,
                    "error": None,
                }
                try:
                    action, usage = adapter.decide(agent["id"], public)
                    record.update(action=action, usage=usage)
                    result = auto.act(
                        grants[seat]["agent_token"],
                        match_id,
                        revision=0,
                        version=public["version"],
                        action=action["type"],
                        amount=action["amount_to"],
                        request_id=f"decision-{index:04d}",
                    )
                    record.update(accepted=result["accepted"], error=result["reason"])
                    if not result["accepted"]:
                        failure = "invalid_action"
                except HostFailure as exc:
                    record.update(error=exc.code, usage=exc.usage)
                    failure = exc.code
                except (ValueError, PermissionError):
                    record["error"] = failure = "service_rejected_action"
                append(output / "calls.jsonl", record)
                print(
                    json.dumps(
                        {
                            "table": table,
                            "agent": agent["id"],
                            "step": index,
                            "action": record["action"],
                            "error": record["error"],
                        }
                    ),
                    flush=True,
                )
                if failure:
                    break
            final = auto.observe(grants[0]["agent_token"])["observation"]
            # Retain unresolved state; never fabricate a fold or final balance on model failure.
            if final["resolved"]:
                for seat, (p, g) in enumerate(zip(participants, grants)):
                    auto.consent(
                        g["controller_token"],
                        match_id,
                        "internal_evaluation",
                        "owner-study-20261028",
                    )
                    p["available_simulated_v"] += final["stacks"][seat]
                    p["settled_hands"] += 1
                    p["status"] = "settled" if p["settled_hands"] == 2 else "awaiting_next_hand"
                    if p["status"] == "settled":
                        p.update(
                            final_balance=p["available_simulated_v"],
                            net_simulated_v=p["available_simulated_v"] - 500,
                        )
                completed.append(match_id)
                destination = output / f"poker-{table}"
                dataset.export_matches(arena, [match_id], destination, "internal_evaluation")
                dataset.verify_bundle(destination)
            else:
                for seat, p in enumerate(participants):
                    p.update(
                        status="incomplete",
                        error=failure or "decision_limit",
                        uncommitted_balance=final["stacks"][seat],
                        committed=final["committed"][seat],
                    )
            private["tables"][table]["status"] = participants[0]["status"]
            save(home / "study.json", private)
            save(output / "roster.json", roster)
            if failure and failure != "invalid_action":
                break  # Stop a broken/auth-limited host instead of spending on repeated failures.
        refresh(output)
        return {"host": host, "new_settled_tables": len(completed), "summary": summary(output)}


def summary(output):
    from decimal import Decimal
    protocol = json.loads((output / 'protocol.json').read_text())
    funded = protocol.get('inference', {}).get('source') == 'admin_sponsored_openvegas_gateway'
    roster = json.loads((output / "roster.json").read_text())
    calls = lines(output / "calls.jsonl")
    settled = [a for a in roster if a["status"] == "settled"]
    invoked = {
        c["agent_id"]
        for c in calls
        if c.get("action") is not None and c["collection_mode"] in {'native_cli_model_decision', 'native_gateway_model_decision'}
    }
    measured = [
        c["usage"]["latency_ms"] for c in calls if c.get("usage", {}).get("latency_ms") is not None
    ]
    return {
        "planned_agents": len(roster),
        "model_responded_agents": len(invoked),
        "settled_agents": len(settled),
        "settled_tables": len(list(output.glob("poker-*"))),
        "completed_20_agent_study": len(settled) == 20 and len(invoked) == 20,
        "agent_statuses": dict(Counter(a["status"] for a in roster)),
        "host_calls": dict(Counter(c["host"] for c in calls)),
        "actions": dict(Counter(c["action"]["type"] for c in calls if c["action"])),
        "accepted_actions": sum(c["accepted"] for c in calls),
        "errors": dict(Counter(c["error"] for c in calls if c["error"])),
        "latency_median_ms": statistics.median(measured) if measured else None,
        "latency_max_ms": max(measured) if measured else None,
        "initial_simulated_v": 10000,
        "settled_net_simulated_v": sum(a["net_simulated_v"] for a in settled),
        "real_v_issued_or_wagered": 0,
        "cash_inference_cost_usd": None,
        "cash_cost_note": ('Admin-sponsored gateway inference; $V debits are separate from simulated wagers, not supplier invoices.'
                           if funded else 'Existing subscriptions; not measured cash expense, not zero-cost inference.'),
        "inference": protocol.get('inference', {'source': 'existing_host_subscription'}),
        "gateway_debit_v": str(sum((Decimal(c.get('usage', {}).get('inference_debit_v', '0')) for c in calls
            if c['collection_mode'] == 'native_gateway_model_decision'), Decimal(0))),
        "public_release": False,
    }


def refresh(output):
    report = summary(output)
    funded = report['inference']['source'] == 'admin_sponsored_openvegas_gateway'
    transport = 'gateway API' if funded else 'native CLI'
    save(output / "summary.json", report)
    roster = json.loads((output / "roster.json").read_text())
    with (output / "agent-balances.csv").open("w", newline="") as f:
        fields = [
            "id",
            "host",
            "requested_model",
            "starting_balance",
            "available_simulated_v",
            "settled_hands",
            "final_balance",
            "status",
        ]
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(roster)
    (output / "FINDINGS.md").write_text(
        "# Observed findings\n\n"
        f"Status: {'completed feasibility sample' if report['completed_20_agent_study'] else 'PARTIAL; not a completed twenty-agent study'}.\n\n"
        f"{report['model_responded_agents']} agents returned model decisions; "
        f"{report['settled_tables']} tables settled; {report['accepted_actions']} actions accepted.\n"
        f"Actions: {report['actions']}. Retained errors: {report['errors']}.\n"
        f"Measured end-to-end {transport} decision latency: median {report['latency_median_ms']} ms; "
        f"maximum {report['latency_max_ms']} ms. Includes transport overhead.\n"
        f"Net transfer across settled participants: {report['settled_net_simulated_v']} simulated $V.\n\n"
        "No claim of model superiority, optimal decisions, stable risk appetite or real-world transfer. "
        "Two hands per agent and repeated instances of the same model are not independent model populations. "
        "Games were assigned; this does not measure voluntary participation. "
        "Wins and losses are outcomes, not preference labels. No training benefit has been measured. "
        "All incomplete/failed calls remain in calls.jsonl. No real $V was issued or wagered.\n"
        + (f"Recorded inference debit: {report['gateway_debit_v']} $V, separate from simulated wagers. "
           "This is wallet usage, not a supplier invoice.\n" if funded else
           "No OpenVegas inference-wallet debit; personal inference entitlement is separate.\n")
    )
    (output / "README.md").write_text(
        "# OpenVegas Arena: 20-agent headless feasibility study\n\n"
        "Prepared for owner review before any AI-lab submission. NOT SENT.\n"
        "A controlled environment and dataset for evaluating how agents allocate budgets, "
        "choose opportunities, and manage risk under uncertainty. This sample tests poker execution only.\n\n"
        "## Read first\nSee FINDINGS.md and summary.json for actual completion, including blocked hosts.\n"
        "Each of 10 Claude and 10 Codex identities starts with 500 simulated $V (10,000 total). "
        "Each plays two 250-chip buy-ins with swapped seats; the uncommitted reserve and winnings carry "
        "forward and no agent receives additional chips. These are non-redeemable test chips, unrelated to live wallets or inference credits. "
        f"The headless {transport} adapter selects actions; the existing Arena backend validates and settles them. "
        "No terminal UI automation, extra terminal windows or model-generated executable code.\n\n"
        "## Files\n"
        "- protocol.json: instructions, model selectors, limits and exact source hashes.\n"
        "- roster.json: all planned identities, statuses and settled/locked balances.\n"
        "- calls.jsonl: exact public prompt/observation, action, acceptance, failures and measured usage.\n"
        "- run-events.jsonl: sanitized login/version checks; no tokens or account identifiers.\n"
        "- poker-*/: existing purpose-gated Arena JSONL export with its own replay verifier.\n"
        "- poker-*/private-replay.jsonl: privileged audit seeds. Never feed to evaluated agents or training inputs.\n"
        "- manifest.json: exact delivery-file hashes. Runtime SQLite and capability files are excluded.\n\n"
        "## Interpretation and rights\n"
        "Ten identities per host are repeated instances, NOT ten distinct models or ten human developers. "
        "CLI versions/models and prompts are part of the tested system. Prompt history is reconstructed "
        "from public game state; no private reasoning is requested or exported. Model selectors may be "
        "aliases; observed_model is populated only when the host reports it. Token accounting differs "
        "between hosts; cash inference cost is unknown. Pricing-equivalent figures are not invoices.\n"
        "Owner authorization covers local internal evaluation and preparing this package. "
        "External sharing, provider-output licensing, privacy/legal review and buyer acceptance "
        "are not established by this run. No commercial resale rights or public release claimed. "
        "Do not use any of these exposed scenarios as secret held-out evaluations after sharing.\n\n"
        "## Reproduce\n"
        "From the OpenVegas checkout with its .venv, run: `python -m openvegas.evaluation.host_study "
        "verify --output <this-folder>`. Run the study tests with "
        "`pytest tests/test_arena/test_host_study.py`. To collect a NEW study, use `prepare` "
        "with new home/output paths, then `run --home <private-home> --host codex` and `--host claude`. "
        + ("For company-funded collection use `python -m openvegas.evaluation.gateway_study --help`, "
           "new directories, an explicit inference ceiling and a scoped OpenVegas sign-in. "
           "No personal Claude/Codex subscription is required by that adapter. " if funded else
           "Existing subscriptions must be signed in for the personal CLI adapter. ")
        + "Runs never buy credits or retry failed tables.\n"
    )
    save(
        output / "data-dictionary.json",
        {
            "unit": "simulated_V is display terminology for non-redeemable integer test_chips",
            "starting_balance": "Exactly 500 per distinct agent, no refill",
            "final_balance": "Null unless the hand settled; not refunded on failure",
            "net_simulated_v": "Final minus initial, not gross pot payout",
            "prompt": ("Exact gateway user input, containing public observation and action schema" if funded else
                       "Exact user input to CLI, plus protocol.system_instruction as Claude system prompt"),
            "observation": "Acting player's cards and public history; no other private cards or seed",
            "usage": "Host-reported tokens plus measured wall latency; no private reasoning",
            "error": "Sanitized error class; retained, never converted to a model action",
            "status": "not_run, running, incomplete or settled",
            "provider_calls": ("One gateway request per recorded decision; never retried automatically" if funded else
                               "CLI invocations; a host may perform more than one model request internally"),
            "model_provenance": "requested_model is configuration; observed_model only when host reports it",
        },
    )
    files = {
        str(p.relative_to(output)): sha(p.read_bytes())
        for p in sorted(output.rglob("*"))
        if p.is_file() and p != output / "manifest.json"
    }
    save(
        output / "manifest.json", {"version": VERSION, "files": files, "commercial_release": False}
    )


def verify(output):
    manifest = json.loads((output / "manifest.json").read_text())
    actual = {
        str(p.relative_to(output))
        for p in output.rglob("*")
        if p.is_file() and p != output / "manifest.json"
    }
    if manifest["version"] not in {"arena.host-study.v1", VERSION} or actual != set(
        manifest["files"]
    ):
        raise ValueError("package_file_inventory_mismatch")
    for name, digest in manifest["files"].items():
        p = output / name
        if p.is_symlink() or sha(p.read_bytes()) != digest:
            raise ValueError("package_checksum_mismatch")
    roster = json.loads((output / "roster.json").read_text())
    if len(roster) != 20 or len({a["id"] for a in roster}) != 20:
        raise ValueError("roster_count_mismatch")
    if Counter(a["host"] for a in roster) != {"claude": 10, "codex": 10}:
        raise ValueError("host_count_mismatch")
    if any(a["starting_balance"] != 500 or a["redeemable"] for a in roster):
        raise ValueError("allocation_mismatch")
    calls = lines(output / "calls.jsonl")
    if len({c["id"] for c in calls}) != len(calls):
        raise ValueError("duplicate_call")
    for c in calls:
        if c["agent_id"] not in {a["id"] for a in roster} or c["prompt"] != prompt_for(
            c["agent_id"], c["observation"]
        ):
            raise ValueError("prompt_provenance_mismatch")
        if {"seed", "deck", "holes", "burned"} & c["observation"].keys():
            raise ValueError("private_state_leak")
        agent = next(a for a in roster if a["id"] == c["agent_id"])
        if (
            c["host"] != agent["host"]
            or c["requested_model"] != agent["requested_model"]
            or c["match_id"] not in agent["matches"]
        ):
            raise ValueError("agent_provenance_mismatch")
        hand = agent["matches"].index(c["match_id"])
        if c["observation"]["seat"] != (agent["seat"] if hand == 0 else 1 - agent["seat"]):
            raise ValueError("agent_seat_provenance_mismatch")
        if manifest["version"] == VERSION and c.get("action_schema") != schema_for(
            c["observation"]
        ):
            raise ValueError("decoding_schema_provenance_mismatch")
    episode_by_id = {}
    for p in output.glob("poker-*"):
        dataset.verify_bundle(p)
        for episode in lines(p / "episodes.jsonl"):
            if episode["episode_id"] in episode_by_id:
                raise ValueError("duplicate_exported_episode")
            episode_by_id[episode["episode_id"]] = episode
        decisions = {d["decision_id"]: d for d in lines(p / "decisions.jsonl")}
        for d in decisions.values():
            matching = [
                c
                for c in calls
                if c["match_id"] == d["episode_id"]
                and c["observation"]["version"] == d["submitted_version"]
                and c["accepted"]
            ]
            if (
                len(matching) != 1
                or matching[0]["observation"] != d["observation"]
                or matching[0]["action"] != d["action"]
            ):
                raise ValueError("model_to_engine_provenance_mismatch")
    for agent in roster:
        if len(agent["matches"]) > 2 or len(set(agent["matches"])) != len(agent["matches"]):
            raise ValueError("entry_budget_exceeded")
        expected = 500 - 250 * len(agent["matches"])
        resolved = 0
        for hand, match_id in enumerate(agent["matches"]):
            if match_id not in episode_by_id:
                continue
            episode = episode_by_id[match_id]
            seat = agent["seat"] if hand == 0 else 1 - agent["seat"]
            if episode["initial"] != [250, 250]:
                raise ValueError("unexpected_buy_in")
            expected += episode["final"][seat]
            resolved += 1
        if expected != agent["available_simulated_v"] or resolved != agent["settled_hands"]:
            raise ValueError("agent_balance_reconciliation_mismatch")
        if agent["status"] == "settled" and (
            resolved != 2
            or agent["final_balance"] != expected
            or agent["net_simulated_v"] != expected - 500
        ):
            raise ValueError("agent_settlement_mismatch")
    report = summary(output)
    if (
        report != json.loads((output / "summary.json").read_text())
        or report["settled_net_simulated_v"] != 0
    ):
        raise ValueError("summary_or_conservation_mismatch")
    return {"integrity_valid": True, **report}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["prepare", "run", "verify", "package"])
    p.add_argument("--home", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--host", choices=["claude", "codex"])
    p.add_argument("--max-tables", type=int, default=5)
    args = p.parse_args()
    if args.command == "prepare":
        prepare(args.home.resolve(), args.output.resolve())
        result = verify(args.output)
    elif args.command == "run":
        if not 1 <= args.max_tables <= 5 or not args.host:
            p.error("host and 1..5 max-tables required")
        result = run_host(args.home.resolve(), args.host, max_tables=args.max_tables)
    else:
        result = verify(args.output)
        if args.command == "package":
            target = args.output.with_suffix(".zip")
            with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(args.output.rglob("*")):
                    if path.is_file():
                        archive.write(path, Path(args.output.name) / path.relative_to(args.output))
            result["archive"] = str(target.resolve())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
