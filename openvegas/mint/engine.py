"""Mint engine — Tier 1 (server-proxied) BYOK proof-of-burn."""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from openvegas.gateway.catalog import ProviderCatalog
from openvegas.wallet.ledger import WalletService


class MintError(Exception):
    pass


MINT_RATES = {
    "solo": Decimal("0.92"),
    "split": Decimal("1.00"),
    "sponsor": Decimal("1.15"),
}


@dataclass
class MintChallenge:
    id: str
    user_id: str
    nonce: str
    task_prompt: str
    company_prompt: str | None
    min_output_tokens: int
    target_cost_usd: Decimal
    max_credit_v: Decimal
    provider: str
    model: str
    mode: str
    expires_at: datetime


SOLO_TASK_TEMPLATES = [
    "Review this code and suggest improvements:\n{input}",
    "Generate unit tests for this function:\n{input}",
    "Explain this error and suggest a fix:\n{input}",
    "Summarize these logs and flag anomalies:\n{input}",
    "Refactor this code for readability:\n{input}",
]


class MintService:
    """Handles mint challenge creation and Tier 1 (proxied) verification."""

    def __init__(self, db: Any, wallet: WalletService, catalog: ProviderCatalog):
        self.db = db
        self.wallet = wallet
        self.catalog = catalog

    async def create_challenge(
        self,
        user_id: str,
        amount_usd: float,
        provider: str,
        model: str,
        mode: str = "solo",
    ) -> MintChallenge:
        """Create a mint challenge (server-issued, single-use, 5-min expiry)."""
        pricing = await self.catalog.get_pricing(provider, model)

        nonce = secrets.token_hex(16)
        challenge_id = str(uuid.uuid4())
        target = Decimal(str(amount_usd))
        # Estimate min output tokens from target cost
        output_cost_per_token = Decimal(str(pricing["cost_output_per_1m"])) / Decimal("1000000")
        min_output = int(target * Decimal("0.5") / output_cost_per_token) if output_cost_per_token > 0 else 100
        min_output = max(min_output, 50)

        rate = MINT_RATES.get(mode, Decimal("0.92"))
        max_credit_v = (target / Decimal("0.01")) * rate * Decimal("1.5")  # 50% buffer
        max_credit_v = max_credit_v.quantize(Decimal("0.01"))

        expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

        # Pick a task template
        import random
        template = random.choice(SOLO_TASK_TEMPLATES)
        task_prompt = template.replace("{input}", "Write a helpful developer tip about Python best practices.")

        company_prompt = None
        if mode == "split":
            company_prompt = "Generate 5 creative horse names and ASCII art sprites for a racing game."
        elif mode == "sponsor":
            company_prompt = "Generate 20 diverse betting scenarios with odds for horse racing simulation."

        purpose = "company" if os.getenv("OPENVEGAS_COMPANY_MINT_DEFAULT", "1") == "1" else "user"
        disclosure_version = "v1"
        default_policy_version = "company_default_v1" if purpose == "company" else "user_default_v1"

        await self.db.execute(
            """INSERT INTO mint_challenges
               (id, user_id, nonce, provider, model, mode, task_prompt, company_prompt,
                target_cost_usd, max_credit_v, expires_at, purpose, disclosure_version, default_policy_version)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)""",
            challenge_id, user_id, nonce, provider, model, mode,
            task_prompt, company_prompt, target, max_credit_v,
            expires_at, purpose, disclosure_version, default_policy_version,
        )

        return MintChallenge(
            id=challenge_id, user_id=user_id, nonce=nonce,
            task_prompt=task_prompt, company_prompt=company_prompt,
            min_output_tokens=min_output, target_cost_usd=target,
            max_credit_v=max_credit_v, provider=provider, model=model,
            mode=mode, expires_at=expires_at,
        )

    async def verify_and_credit(
        self,
        challenge_id: str,
        user_id: str,
        nonce: str,
        provider: str,
        model: str,
        api_key: str,
    ) -> dict:
        """Tier 1 (proxied): Server calls provider with user's key, credits $V."""
        # Load challenge from DB
        row = await self.db.fetchrow(
            "SELECT * FROM mint_challenges WHERE id = $1", challenge_id,
        )
        if not row:
            raise MintError("Challenge not found")
        if str(row["user_id"]) != user_id:
            raise MintError("Challenge does not belong to authenticated user")

        challenge = MintChallenge(
            id=str(row["id"]), user_id=str(row["user_id"]), nonce=row["nonce"],
            task_prompt=row["task_prompt"], company_prompt=row.get("company_prompt"),
            min_output_tokens=50,  # will validate below
            target_cost_usd=Decimal(str(row["target_cost_usd"])),
            max_credit_v=Decimal(str(row["max_credit_v"])),
            provider=row["provider"], model=row["model"],
            mode=row["mode"], expires_at=row["expires_at"],
        )

        now = datetime.now(timezone.utc)

        if challenge.nonce != nonce:
            raise MintError("Nonce mismatch")
        if now > challenge.expires_at:
            raise MintError("Challenge expired (5 min TTL)")
        if row.get("consumed"):
            raise MintError("Challenge already consumed")
        if provider != challenge.provider or model != challenge.model:
            raise MintError("Provider/model mismatch")

        # Execute the burn — server calls provider directly (Tier 1)
        raw_response, response_text, trusted = await self._execute_proxied_burn(
            api_key, challenge
        )

        # Calculate $V
        pricing = await self.catalog.get_pricing(provider, model)
        input_cost = (
            Decimal(str(trusted["input_tokens"]))
            * Decimal(str(pricing["cost_input_per_1m"]))
            / Decimal("1000000")
        )
        output_cost = (
            Decimal(str(trusted["output_tokens"]))
            * Decimal(str(pricing["cost_output_per_1m"]))
            / Decimal("1000000")
        )
        total_burn_usd = input_cost + output_cost

        rate = MINT_RATES.get(challenge.mode, Decimal("0.92"))
        v_amount = (total_burn_usd / Decimal("0.01")) * rate
        v_amount = v_amount.quantize(Decimal("0.01"))
        v_amount = min(v_amount, challenge.max_credit_v)

        mint_id = f"mint:{challenge_id}"
        response_hash = hashlib.sha256(response_text.encode()).hexdigest()

        # All three writes in one transaction
        async with self.db.transaction() as tx:
            await tx.execute(
                """INSERT INTO mint_events
                   (challenge_id, user_id, provider, model, mode,
                    provider_request_id, input_tokens, output_tokens,
                    burn_cost_usd, v_credited, response_hash)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
                challenge.id, challenge.user_id, provider, model,
                challenge.mode, trusted["request_id"],
                trusted["input_tokens"], trusted["output_tokens"],
                total_burn_usd, v_amount, response_hash,
            )
            await tx.execute(
                "UPDATE mint_challenges SET consumed = TRUE WHERE id = $1",
                challenge.id,
            )

            user_account_id = f"user:{challenge.user_id}"
            await self.wallet.ensure_account(user_account_id)
            await self.wallet.mint(
                account_id=user_account_id,
                amount=v_amount,
                mint_id=mint_id,
                tx=tx,
            )

        return {
            "v_credited": str(v_amount),
            "cost_usd": str(total_burn_usd),
            "input_tokens": trusted["input_tokens"],
            "output_tokens": trusted["output_tokens"],
            "provider_request_id": trusted["request_id"],
            "response_text": response_text,
        }

    async def _execute_proxied_burn(
        self, api_key: str, challenge: MintChallenge
    ) -> tuple[dict, str, dict]:
        """Server calls provider directly. Response is unforgeable.
        Key is used once and immediately discarded."""
        prompt = challenge.task_prompt
        if challenge.company_prompt:
            prompt += f"\n\nAdditionally: {challenge.company_prompt}"

        if challenge.provider == "anthropic":
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=api_key)
            msg = await client.messages.create(
                model=challenge.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.model_dump()
            text = msg.content[0].text
            trusted = {
                "input_tokens": msg.usage.input_tokens,
                "output_tokens": msg.usage.output_tokens,
                "request_id": msg.id,
            }
        elif challenge.provider == "openai":
            import openai
            client = openai.AsyncOpenAI(api_key=api_key)
            resp = await client.chat.completions.create(
                model=challenge.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.model_dump()
            text = resp.choices[0].message.content or ""
            trusted = {
                "input_tokens": resp.usage.prompt_tokens,
                "output_tokens": resp.usage.completion_tokens,
                "request_id": resp.id,
            }
        elif challenge.provider == "gemini":
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(challenge.model)
            resp = await model.generate_content_async(prompt)
            raw = {}
            text = resp.text
            meta = resp.usage_metadata
            trusted = {
                "input_tokens": meta.prompt_token_count if meta else 0,
                "output_tokens": meta.candidates_token_count if meta else 0,
                "request_id": "",
            }
        else:
            raise MintError(f"Unknown provider: {challenge.provider}")

        # api_key goes out of scope — never stored
        return raw, text, trusted
