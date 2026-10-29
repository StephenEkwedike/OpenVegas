"""Opt-in opportunity decisions. No prompts, private reasoning or inferred task outcomes."""

import json
import re
import uuid
from copy import deepcopy

from .autonomy import Autonomy
from .dataset import canonical
from .engine import integer
from .service import digest


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", value):
        raise ValueError("use_identifier_not_task_text")
    return value


class Checkpoints:
    def __init__(self, arena):
        self.arena = arena
        self.auto = Autonomy(arena)
        with arena.tx() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS arena_checkpoints (id TEXT PRIMARY KEY, grant_id TEXT, document TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS arena_checkpoint_events (checkpoint_id TEXT, phase TEXT, document TEXT NOT NULL, PRIMARY KEY(checkpoint_id, phase))"
            )

    def games(self, token):
        view = self.auto.observe(token)
        g = view["grant"]
        return {
            "opportunities": [
                {
                    "game_id": "holdem",
                    "engine_version": "arena.holdem.v1",
                    "pool": "agent",
                    "seats": g["seats"],
                    "commitment": g["chips"],
                    "unit": "test_chips",
                    "fee": 0,
                    "payout": "shared pot; no subsidized prize",
                    "maximum_loss": g["chips"],
                    "expected_return": None,
                    "probability": None,
                    "opponents": "not known until matched",
                    "turn_seconds": self.arena.turn_seconds,
                    "matchmaking_wait_seconds": 300,
                }
            ],
            "context": view,
            "no_play_is_valid": True,
            "inference_budget": None,
            "inference_billing": "host_managed_unchanged",
        }

    def create(self, token, objective_id="unspecified", task_budget=None):
        identifier(objective_id)
        if task_budget is not None:
            integer(task_budget, 0, 1_000_000)
        context = self.games(token)
        with self.arena.tx() as db:
            g = self.auto._load(db, token)
            if (
                db.execute(
                    "SELECT count(*) FROM arena_checkpoints WHERE grant_id=?", (g["id"],)
                ).fetchone()[0]
                >= 1000
            ):
                raise ValueError("checkpoint_limit")
            row = {
                "id": uuid.uuid4().hex,
                "schema_version": "openvegas.checkpoint.v1",
                "collection_mode": "arena_opt_in",
                "recorded_at": self.arena.clock(),
                "objective_id": objective_id,
                "task_budget": task_budget,
                "task_provenance": "client_reported_not_verified",
                "context": context,
                "options": ["enter", "decline", "continue_work", "stop"],
                "configuration": g["configuration"],
                "context_hash": digest(canonical(context)),
            }
            db.execute(
                "INSERT INTO arena_checkpoints VALUES (?,?,?)", (row["id"], g["id"], canonical(row))
            )
            return row

    def _load(self, db, token, checkpoint_id, role="agent"):
        g = self.auto._load(db, token, role)
        r = db.execute(
            "SELECT document FROM arena_checkpoints WHERE id=? AND grant_id=?",
            (checkpoint_id, g["id"]),
        ).fetchone()
        if not r:
            raise PermissionError("checkpoint_not_owned")
        return g, json.loads(r[0])

    def _append(self, db, checkpoint_id, phase, event):
        row = db.execute(
            "SELECT document FROM arena_checkpoint_events WHERE checkpoint_id=? AND phase=?",
            (checkpoint_id, phase),
        ).fetchone()
        if row:
            return json.loads(row[0])
        event = {"recorded_at": self.arena.clock(), "phase": phase, **event}
        db.execute(
            "INSERT INTO arena_checkpoint_events VALUES (?,?,?)",
            (checkpoint_id, phase, canonical(event)),
        )
        return event

    def choose(self, token, checkpoint_id, choice, context_hash, game_id=None):
        if choice not in {"enter", "decline", "continue_work", "stop"}:
            raise ValueError("unknown_choice")
        if (choice == "enter" and game_id != "holdem") or (
            choice != "enter" and game_id is not None
        ):
            raise ValueError("game_choice_mismatch")
        with self.arena.tx() as db:
            _g, row = self._load(db, token, checkpoint_id)
            if context_hash != row["context_hash"]:
                raise ValueError("context_hash_mismatch")
            payload = {"choice": choice, "game_id": game_id, "context_hash": context_hash}
            event = self._append(db, checkpoint_id, "choice", {"actor": "agent", **payload})
            if any(event[k] != v for k, v in payload.items()):
                raise ValueError("choice_idempotency_conflict")
            receipt = db.execute(
                "SELECT document FROM arena_checkpoint_events WHERE checkpoint_id=? AND phase='entry_result'",
                (checkpoint_id,),
            ).fetchone()
            if receipt:
                return {
                    "decision": payload,
                    "entry": json.loads(receipt[0]),
                    "result": None,
                    "message": "Prior entry receipt; observe current state before another action.",
                }
            revision = row["context"]["context"]["grant"]["revision"]
        if choice == "enter":
            result = self.auto.find(
                token, "checkpoint-" + checkpoint_id, expected_revision=revision
            )
            with self.arena.tx() as db:
                self._load(db, token, checkpoint_id)
                event = self._append(
                    db,
                    checkpoint_id,
                    "entry_result",
                    {
                        "kind": "policy_block" if result.get("blocked") else "entry_receipt",
                        "reason": result.get("blocked"),
                        "match_id": result.get("observation", {}).get("match_id"),
                    },
                )
            return {"decision": payload, "entry": event, "result": result}
        return {
            "decision": payload,
            "entry": None,
            "message": "No game entered. No wallet or provider change.",
        }

    def outcome(self, token, checkpoint_id, status, evidence_id=None):
        if status not in {"succeeded", "failed", "abandoned", "unknown"}:
            raise ValueError("unknown_outcome")
        if evidence_id is not None:
            identifier(evidence_id)
        with self.arena.tx() as db:
            self._load(db, token, checkpoint_id)
            event = {
                "status": status,
                "evidence_id": evidence_id,
                "provenance": "client_reported_not_verified",
                "inference_cost_usd": None,
            }
            saved = self._append(db, checkpoint_id, "task_outcome", event)
            if any(saved[k] != v for k, v in event.items()):
                raise ValueError("outcome_idempotency_conflict")
            return saved

    def inspect(self, controller_token):
        with self.arena.tx() as db:
            g = self.auto._load(db, controller_token, "controller")
            rows = db.execute(
                "SELECT id, document FROM arena_checkpoints WHERE grant_id=? ORDER BY rowid",
                (g["id"],),
            ).fetchall()
            result = []
            for checkpoint_id, document in rows:
                result.append(
                    {
                        "checkpoint": json.loads(document),
                        "events": [
                            json.loads(r[0])
                            for r in db.execute(
                                "SELECT document FROM arena_checkpoint_events WHERE checkpoint_id=? ORDER BY rowid",
                                (checkpoint_id,),
                            )
                        ],
                    }
                )
            return {
                "records": result,
                "interventions": deepcopy(g["interventions"]),
                "collection_mode": "arena_opt_in",
                "external_transfer_authorized": False,
            }
