"""Bounded test-chip delegation, reusing Arena transactions and its action engine.

Agent and controller capabilities are distinct. Neither can create/extend grants,
read opponents' cards, export data, or touch the $V ledger.
"""

from copy import deepcopy
import json
import re
import secrets
import uuid

from .engine import MAX_SEATS, integer, new_hand
from .service import configuration, digest, token_valid


def request_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value):
        raise ValueError("invalid_request_id")
    return value


class Autonomy:
    def __init__(self, arena):
        self.arena = arena

    def issue(
        self,
        *,
        seats=2,
        chips=200,
        max_matches=10,
        entry_budget=2000,
        lifetime_seconds=3600,
        config=None,
    ):
        integer(seats, 2, MAX_SEATS)
        integer(chips, 2, 10000)
        integer(max_matches, 1, 100)
        integer(entry_budget, chips, 1_000_000)
        integer(lifetime_seconds, 30, 86400)
        agent, controller = (secrets.token_urlsafe(32) for _ in range(2))
        grant = {
            "id": str(uuid.uuid4()),
            "seats": seats,
            "chips": chips,
            "max_matches": max_matches,
            "entry_budget": entry_budget,
            "expires_at": self.arena.clock() + lifetime_seconds,
            "configuration": configuration(config or {}),
            "matches": [],
            "spent_entry_chips": 0,
            "active_match": None,
            "paused": False,
            "stopped": False,
            "revision": 0,
            "find_requests": {},
            "control_requests": {},
            "interventions": [],
        }
        with self.arena.tx() as db:
            if db.execute("SELECT count(*) FROM arena_grants").fetchone()[0] >= 1000:
                raise ValueError("local_grant_limit_reached")
            db.execute(
                "INSERT INTO arena_grants VALUES (?,?,?,?)",
                (grant["id"], digest(agent), digest(controller), json.dumps(grant)),
            )
        return {
            "grant_id": grant["id"],
            "agent_token": agent,
            "controller_token": controller,
            "limits": self._public(grant),
        }

    def _load(self, db, token, role="agent"):
        token_valid(token)
        column = "agent_key" if role == "agent" else "controller_key"
        row = db.execute(
            f"SELECT document FROM arena_grants WHERE {column}=?", (digest(token),)
        ).fetchone()
        if not row:
            raise PermissionError("invalid_grant_capability")
        return json.loads(row[0])

    def _save(self, db, grant):
        db.execute(
            "UPDATE arena_grants SET document=? WHERE id=?", (json.dumps(grant), grant["id"])
        )

    def _public(self, g):
        return {
            k: deepcopy(g[k])
            for k in (
                "id",
                "seats",
                "chips",
                "max_matches",
                "entry_budget",
                "expires_at",
                "spent_entry_chips",
                "paused",
                "stopped",
                "revision",
            )
        } | {
            "matches_entered": len(g["matches"]),
            "pool": "agent",
            "unit": "test_chips",
            "redeemable": False,
        }

    def _blocked(self, g):
        if g["stopped"]:
            return "grant_stopped"
        if self.arena.clock() >= g["expires_at"]:
            return "grant_expired"
        if g["paused"]:
            return "grant_paused"
        return None

    def _match(self, db, g, match_id):
        if match_id not in g["matches"]:
            raise PermissionError("match_outside_grant")
        m = self.arena._load(db, match_id)
        seat = next(i for i, p in enumerate(m["players"]) if p.get("grant_id") == g["id"])
        return m, seat

    def _view(self, g, m=None, seat=None):
        result = {"grant": self._public(g), "blocked": self._blocked(g)}
        if m:
            result["observation"] = self.arena._view(m, seat)
        return result

    def observe(self, token):
        with self.arena.tx() as db:
            g = self._load(db, token)
            if not g["active_match"]:
                return self._view(g)
            m, seat = self._match(db, g, g["active_match"])
            self.arena._expire(m)
            self.arena._save(db, m)
            return self._view(g, m, seat)

    def find(self, token, request_id, *, expected_revision=None):
        request_key(request_id)
        if expected_revision is not None:
            integer(expected_revision)
        a = self.arena
        with a.tx() as db:
            g = self._load(db, token)
            if request_id in g["find_requests"]:
                m, seat = self._match(db, g, g["find_requests"][request_id])
                a._expire(m)
                a._save(db, m)
                return self._view(g, m, seat)
            if expected_revision is not None and expected_revision != g["revision"]:
                return self._view(g) | {"blocked": "stale_control_revision_refresh"}
            if self._blocked(g):
                return self._view(g)
            if len(g["find_requests"]) >= 2000:
                return self._view(g) | {"blocked": "local_find_request_limit"}
            if g["active_match"]:
                m, seat = self._match(db, g, g["active_match"])
                a._expire(m)
                a._save(db, m)
                if not m.get("cancelled") and (not m["state"] or not m["state"]["resolved"]):
                    g["find_requests"][request_id] = m["id"]
                    self._save(db, g)
                    return self._view(g, m, seat)
            if (
                len(g["matches"]) >= g["max_matches"]
                or g["spent_entry_chips"] + g["chips"] > g["entry_budget"]
            ):
                return self._view(g) | {"blocked": "entry_budget_or_match_limit"}
            selected = None
            for (document,) in db.execute("SELECT document FROM arena_matches ORDER BY rowid"):
                m = json.loads(document)
                if (
                    m.get("matchmaking")
                    and not m.get("cancelled")
                    and not m["state"]
                    and m["pool"] == "agent"
                    and m["capacity"] == g["seats"]
                    and m["chips"] == g["chips"]
                    and a.clock() - m["created_at"] < 300
                ):
                    # Do not start against an expired/paused/stopped waiting delegate.
                    peers = [
                        json.loads(
                            db.execute(
                                "SELECT document FROM arena_grants WHERE id=?", (p["grant_id"],)
                            ).fetchone()[0]
                        )
                        for p in m["players"]
                    ]
                    if not any(self._blocked(p) for p in peers):
                        selected = m
                        break
            if selected is None:
                selected, _ = a._create(db, g["seats"], "agent", g["chips"])
                selected["matchmaking"] = True
            m = selected
            seat = len(m["players"])
            m["players"].append(
                {
                    "key": digest(secrets.token_urlsafe(32)),
                    "grant_id": g["id"],
                    "source_type": "agent",
                    "configuration": g["configuration"],
                    "rights": {"purpose": None, "record_id": None},
                }
            )
            if len(m["players"]) == m["capacity"]:
                m["state"] = new_hand(m["seed"], [m["chips"]] * m["capacity"])
                a._deadline(m)
            g["matches"].append(m["id"])
            g["spent_entry_chips"] += g["chips"]
            g["active_match"] = m["id"]
            g["find_requests"][request_id] = m["id"]
            self._save(db, g)
            a._save(db, m)
            return self._view(g, m, seat)

    def act(self, token, match_id, *, revision, version, action, request_id, amount=None):
        integer(revision)
        with self.arena.tx() as db:
            g = self._load(db, token)
            m, seat = self._match(db, g, match_id)
            rejection = self._blocked(g)
            if revision != g["revision"]:
                rejection = "stale_control_revision_refresh"
            result = self.arena._act(
                m,
                seat,
                version,
                action,
                request_id,
                amount,
                rejection=rejection,
                request_scope="agent",
                authority="agent",
                control_revision=revision,
            )
            self.arena._save(db, m)
            return result | {"grant": self._public(g)}

    def control(
        self, token, *, command, request_id, revision, version=None, action=None, amount=None
    ):
        request_key(request_id)
        integer(revision)
        if command not in {"pause", "resume", "stop", "override"}:
            raise ValueError("unknown_intervention")
        if command != "override" and any(v is not None for v in (version, action, amount)):
            raise ValueError("action_only_for_override")
        if command == "override":
            integer(version)
            if action not in {"check", "call", "fold", "raise"}:
                raise ValueError("unknown_action")
            if amount is not None:
                integer(amount)
        payload = [command, revision, version, action, amount]
        a = self.arena
        with a.tx() as db:
            g = self._load(db, token, "controller")
            if request_id in g["control_requests"]:
                cached = g["control_requests"][request_id]
                if cached["payload"] != payload:
                    raise ValueError("idempotency_conflict")
                return deepcopy(cached["response"])
            if len(g["interventions"]) >= 1000:
                raise ValueError("local_intervention_limit")
            m, seat = self._match(db, g, g["active_match"]) if g["active_match"] else (None, None)
            if m:
                a._expire(m)
            active = m and not m.get("cancelled") and (not m["state"] or not m["state"]["resolved"])
            event = {
                "schema_version": "openvegas.intervention.v1",
                "id": uuid.uuid4().hex,
                "grant_id": g["id"],
                "episode_id": m["id"] if active else None,
                "actor": "human_controller",
                "seat": seat,
                "command": command,
                "recorded_at": a.clock(),
                "revision_before": g["revision"],
                "submitted_revision": revision,
                "version": version,
                "action": {"type": action, "amount_to": amount},
                "decision_index": len(m["events"]) if active else None,
                "accepted": False,
                "reason": None,
            }
            if revision != g["revision"]:
                event["reason"] = "stale_control_revision_refresh"
            elif g["stopped"] and command != "stop":
                event["reason"] = "grant_stopped"
            elif command == "override":
                if not active or not m["state"]:
                    event["reason"] = "no_active_hand"
                elif a.clock() >= g["expires_at"]:
                    event["reason"] = "grant_expired"
                else:
                    result = a._act(
                        m,
                        seat,
                        version,
                        action,
                        request_id,
                        amount,
                        request_scope="controller",
                        authority="human_controller",
                        intervention_id=event["id"],
                        control_revision=revision,
                    )
                    event["accepted"], event["reason"] = result["accepted"], result["reason"]
                    event["decision_index"] = m["events"][-1]["event_index"]
            else:
                g["paused"] = command == "pause"
                g["stopped"] = command == "stop"
                event["accepted"] = True
                if command == "stop" and m and not m["state"]:
                    m["cancelled"] = True
            g["revision"] += 1
            event["revision_after"] = g["revision"]
            g["interventions"].append(event)
            if active:
                m.setdefault("interventions", []).append(event)
            if m:
                a._save(db, m)
            response = self._view(g, m, seat) | {"intervention": event}
            g["control_requests"][request_id] = {"payload": payload, "response": response}
            self._save(db, g)
            return response

    def inspect(self, token):
        with self.arena.tx() as db:
            g = self._load(db, token, "controller")
            m, seat = self._match(db, g, g["active_match"]) if g["active_match"] else (None, None)
            if m:
                self.arena._expire(m)
                self.arena._save(db, m)
            return self._view(g, m, seat) | {"interventions": g["interventions"]}

    def consent(self, token, match_id, purpose, record_id):
        with self.arena.tx() as db:
            g = self._load(db, token, "controller")
            m, seat = self._match(db, g, match_id)
            result = self.arena._consent(m, seat, purpose, record_id)
            self.arena._save(db, m)
            return result
