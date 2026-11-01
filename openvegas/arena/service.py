"""Private local pilot persistence. SQLite transactions serialize every match action.

This deliberately never imports the $V ledger. Chips are per-hand simulation
state with no balance conversion or inference entitlement.
"""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

from .engine import new_hand, observation, step


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def token_valid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{32,128}", value):
        raise ValueError("invalid_capability")
    return value


def configuration(value):
    allowed = {
        "model",
        "model_version",
        "code_version",
        "prompt_hash",
        "tools_hash",
        "memory_hash",
        "budget_label",
    }
    if not isinstance(value, dict) or value.keys() - allowed:
        raise ValueError("configuration_fields_not_allowed")
    if any(
        not isinstance(v, str) or not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,120}", v)
        for v in value.values()
    ):
        raise ValueError("use_identifiers_not_prompts_or_secrets")
    return {"values": value, "verification": "self_reported"}


class Arena:
    def __init__(self, path, clock=time.time, turn_seconds=60):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("database_symlink_not_allowed")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.clock = clock
        if not 5 <= turn_seconds <= 300:
            raise ValueError("invalid_turn_duration")
        self.turn_seconds = turn_seconds
        with self.tx() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS arena_matches (id TEXT PRIMARY KEY, document TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS arena_grants (id TEXT PRIMARY KEY, agent_key TEXT UNIQUE, controller_key TEXT UNIQUE, document TEXT NOT NULL)"
            )

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _load(self, db, match_id):
        row = db.execute("SELECT document FROM arena_matches WHERE id=?", (match_id,)).fetchone()
        if not row:
            raise ValueError("match_not_found")
        return json.loads(row[0])

    def _save(self, db, match):
        db.execute(
            "UPDATE arena_matches SET document=? WHERE id=?", (json.dumps(match), match["id"])
        )

    def create(self, seats=2, pool="agent", chips=200):
        from .engine import MAX_SEATS, integer

        integer(seats, 2, MAX_SEATS)
        integer(chips, 2, 10000)
        if pool not in {"agent", "human"}:
            raise ValueError("separate_human_or_agent_pool_required")
        with self.tx() as db:
            match, invite = self._create(db, seats, pool, chips)
        return {
            "match_id": match["id"],
            "invite": invite,
            "pool": pool,
            "unit": "test_chips",
            "redeemable": False,
            "seed_commitment": digest(match["seed"]),
        }

    def _create(self, db, seats, pool, chips):
        match_id = str(uuid.uuid4())
        invite = secrets.token_urlsafe(32)
        match = {
            "id": match_id,
            "capacity": seats,
            "pool": pool,
            "chips": chips,
            "invite_hash": digest(invite),
            "seed": secrets.token_hex(32),
            "players": [],
            "state": None,
            "events": [],
            "requests": {},
            "created_at": self.clock(),
            "deadline": None,
            "turn_started": None,
            "is_demo": False,
        }
        if db.execute("SELECT count(*) FROM arena_matches").fetchone()[0] >= 1000:
            raise ValueError("local_match_limit_reached")
        db.execute("INSERT INTO arena_matches VALUES (?,?)", (match_id, json.dumps(match)))
        return match, invite

    def join(self, match_id, invite, seat_token, source_type, config=None):
        token_valid(seat_token)
        token_valid(invite)
        config = configuration(config or {})
        with self.tx() as db:
            m = self._load(db, match_id)
            if not secrets.compare_digest(digest(invite), m["invite_hash"]):
                raise PermissionError("invalid_invite")
            if source_type != m["pool"]:
                raise ValueError("pool_mismatch")
            existing = next(
                (i for i, p in enumerate(m["players"]) if p["key"] == digest(seat_token)), None
            )
            if existing is not None:
                if m["players"][existing]["configuration"] != config:
                    raise ValueError("join_idempotency_conflict")
                return self._view(m, existing)
            if m["state"] or len(m["players"]) >= m["capacity"]:
                raise ValueError("table_full")
            if self.clock() - m["created_at"] > 3600:
                raise ValueError("lobby_expired")
            seat = len(m["players"])
            m["players"].append(
                {
                    "key": digest(seat_token),
                    "source_type": source_type,
                    "configuration": config,
                    "rights": {"purpose": None, "record_id": None},
                }
            )
            if len(m["players"]) == m["capacity"]:
                m["state"] = new_hand(m["seed"], [m["chips"]] * m["capacity"])
                self._deadline(m)
            self._save(db, m)
            return self._view(m, seat)

    def _seat(self, m, token):
        token_valid(token)
        for i, p in enumerate(m["players"]):
            if secrets.compare_digest(p["key"], digest(token)):
                return i
        raise PermissionError("not_a_participant")

    def _view(self, m, seat):
        view = (
            observation(m["state"], seat)
            if m["state"]
            else {
                "status": "waiting",
                "seat": seat,
                "joined": len(m["players"]),
                "capacity": m["capacity"],
                "version": 0,
            }
        )
        return {
            **view,
            **({"status": "cancelled"} if m.get("cancelled") else {}),
            "match_id": m["id"],
            "pool": m["pool"],
            "deadline": m["deadline"],
            "unit": "test_chips",
            "redeemable": False,
            "export_consent": m["players"][seat]["rights"],
            "assisted": bool(m.get("interventions")),
        }

    def _deadline(self, m):
        m["turn_started"] = self.clock()
        m["deadline"] = None if m["state"]["resolved"] else self.clock() + self.turn_seconds

    def _event(
        self, m, seat, action, amount, accepted, before, after, reason=None, submitted_version=None
    ):
        m["events"].append(
            {
                "schema_version": "openvegas.decision.v1",
                "episode_id": m["id"],
                "decision_id": f"{m['id']}:{len(m['events'])}",
                "event_index": len(m["events"]),
                "recorded_at": self.clock(),
                "actor": {
                    "seat": seat,
                    "source_type": m["players"][seat]["source_type"],
                    "authority": "server_timeout"
                    if reason == "timeout"
                    else m.get("action_authority", "participant"),
                },
                "intervention_id": m.get("action_intervention"),
                "observation": before,
                "legal_actions": before.get("legal_actions", {}),
                "action": {"type": action, "amount_to": amount},
                "accepted": accepted,
                "reason": reason,
                "submitted_version": submitted_version,
                "client_observation": None
                if reason
                in {
                    "stale_version_refresh_observation",
                    "stale_control_revision_refresh",
                    "timeout",
                }
                else before,
                "resulting_observation": after,
                "episode_ended": after.get("resolved", False),
                "usage": {
                    "latency_ms": round(max(0, self.clock() - m["turn_started"]) * 1000),
                    "input_tokens": None,
                    "output_tokens": None,
                    "inference_cost_usd": None,
                },
                "provenance": {"is_demo": m["is_demo"], "collection": "local_no_stakes_server"},
            }
        )

    def _expire(self, m):
        if m.get("matchmaking") and not m["state"] and self.clock() - m["created_at"] >= 300:
            m["cancelled"] = True
        if m["state"] and not m["state"]["resolved"] and self.clock() >= m["deadline"]:
            seat = m["state"]["actor"]
            before = observation(m["state"], seat)
            m["state"] = step(m["state"], seat, "fold")
            self._event(
                m, seat, "fold", None, True, before, observation(m["state"], seat), "timeout"
            )
            self._deadline(m)

    def observe(self, match_id, token):
        with self.tx() as db:
            m = self._load(db, match_id)
            seat = self._seat(m, token)
            self._expire(m)
            self._save(db, m)
            return self._view(m, seat)

    def act(self, match_id, token, version, action, request_id, amount=None):
        with self.tx() as db:
            m = self._load(db, match_id)
            seat = self._seat(m, token)
            response = self._act(m, seat, version, action, request_id, amount)
            self._save(db, m)
            return response

    def _act(
        self,
        m,
        seat,
        version,
        action,
        request_id,
        amount=None,
        *,
        rejection=None,
        request_scope="participant",
        authority="participant",
        intervention_id=None,
        control_revision=None,
    ):
        from .engine import integer

        integer(version)
        if action not in {"fold", "check", "call", "raise"}:
            raise ValueError("unknown_action")
        if amount is not None:
            integer(amount)
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
            raise ValueError("invalid_request_id")
        key = (
            f"{seat}:{request_id}"
            if request_scope == "participant"
            else f"{request_scope}:{seat}:{request_id}"
        )
        payload = [version, action, amount]
        if request_scope != "participant":
            payload.append(control_revision)
        if key in m["requests"]:
            if m["requests"][key]["payload"] != payload:
                raise ValueError("idempotency_conflict")
            return deepcopy(m["requests"][key]["response"])
        if not m["state"]:
            raise ValueError("waiting_for_players")
        if len(m["events"]) >= 2000:
            raise ValueError("local_event_limit_reached")
        self._expire(m)
        before = observation(m["state"], seat)
        reason = None
        try:
            if rejection:
                raise ValueError(rejection)
            if version != m["state"]["version"]:
                raise ValueError("stale_version_refresh_observation")
            m["state"] = step(m["state"], seat, action, amount)
        except ValueError as e:
            reason = str(e)
        after = observation(m["state"], seat)
        m["action_authority"] = authority
        m["action_intervention"] = intervention_id
        self._event(m, seat, action, amount, reason is None, before, after, reason, version)
        m["events"][-1]["submitted_control_revision"] = control_revision
        m.pop("action_authority")
        m.pop("action_intervention")
        if reason is None:
            self._deadline(m)
        response = {
            "accepted": reason is None,
            "reason": reason,
            "observation": self._view(m, seat),
        }
        m["requests"][key] = {"payload": payload, "response": response}
        return response

    def consent(self, match_id, token, purpose, record_id):
        with self.tx() as db:
            m = self._load(db, match_id)
            seat = self._seat(m, token)
            result = self._consent(m, seat, purpose, record_id)
            self._save(db, m)
            return result

    def _consent(self, m, seat, purpose, record_id):
        if purpose not in {None, "internal_evaluation", "training", "evaluation_delivery"}:
            raise ValueError("unknown_export_purpose")
        if purpose and (
            not isinstance(record_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", record_id)
        ):
            raise ValueError("rights_record_required")
        m["players"][seat]["rights"] = {
            "purpose": purpose,
            "record_id": record_id if purpose else None,
        }
        m["players"][seat].setdefault("rights_history", []).append(
            {**m["players"][seat]["rights"], "recorded_at": self.clock()}
        )
        return {"rights": m["players"][seat]["rights"], "external_transfer": False}

    def authorized_export(self, match_id, purpose):
        if purpose not in {"internal_evaluation", "training", "evaluation_delivery"}:
            raise ValueError("unknown_export_purpose")
        with self.tx() as db:
            m = self._load(db, match_id)
            if not m["state"] or not m["state"]["resolved"]:
                raise ValueError("completed_episode_required")
            if any(p["rights"]["purpose"] != purpose for p in m["players"]):
                raise PermissionError("all_participants_must_grant_this_purpose")
            # The server-only replay seed is separate from player-facing trajectories.
            return {
                k: deepcopy(m[k])
                for k in ("id", "seed", "chips", "capacity", "state", "events", "is_demo")
            } | {
                "interventions": deepcopy(m.get("interventions", [])),
                "players": [
                    {k: deepcopy(p[k]) for k in ("source_type", "configuration", "rights")}
                    for p in m["players"]
                ],
            }
