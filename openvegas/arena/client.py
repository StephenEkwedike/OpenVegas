"""Thin local-pilot API client; does not run an agent loop or change providers."""

from pathlib import Path

from .__main__ import call, local_url, private_read


class ArenaClient:
    def __init__(self, identity):
        self._identity = identity
        local_url(identity["url"])

    @classmethod
    def from_file(cls, path):
        return cls(private_read(Path(path)))

    def request(self, path, body=None):
        return call(self._identity["url"], path, self._identity["token"], body)

    def get_context(self):
        return self.request("/agent")

    def list_games(self):
        return self.request("/agent/games")

    def checkpoint(self, objective_id="unspecified", task_budget=None):
        return self.request(
            "/agent/checkpoints", {"objective_id": objective_id, "task_budget": task_budget}
        )

    def choose(self, checkpoint_id, context_hash, choice, game_id=None):
        return self.request(
            "/agent/choose",
            {
                "checkpoint_id": checkpoint_id,
                "context_hash": context_hash,
                "choice": choice,
                "game_id": game_id,
            },
        )

    def join_game(self, checkpoint_id, context_hash, game_id="holdem"):
        return self.choose(checkpoint_id, context_hash, "enter", game_id)

    def observe(self):
        return self.get_context()

    def act(self, match_id, version, revision, action, request_id, amount=None):
        return self.request(
            "/agent/act",
            {
                "match_id": match_id,
                "version": version,
                "revision": revision,
                "action": action,
                "request_id": request_id,
                "amount": amount,
            },
        )

    def leave_game(self, match_id, version, revision, request_id):
        # Only a legal fold at this player's turn; no implicit refund/forced move.
        return self.act(match_id, version, revision, "fold", request_id)

    def task_outcome(self, checkpoint_id, status, evidence_id=None):
        return self.request(
            "/agent/task-outcome",
            {"checkpoint_id": checkpoint_id, "status": status, "evidence_id": evidence_id},
        )
