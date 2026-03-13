"""HTTP client for communicating with OpenVegas backend."""

from __future__ import annotations

from decimal import Decimal

import httpx

from openvegas.config import get_backend_url, get_bearer_token


class APIError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"API error {status}: {detail}")


class OpenVegasClient:
    """REST client for the OpenVegas backend."""

    def __init__(self):
        self.base_url = get_backend_url()
        self.token = get_bearer_token()

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                **kwargs,
            )
            if resp.status_code >= 400:
                detail = resp.text
                try:
                    detail = resp.json().get("detail", resp.text)
                except Exception:
                    pass
                raise APIError(resp.status_code, detail)
            return resp.json()

    async def get_balance(self) -> dict:
        return await self._request("GET", "/wallet/balance")

    async def get_history(self) -> dict:
        return await self._request("GET", "/wallet/history")

    async def create_mint_challenge(
        self, amount_usd: float, provider: str, mode: str
    ) -> dict:
        return await self._request(
            "POST", "/mint/challenge",
            json={"amount_usd": amount_usd, "provider": provider, "mode": mode},
        )

    async def verify_mint(
        self, challenge_id: str, nonce: str, provider: str, model: str, api_key: str
    ) -> dict:
        return await self._request(
            "POST", "/mint/verify",
            json={
                "challenge_id": challenge_id,
                "nonce": nonce,
                "provider": provider,
                "model": model,
                "tier": "proxied",
                "api_key": api_key,
            },
        )

    async def play_game(self, game: str, bet: dict) -> dict:
        return await self._request("POST", f"/games/{game}/play", json=bet)

    async def play_game_demo(self, game: str, bet: dict) -> dict:
        return await self._request("POST", f"/games/{game}/play-demo", json=bet)

    async def ask(self, prompt: str, provider: str, model: str) -> dict:
        return await self._request(
            "POST", "/inference/ask",
            json={"prompt": prompt, "provider": provider, "model": model},
        )

    async def list_models(self, provider: str | None = None) -> dict:
        params = {}
        if provider:
            params["provider"] = provider
        return await self._request("GET", "/models", params=params)

    async def verify_game(self, game_id: str) -> dict:
        return await self._request("GET", f"/games/verify/{game_id}")

    async def verify_demo_game(self, game_id: str) -> dict:
        return await self._request("GET", f"/games/demo/verify/{game_id}")

    async def store_list(self) -> dict:
        return await self._request("GET", "/store/list")

    async def store_buy(self, item_id: str, idempotency_key: str | None = None) -> dict:
        payload: dict = {"item_id": item_id}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return await self._request("POST", "/store/buy", json=payload)

    async def store_grants(self) -> dict:
        return await self._request("GET", "/store/grants")

    async def create_topup_checkout(
        self,
        amount_usd: Decimal | str,
        idempotency_key: str | None = None,
    ) -> dict:
        payload: dict = {"amount_usd": str(amount_usd)}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return await self._request("POST", "/billing/topups/checkout", json=payload)

    async def get_topup_status(self, topup_id: str) -> dict:
        return await self._request("GET", f"/billing/topups/{topup_id}")

    async def human_casino_start_session(
        self,
        *,
        max_loss_v: Decimal | str,
        max_rounds: int,
        idempotency_key: str,
    ) -> dict:
        return await self._request(
            "POST",
            "/casino/human/sessions/start",
            json={
                "max_loss_v": float(Decimal(str(max_loss_v))),
                "max_rounds": int(max_rounds),
                "idempotency_key": idempotency_key,
            },
        )

    async def human_casino_list_games(self) -> dict:
        return await self._request("GET", "/casino/human/games")

    async def human_casino_start_round(
        self,
        *,
        casino_session_id: str,
        game_code: str,
        wager_v: Decimal | str,
        idempotency_key: str,
    ) -> dict:
        return await self._request(
            "POST",
            "/casino/human/rounds/start",
            json={
                "casino_session_id": casino_session_id,
                "game_code": game_code,
                "wager_v": float(Decimal(str(wager_v))),
                "idempotency_key": idempotency_key,
            },
        )

    async def human_casino_action(
        self,
        *,
        round_id: str,
        action: str,
        payload: dict,
        idempotency_key: str,
    ) -> dict:
        return await self._request(
            "POST",
            f"/casino/human/rounds/{round_id}/action",
            json={
                "action": action,
                "payload": payload,
                "idempotency_key": idempotency_key,
            },
        )

    async def human_casino_resolve(self, *, round_id: str, idempotency_key: str) -> dict:
        return await self._request(
            "POST",
            f"/casino/human/rounds/{round_id}/resolve",
            json={"idempotency_key": idempotency_key},
        )

    async def human_casino_verify(self, round_id: str) -> dict:
        return await self._request("GET", f"/casino/human/rounds/{round_id}/verify")

    async def human_casino_get_session(self, session_id: str) -> dict:
        return await self._request("GET", f"/casino/human/sessions/{session_id}")

    async def human_casino_demo_autoplay(
        self,
        *,
        casino_session_id: str,
        game_code: str,
        wager_v: Decimal | str,
        idempotency_key: str,
    ) -> dict:
        return await self._request(
            "POST",
            "/casino/human/rounds/demo-autoplay",
            json={
                "casino_session_id": casino_session_id,
                "game_code": game_code,
                "wager_v": float(Decimal(str(wager_v))),
                "idempotency_key": idempotency_key,
            },
        )
