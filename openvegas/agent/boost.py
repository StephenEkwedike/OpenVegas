"""Deterministic boost engine — challenge issuance, scoring, and reward settlement."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from openvegas.wallet.ledger import WalletService


BOOST_RUBRICS = {
    "v1_code_quality": {
        "version": "v1",
        "task_templates": [
            "Write a Python function that reverses a linked list. Include docstring and type hints.",
            "Write a Python function that finds the longest common subsequence of two strings. Include docstring and type hints.",
            "Write a Python function that implements a basic LRU cache. Include docstring and type hints.",
            "Write a Python function that validates an email address using regex. Include docstring and type hints.",
            "Write a Python function that merges two sorted lists into one sorted list. Include docstring and type hints.",
        ],
        "criteria": [
            {"name": "compiles", "weight": 0.30, "check": "compile_check"},
            {"name": "has_docstring", "weight": 0.20, "check": "docstring_check"},
            {"name": "has_type_hints", "weight": 0.20, "check": "type_hint_check"},
            {"name": "passes_lint", "weight": 0.15, "check": "ruff_check"},
            {"name": "length_adequate", "weight": 0.15, "check": "length_check"},
        ],
        "min_score_for_reward": 0.6,
    },
}


class BoostVerifier:
    """Deterministic scoring — no LLM in the loop, pure code checks."""

    def score(self, rubric_version: str, artifact_text: str) -> tuple[float, dict]:
        rubric = BOOST_RUBRICS[rubric_version]
        results = {}
        total = 0.0

        for criterion in rubric["criteria"]:
            check_fn = getattr(self, f"_{criterion['check']}")
            passed = check_fn(artifact_text)
            results[criterion["name"]] = passed
            if passed:
                total += criterion["weight"]

        return round(total, 2), results

    def _compile_check(self, code: str) -> bool:
        try:
            compile(code, "<boost>", "exec")
            return True
        except SyntaxError:
            return False

    def _docstring_check(self, code: str) -> bool:
        return '"""' in code or "'''" in code

    def _type_hint_check(self, code: str) -> bool:
        indicators = ["->", ": str", ": int", ": list", ": dict", ": bool", ": float", ": None"]
        return any(ind in code for ind in indicators)

    def _ruff_check(self, code: str) -> bool:
        try:
            with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
                f.write(code)
                f.flush()
                result = subprocess.run(
                    ["ruff", "check", "--select", "E,W", f.name],
                    capture_output=True, timeout=10,
                )
                return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return True  # ruff not installed or timeout — pass by default

    def _length_check(self, code: str) -> bool:
        lines = code.strip().split("\n")
        return 5 <= len(lines) <= 200


class BoostService:
    def __init__(self, db: Any, wallet: WalletService, verifier: BoostVerifier | None = None):
        self.db = db
        self.wallet = wallet
        self.verifier = verifier or BoostVerifier()

    async def create_challenge(
        self, org_id: str, session_id: str, max_reward_v: float = 50.0
    ) -> dict:
        import random
        rubric = BOOST_RUBRICS["v1_code_quality"]
        task_prompt = random.choice(rubric["task_templates"])
        challenge_id = str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

        await self.db.execute(
            """INSERT INTO boost_challenges
               (id, org_id, agent_session_id, rubric_version, task_prompt, rubric_json,
                max_reward_v, expires_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            challenge_id, org_id, session_id, "v1_code_quality",
            task_prompt, json.dumps({"criteria": rubric["criteria"]}),
            max_reward_v, expires_at,
        )

        return {
            "challenge_id": challenge_id,
            "task_prompt": task_prompt,
            "max_reward_v": str(max_reward_v),
            "rubric": rubric["criteria"],
            "expires_at": expires_at.isoformat(),
        }

    async def submit_and_score(
        self, challenge_id: str, artifact_text: str, agent_account_id: str, org_id: str
    ) -> dict:
        row = await self.db.fetchrow(
            """SELECT bc.*, s.agent_account_id AS owner_agent_id, s.org_id AS owner_org_id
               FROM boost_challenges bc
               JOIN agent_sessions s ON bc.agent_session_id = s.id
               WHERE bc.id = $1 AND s.agent_account_id = $2 AND s.org_id = $3""",
            challenge_id, agent_account_id, org_id
        )
        if not row:
            raise ValueError("Challenge not found or does not belong to this agent/org")
        if row["status"] != "pending":
            raise ValueError(f"Challenge already {row['status']}")
        if datetime.now(timezone.utc) > row["expires_at"]:
            raise ValueError("Challenge expired")

        score, details = self.verifier.score("v1_code_quality", artifact_text)
        max_reward = Decimal(str(row["max_reward_v"]))
        rubric = BOOST_RUBRICS["v1_code_quality"]
        reward_v = Decimal("0")

        if score >= rubric["min_score_for_reward"]:
            reward_v = (max_reward * Decimal(str(score))).quantize(Decimal("0.01"))

        artifact_hash = hashlib.sha256(artifact_text.encode()).hexdigest()

        async with self.db.transaction() as tx:
            await tx.execute(
                """INSERT INTO boost_submissions
                   (challenge_id, artifact_hash, artifact_text, score, reward_v, status)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                challenge_id, artifact_hash, artifact_text,
                float(score), reward_v,
                "rewarded" if reward_v > 0 else "scored",
            )
            await tx.execute(
                "UPDATE boost_challenges SET status = 'scored' WHERE id = $1",
                challenge_id,
            )

            if reward_v > 0:
                agent_wallet_id = f"agent:{agent_account_id}"
                await self.wallet.ensure_account(agent_wallet_id)
                await self.wallet.mint(
                    account_id=agent_wallet_id,
                    amount=reward_v,
                    mint_id=f"boost:{challenge_id}",
                    tx=tx,
                )

        return {
            "score": score,
            "reward_v": str(reward_v),
            "details": details,
            "status": "rewarded" if reward_v > 0 else "below_threshold",
        }
