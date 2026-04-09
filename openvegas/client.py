"""HTTP client for communicating with OpenVegas backend."""

from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import time
from typing import Any, AsyncGenerator

import httpx

from openvegas.auth import (
    AuthError as CliAuthError,
    AuthRefreshMalformed,
    AuthRefreshRejected,
    AuthRefreshTimeout,
    SupabaseAuth,
)
from openvegas.config import (
    clear_persisted_refresh_token,
    clear_session_claim_cache,
    get_backend_url,
    get_bearer_token,
    get_session,
    invalidate_session_cache,
    request_touchid_unlock,
    require_touchid_unlock_for_refresh_storage,
    token_expires_soon,
)
from openvegas.telemetry import emit_metric, emit_once_process


class APIError(Exception):
    def __init__(self, status: int, detail: str, data: dict | None = None):
        self.status = status
        self.detail = detail
        self.data = data or {}
        super().__init__(f"API error {status}: {detail}")


class OpenVegasClient:
    """REST client for the OpenVegas backend."""

    def __init__(self):
        self.base_url = get_backend_url()
        self.token = get_bearer_token()
        self._session_snapshot = get_session()
        self._wallet_bootstrap_done = False
        self._refresh_lock = asyncio.Lock()
        self._refresh_inflight: asyncio.Task[str] | None = None
        self._proactive_fail_last_ts: float | None = None
        self._proactive_fail_cooldown_sec = 60.0
        self._emit_startup_auth_mode_once()

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _emit_startup_auth_mode_once(self) -> None:
        refresh_storage = str(self._session_snapshot.get("refresh_storage", "unknown") or "unknown")
        access_present = "1" if str(self._session_snapshot.get("access_token", "")).strip() else "0"
        near_expiry = "1" if token_expires_soon(self._session_snapshot, leeway_sec=300) else "0"
        emit_once_process(
            "auth_cli_bootstrap_state_total",
            {
                "refresh_storage": refresh_storage,
                "access_present": access_present,
                "near_expiry": near_expiry,
            },
        )

    def _allow_preflight_failure_metric(self) -> bool:
        now = time.monotonic()
        if (
            self._proactive_fail_last_ts is not None
            and (now - self._proactive_fail_last_ts) < self._proactive_fail_cooldown_sec
        ):
            return False
        self._proactive_fail_last_ts = now
        return True

    def _invalidate_session_cache(self, reason: str) -> None:
        self.token = None
        clear_session_claim_cache()
        invalidate_session_cache()
        if reason == "refresh_rejected":
            clear_persisted_refresh_token()
        self._session_snapshot = get_session()

    async def _refresh_once(self, trigger: str) -> str:
        refresh_storage = str(self._session_snapshot.get("refresh_storage", "") or "")
        if require_touchid_unlock_for_refresh_storage(refresh_storage):
            if not request_touchid_unlock():
                emit_metric(
                    "auth_refresh_attempt_total",
                    {
                        "surface": "cli",
                        "trigger": trigger,
                        "outcome": "failure",
                        "reason": "touchid_unlock_failed",
                    },
                )
                raise CliAuthError("touchid_unlock_required")
        try:
            token = await asyncio.to_thread(lambda: SupabaseAuth().refresh_token())
        except AuthRefreshTimeout as e:
            emit_metric(
                "auth_refresh_attempt_total",
                {"surface": "cli", "trigger": trigger, "outcome": "failure", "reason": "refresh_timeout"},
            )
            raise TimeoutError("refresh_timeout") from e
        except AuthRefreshMalformed as e:
            emit_metric(
                "auth_refresh_attempt_total",
                {"surface": "cli", "trigger": trigger, "outcome": "failure", "reason": "refresh_malformed"},
            )
            raise ValueError("refresh_malformed") from e
        except (AuthRefreshRejected, CliAuthError) as e:
            emit_metric(
                "auth_refresh_attempt_total",
                {"surface": "cli", "trigger": trigger, "outcome": "failure", "reason": "refresh_rejected"},
            )
            raise CliAuthError("refresh_rejected") from e
        except Exception as e:
            emit_metric(
                "auth_refresh_attempt_total",
                {"surface": "cli", "trigger": trigger, "outcome": "failure", "reason": "refresh_rejected"},
            )
            raise CliAuthError("refresh_rejected") from e

        self.token = str(token or "").strip() or None
        self._session_snapshot = get_session()
        emit_metric("auth_refresh_attempt_total", {"surface": "cli", "trigger": trigger, "outcome": "success"})
        return str(self.token or "")

    async def _refresh_single_flight(self, trigger: str) -> str:
        if self._refresh_inflight and not self._refresh_inflight.done():
            return await self._refresh_inflight
        async with self._refresh_lock:
            if self._refresh_inflight and not self._refresh_inflight.done():
                return await self._refresh_inflight
            self._refresh_inflight = asyncio.create_task(self._refresh_once(trigger))
        try:
            return await self._refresh_inflight
        finally:
            if self._refresh_inflight and self._refresh_inflight.done():
                self._refresh_inflight = None

    async def _do_http(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                return await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self._headers(),
                    **kwargs,
                )
        except httpx.HTTPError as e:
            raise APIError(
                503,
                f"Backend request failed: {type(e).__name__}. Check server is running and reachable at {self.base_url}.",
            ) from e

    @staticmethod
    def _parse_or_raise(resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            detail = resp.text
            data: dict | None = None
            try:
                body = resp.json()
                if isinstance(body, dict):
                    data = body
                    detail = body.get("detail") or body.get("error") or resp.text
            except Exception:
                pass
            raise APIError(resp.status_code, detail, data=data)
        body = resp.json()
        if isinstance(body, dict):
            return body
        return {"data": body}

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        if token_expires_soon(self._session_snapshot, leeway_sec=300):
            try:
                await self._refresh_single_flight(trigger="proactive")
            except Exception:
                if self._allow_preflight_failure_metric():
                    emit_metric(
                        "auth_refresh_attempt_total",
                        {
                            "surface": "cli",
                            "trigger": "proactive",
                            "outcome": "failure",
                            "reason": "refresh_preflight_failed",
                        },
                    )

        resp = await self._do_http(method, path, **kwargs)
        if resp.status_code in (401, 403):
            try:
                await self._refresh_single_flight(trigger="retry_401")
            except TimeoutError:
                self._invalidate_session_cache("refresh_timeout")
                raise APIError(401, "Session refresh timed out. Run: openvegas login")
            except ValueError:
                self._invalidate_session_cache("refresh_malformed")
                raise APIError(401, "Session refresh returned invalid payload. Run: openvegas login")
            except CliAuthError as e:
                if str(e) == "touchid_unlock_required":
                    raise APIError(401, "Touch ID unlock required for saved session. Run: openvegas login")
                self._invalidate_session_cache("refresh_rejected")
                raise APIError(401, "Session expired. Run: openvegas login")
            resp = await self._do_http(method, path, **kwargs)
        return self._parse_or_raise(resp)

    async def _ensure_wallet_bootstrap(self) -> None:
        if self._wallet_bootstrap_done:
            return
        await self._request("POST", "/wallet/bootstrap")
        self._wallet_bootstrap_done = True

    async def get_balance(self) -> dict:
        await self._ensure_wallet_bootstrap()
        return await self._request("GET", "/wallet/balance")

    async def get_history(self) -> dict:
        await self._ensure_wallet_bootstrap()
        return await self._request("GET", "/wallet/history")

    async def get_billing_activity(self, limit: int = 50) -> dict:
        await self._ensure_wallet_bootstrap()
        return await self._request("GET", f"/billing/activity?limit={int(limit)}")

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

    async def create_horse_quote(
        self,
        *,
        bet_type: str,
        budget_v: Decimal | str,
        idempotency_key: str,
    ) -> dict:
        return await self._request(
            "POST",
            "/games/horse/quotes",
            json={
                "bet_type": bet_type,
                "budget_v": str(Decimal(str(budget_v))),
                "idempotency_key": idempotency_key,
            },
        )

    async def play_horse_quote(
        self,
        *,
        quote_id: str,
        horse: int,
        idempotency_key: str,
        demo_mode: bool = False,
    ) -> dict:
        payload = {
            "quote_id": quote_id,
            "horse": int(horse),
            "idempotency_key": idempotency_key,
        }
        path = "/games/horse/play-demo" if demo_mode else "/games/horse/play"
        return await self._request("POST", path, json=payload)

    async def ask(
        self,
        prompt: str,
        provider: str,
        model: str,
        *,
        idempotency_key: str | None = None,
        thread_id: str | None = None,
        conversation_mode: str | None = None,
        persist_context: bool | None = None,
        enable_tools: bool | None = None,
        enable_web_search: bool | None = None,
        attachments: list[str] | None = None,
    ) -> dict:
        payload = {"prompt": prompt, "provider": provider, "model": model}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        if thread_id:
            payload["thread_id"] = thread_id
        if conversation_mode:
            payload["conversation_mode"] = conversation_mode
        if persist_context is not None:
            payload["persist_context"] = bool(persist_context)
        if enable_tools is not None:
            payload["enable_tools"] = bool(enable_tools)
        if enable_web_search is not None:
            payload["enable_web_search"] = bool(enable_web_search)
        if attachments is not None:
            payload["attachments"] = [str(a or "").strip() for a in attachments if str(a or "").strip()]
        return await self._request("POST", "/inference/ask", json=payload)

    async def ask_stream(
        self,
        prompt: str,
        provider: str,
        model: str,
        *,
        idempotency_key: str | None = None,
        thread_id: str | None = None,
        conversation_mode: str | None = None,
        persist_context: bool | None = None,
        enable_tools: bool | None = None,
        enable_web_search: bool | None = None,
        attachments: list[str] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        payload = {"prompt": prompt, "provider": provider, "model": model}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        if thread_id:
            payload["thread_id"] = thread_id
        if conversation_mode:
            payload["conversation_mode"] = conversation_mode
        if persist_context is not None:
            payload["persist_context"] = bool(persist_context)
        if enable_tools is not None:
            payload["enable_tools"] = bool(enable_tools)
        if enable_web_search is not None:
            payload["enable_web_search"] = bool(enable_web_search)
        if attachments is not None:
            payload["attachments"] = [str(a or "").strip() for a in attachments if str(a or "").strip()]

        attempted_refresh = False
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "POST",
                        f"{self.base_url}/inference/stream",
                        headers=self._headers(),
                        json=payload,
                    ) as resp:
                        if resp.status_code in (401, 403) and not attempted_refresh:
                            attempted_refresh = True
                            try:
                                await self._refresh_single_flight(trigger="retry_401_stream")
                            except TimeoutError:
                                self._invalidate_session_cache("refresh_timeout")
                                raise APIError(401, "Session refresh timed out. Run: openvegas login")
                            except ValueError:
                                self._invalidate_session_cache("refresh_malformed")
                                raise APIError(401, "Session refresh returned invalid payload. Run: openvegas login")
                            except CliAuthError:
                                self._invalidate_session_cache("refresh_rejected")
                                raise APIError(401, "Session expired. Run: openvegas login")
                            continue

                        if resp.status_code >= 400:
                            detail = await resp.aread()
                            text = detail.decode("utf-8", errors="ignore")
                            data: dict | None = None
                            try:
                                parsed = json.loads(text)
                                if isinstance(parsed, dict):
                                    data = parsed
                                    text = str(parsed.get("detail") or parsed.get("error") or text)
                            except Exception:
                                pass
                            raise APIError(resp.status_code, text, data=data)

                        current_event = "message"
                        async for line in resp.aiter_lines():
                            raw = (line or "").strip()
                            if not raw:
                                continue
                            if raw.startswith("event:"):
                                current_event = raw[6:].strip() or "message"
                                continue
                            if not raw.startswith("data:"):
                                continue
                            payload_line = raw[5:].strip()
                            if not payload_line:
                                continue
                            try:
                                data = json.loads(payload_line)
                            except Exception:
                                continue
                            if isinstance(data, dict):
                                yield {"event": current_event, "data": data}
                        return
            except httpx.HTTPError as e:
                raise APIError(
                    503,
                    f"Backend stream request failed: {type(e).__name__}. "
                    f"Check server is running and reachable at {self.base_url}.",
                ) from e

    async def upload_init(
        self,
        *,
        filename: str,
        size_bytes: int,
        mime_type: str,
        sha256_hex: str,
        timeout_sec: float | None = None,
    ) -> dict:
        req: dict[str, Any] = {
            "json": {
                "filename": str(filename or ""),
                "size_bytes": int(size_bytes),
                "mime_type": str(mime_type or ""),
                "sha256": str(sha256_hex or "").lower(),
            }
        }
        if timeout_sec is not None:
            req["timeout"] = float(timeout_sec)
        return await self._request("POST", "/files/upload/init", **req)

    async def upload_complete(
        self,
        *,
        upload_id: str,
        content_base64: str,
        timeout_sec: float | None = None,
    ) -> dict:
        req: dict[str, Any] = {
            "json": {
                "upload_id": str(upload_id or ""),
                "content_base64": str(content_base64 or ""),
            }
        }
        if timeout_sec is not None:
            req["timeout"] = float(timeout_sec)
        return await self._request("POST", "/files/upload/complete", **req)

    async def search_files(self, *, query: str, limit: int = 5) -> dict:
        return await self._request(
            "POST",
            "/files/search",
            json={
                "query": str(query or ""),
                "limit": int(limit),
            },
        )

    async def mcp_list_servers(self) -> dict:
        return await self._request("GET", "/mcp/servers")

    async def mcp_register_server(
        self,
        *,
        name: str,
        transport: str,
        target: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        return await self._request(
            "POST",
            "/mcp/servers/register",
            json={
                "name": str(name or ""),
                "transport": str(transport or ""),
                "target": str(target or ""),
                "metadata": dict(metadata or {}),
            },
        )

    async def mcp_server_health(self, *, server_id: str) -> dict:
        return await self._request("GET", f"/mcp/servers/{server_id}/health")

    async def mcp_list_tools(self, *, server_id: str, timeout_sec: int = 20) -> dict:
        return await self._request(
            "GET",
            f"/mcp/servers/{server_id}/tools",
            params={"timeout_sec": int(timeout_sec)},
        )

    async def mcp_call_tool(
        self,
        *,
        server_id: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        timeout_sec: int = 20,
    ) -> dict:
        return await self._request(
            "POST",
            f"/mcp/servers/{server_id}/tools/call",
            json={
                "tool": str(tool or ""),
                "arguments": dict(arguments or {}),
                "timeout_sec": int(timeout_sec),
            },
        )

    async def code_exec_create(self, *, language: str, code: str, timeout_sec: int = 10) -> dict:
        return await self._request(
            "POST",
            "/code-exec/jobs",
            json={
                "language": str(language or "python"),
                "code": str(code or ""),
                "timeout_sec": int(timeout_sec),
            },
        )

    async def code_exec_status(self, *, job_id: str) -> dict:
        return await self._request("GET", f"/code-exec/jobs/{job_id}")

    async def code_exec_result(self, *, job_id: str) -> dict:
        return await self._request("GET", f"/code-exec/jobs/{job_id}/result")

    async def image_generate(
        self,
        *,
        prompt: str,
        provider: str = "openai",
        model: str = "gpt-image-1",
        size: str = "1024x1024",
    ) -> dict:
        return await self._request(
            "POST",
            "/images/generate",
            json={
                "prompt": str(prompt or ""),
                "provider": str(provider or "openai"),
                "model": str(model or "gpt-image-1"),
                "size": str(size or "1024x1024"),
            },
        )

    async def create_realtime_session(
        self,
        *,
        provider: str = "openai",
        model: str = "gpt-4o-realtime-preview",
        voice: str = "alloy",
    ) -> dict:
        return await self._request(
            "POST",
            "/realtime/session",
            json={
                "provider": str(provider or "openai"),
                "model": str(model or "gpt-4o-realtime-preview"),
                "voice": str(voice or "alloy"),
            },
        )

    async def cancel_realtime_relay(self, *, relay_session_id: str, reason: str = "user_cancel") -> dict:
        return await self._request(
            "POST",
            f"/realtime/relay/{relay_session_id}/cancel",
            json={"reason": str(reason or "user_cancel")},
        )

    async def speech_transcribe(
        self,
        *,
        file_id: str,
        provider: str = "openai",
        model: str = "gpt-4o-mini-transcribe",
        language: str | None = None,
        prompt: str | None = None,
        timeout_sec: float | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "file_id": str(file_id or ""),
            "provider": str(provider or "openai"),
            "model": str(model or "gpt-4o-mini-transcribe"),
        }
        if language:
            payload["language"] = str(language)
        if prompt:
            payload["prompt"] = str(prompt)
        req: dict[str, Any] = {"json": payload}
        if timeout_sec is not None:
            req["timeout"] = float(timeout_sec)
        return await self._request("POST", "/speech/transcribe", **req)

    async def get_ops_diagnostics(self) -> dict:
        return await self._request("GET", "/ops/diagnostics")

    async def get_ops_alerts(self) -> dict:
        return await self._request("GET", "/ops/alerts")

    async def get_ops_runs(self, *, limit: int = 25) -> dict:
        return await self._request("GET", "/ops/runs", params={"limit": max(1, int(limit))})

    async def get_ops_run_detail(self, *, run_id: str) -> dict:
        return await self._request("GET", f"/ops/runs/{str(run_id or '').strip()}")

    async def get_ops_trends(self, *, limit: int = 120) -> dict:
        return await self._request("GET", "/ops/trends", params={"limit": max(1, int(limit))})

    async def get_ops_alert_state(self) -> dict:
        return await self._request("GET", "/ops/alerts/state")

    async def get_ops_alert_audit(self, *, limit: int = 100) -> dict:
        return await self._request("GET", "/ops/alerts/audit", params={"limit": max(1, min(500, int(limit)))})

    async def ack_ops_alert(self, *, metric: str) -> dict:
        return await self._request("POST", "/ops/alerts/ack", json={"metric": str(metric or "")})

    async def silence_ops_alert(self, *, metric: str, duration_sec: int = 900, reason: str = "") -> dict:
        return await self._request(
            "POST",
            "/ops/alerts/silence",
            json={
                "metric": str(metric or ""),
                "duration_sec": int(duration_sec),
                "reason": str(reason or ""),
            },
        )

    async def get_mode(self) -> dict:
        return await self._request("GET", "/inference/mode")

    async def set_mode(
        self,
        *,
        llm_mode: str | None = None,
        conversation_mode: str | None = None,
    ) -> dict:
        payload: dict = {}
        if llm_mode is not None:
            payload["llm_mode"] = llm_mode
        if conversation_mode is not None:
            payload["conversation_mode"] = conversation_mode
        return await self._request("POST", "/inference/mode", json=payload)

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

    async def get_saved_topup_payment_method(self) -> dict:
        return await self._request("GET", "/billing/topups/saved-payment-method")

    async def charge_saved_topup(
        self,
        amount_usd: Decimal | str,
        idempotency_key: str | None = None,
    ) -> dict:
        payload: dict = {"amount_usd": str(amount_usd)}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return await self._request("POST", "/billing/topups/charge-saved", json=payload)

    async def create_topup_payment_method_portal_session(self) -> dict:
        return await self._request("POST", "/billing/topups/payment-method-portal-session")

    async def preview_topup_checkout(self, amount_usd: Decimal | str) -> dict:
        return await self._request(
            "POST",
            "/billing/topups/preview",
            json={"amount_usd": str(amount_usd)},
        )

    async def suggest_topup(self, suggested_topup_usd: Decimal | str | None = None) -> dict:
        payload: dict[str, str] = {}
        if suggested_topup_usd is not None:
            payload["suggested_topup_usd"] = str(suggested_topup_usd)
        return await self._request("POST", "/billing/topups/suggest", json=payload)

    async def get_topup_status(self, topup_id: str) -> dict:
        return await self._request("GET", f"/billing/topups/{topup_id}")

    async def complete_fake_topup(self, topup_id: str) -> dict:
        return await self._request(
            "POST",
            "/billing/webhook/fake/complete",
            json={"topup_id": str(topup_id)},
        )

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
        preferred_action: str | None = None,
        preferred_payload: dict | None = None,
    ) -> dict:
        body = {
            "casino_session_id": casino_session_id,
            "game_code": game_code,
            "wager_v": float(Decimal(str(wager_v))),
            "idempotency_key": idempotency_key,
        }
        if preferred_action:
            body["preferred_action"] = str(preferred_action)
        if preferred_payload:
            body["preferred_payload"] = dict(preferred_payload)
        return await self._request(
            "POST",
            "/casino/human/rounds/demo-autoplay",
            json=body,
        )

    async def agent_start_session(self, *, envelope_v: Decimal | str) -> dict:
        return await self._request(
            "POST",
            "/v1/agent/sessions/start",
            json={"envelope_v": str(Decimal(str(envelope_v)))},
        )

    async def agent_get_budget(self, *, session_id: str) -> dict:
        return await self._request("GET", "/v1/agent/budget", params={"session_id": session_id})

    async def agent_infer(
        self,
        *,
        session_id: str,
        prompt: str,
        provider: str,
        model: str,
        max_tokens: int = 1024,
    ) -> dict:
        return await self._request(
            "POST",
            "/v1/agent/infer",
            json={
                "session_id": session_id,
                "prompt": prompt,
                "provider": provider,
                "model": model,
                "max_tokens": int(max_tokens),
            },
        )

    async def agent_boost_challenge(self, *, session_id: str) -> dict:
        return await self._request(
            "POST",
            "/v1/agent/boost/challenge",
            json={"session_id": session_id},
        )

    async def agent_boost_submit(self, *, challenge_id: str, artifact_text: str) -> dict:
        return await self._request(
            "POST",
            "/v1/agent/boost/submit",
            json={"challenge_id": challenge_id, "artifact_text": artifact_text},
        )

    async def agent_casino_start_session(
        self,
        *,
        agent_session_id: str,
        max_loss_v: Decimal | str,
    ) -> dict:
        return await self._request(
            "POST",
            "/v1/agent/casino/sessions/start",
            json={
                "agent_session_id": agent_session_id,
                "max_loss_v": float(Decimal(str(max_loss_v))),
            },
        )

    async def agent_casino_list_games(self) -> dict:
        return await self._request("GET", "/v1/agent/casino/games")

    async def agent_casino_start_round(
        self,
        *,
        casino_session_id: str,
        game_code: str,
        wager_v: Decimal | str,
    ) -> dict:
        return await self._request(
            "POST",
            "/v1/agent/casino/rounds/start",
            json={
                "casino_session_id": casino_session_id,
                "game_code": game_code,
                "wager_v": float(Decimal(str(wager_v))),
            },
        )

    async def agent_casino_action(
        self,
        *,
        round_id: str,
        action: str,
        payload: dict | None = None,
        idempotency_key: str,
    ) -> dict:
        return await self._request(
            "POST",
            f"/v1/agent/casino/rounds/{round_id}/action",
            json={
                "action": action,
                "payload": payload or {},
                "idempotency_key": idempotency_key,
            },
        )

    async def agent_casino_resolve(self, *, round_id: str) -> dict:
        return await self._request("POST", f"/v1/agent/casino/rounds/{round_id}/resolve", json={})

    async def agent_casino_verify(self, *, round_id: str) -> dict:
        return await self._request("GET", f"/v1/agent/casino/rounds/{round_id}/verify")

    async def agent_casino_get_session(self, *, session_id: str) -> dict:
        return await self._request("GET", f"/v1/agent/casino/sessions/{session_id}")

    async def agent_admin_create_account(self, *, org_id: str, name: str) -> dict:
        return await self._request(
            "POST",
            f"/v1/agent/admin/orgs/{org_id}/accounts",
            json={"name": name},
        )

    async def agent_admin_list_accounts(self, *, org_id: str) -> dict:
        return await self._request("GET", f"/v1/agent/admin/orgs/{org_id}/accounts")

    async def agent_admin_issue_token(
        self,
        *,
        org_id: str,
        agent_account_id: str,
        scopes: list[str],
        ttl_minutes: int = 60,
    ) -> dict:
        return await self._request(
            "POST",
            f"/v1/agent/admin/orgs/{org_id}/accounts/{agent_account_id}/tokens",
            json={"scopes": scopes, "ttl_minutes": int(ttl_minutes)},
        )

    async def agent_admin_get_policy(self, *, org_id: str) -> dict:
        return await self._request("GET", f"/v1/agent/admin/orgs/{org_id}/policies")

    async def agent_admin_set_policy(self, *, org_id: str, fields: dict[str, Any]) -> dict:
        return await self._request("PATCH", f"/v1/agent/admin/orgs/{org_id}/policies", json=fields)

    async def agent_admin_get_audit(self, *, org_id: str, limit: int = 50) -> dict:
        return await self._request(
            "GET",
            f"/v1/agent/admin/orgs/{org_id}/audit",
            params={"limit": int(limit)},
        )

    async def agent_run_create(
        self,
        *,
        state: str = "running",
        is_resumable: bool = False,
        expires_in_seconds: int | None = None,
    ) -> dict:
        payload: dict = {"state": state, "is_resumable": bool(is_resumable)}
        if expires_in_seconds is not None:
            payload["expires_in_seconds"] = int(expires_in_seconds)
        return await self._request("POST", "/agent/runs", json=payload)

    async def agent_run_get(self, run_id: str) -> dict:
        return await self._request("GET", f"/agent/runs/{run_id}")

    async def agent_run_transition(
        self,
        *,
        run_id: str,
        action: str,
        expected_run_version: int,
        expected_valid_actions_signature: str,
        idempotency_key: str,
        payload: dict | None = None,
    ) -> dict:
        return await self._request(
            "POST",
            f"/agent/runs/{run_id}/transition",
            json={
                "action": action,
                "payload": payload or {},
                "expected_run_version": int(expected_run_version),
                "expected_valid_actions_signature": expected_valid_actions_signature,
                "idempotency_key": idempotency_key,
            },
        )

    async def agent_run_cancel(
        self,
        *,
        run_id: str,
        expected_run_version: int,
        expected_valid_actions_signature: str,
        idempotency_key: str,
    ) -> dict:
        return await self._request(
            "POST",
            f"/agent/runs/{run_id}/cancel",
            json={
                "expected_run_version": int(expected_run_version),
                "expected_valid_actions_signature": expected_valid_actions_signature,
                "idempotency_key": idempotency_key,
            },
        )

    async def agent_run_handoff_check(self, *, run_id: str) -> dict:
        return await self._request("POST", f"/agent/runs/{run_id}/ui/handoff-check", json={})

    async def agent_register_workspace(
        self,
        *,
        run_id: str,
        runtime_session_id: str,
        workspace_root: str,
        workspace_fingerprint: str,
        git_root: str | None = None,
    ) -> dict:
        payload: dict = {
            "runtime_session_id": runtime_session_id,
            "workspace_root": workspace_root,
            "workspace_fingerprint": workspace_fingerprint,
        }
        if git_root:
            payload["git_root"] = git_root
        return await self._request(
            "POST",
            f"/agent/runs/{run_id}/session/register-workspace",
            json=payload,
        )

    async def agent_tool_propose(
        self,
        *,
        run_id: str,
        runtime_session_id: str,
        expected_run_version: int,
        expected_valid_actions_signature: str,
        idempotency_key: str,
        tool_name: str,
        arguments: dict,
        shell_mode: str | None = None,
        timeout_sec: int | None = None,
        plan_mode: bool = False,
    ) -> dict:
        payload: dict = {
            "runtime_session_id": runtime_session_id,
            "expected_run_version": int(expected_run_version),
            "expected_valid_actions_signature": expected_valid_actions_signature,
            "idempotency_key": idempotency_key,
            "tool_name": tool_name,
            "arguments": arguments,
            "plan_mode": bool(plan_mode),
        }
        if shell_mode is not None:
            payload["shell_mode"] = shell_mode
        if timeout_sec is not None:
            payload["timeout_sec"] = int(timeout_sec)
        return await self._request("POST", f"/agent/runs/{run_id}/tools/propose", json=payload)

    async def agent_tool_start(
        self,
        *,
        run_id: str,
        runtime_session_id: str,
        tool_call_id: str,
        execution_token: str,
        expected_run_version: int,
        expected_valid_actions_signature: str,
        idempotency_key: str,
    ) -> dict:
        return await self._request(
            "POST",
            f"/agent/runs/{run_id}/tools/start",
            json={
                "runtime_session_id": runtime_session_id,
                "tool_call_id": tool_call_id,
                "execution_token": execution_token,
                "expected_run_version": int(expected_run_version),
                "expected_valid_actions_signature": expected_valid_actions_signature,
                "idempotency_key": idempotency_key,
            },
        )

    async def agent_tool_heartbeat(
        self,
        *,
        run_id: str,
        runtime_session_id: str,
        tool_call_id: str,
        execution_token: str,
    ) -> dict:
        return await self._request(
            "POST",
            f"/agent/runs/{run_id}/tools/heartbeat",
            json={
                "runtime_session_id": runtime_session_id,
                "tool_call_id": tool_call_id,
                "execution_token": execution_token,
            },
        )

    async def agent_tool_result(
        self,
        *,
        run_id: str,
        runtime_session_id: str,
        tool_call_id: str,
        execution_token: str,
        result_status: str,
        result_payload: dict,
        stdout: str = "",
        stderr: str = "",
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
        stdout_sha256: str | None = None,
        stderr_sha256: str | None = None,
        result_submission_hash: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "runtime_session_id": runtime_session_id,
            "tool_call_id": tool_call_id,
            "execution_token": execution_token,
            "result_status": result_status,
            "result_payload": result_payload,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": bool(stdout_truncated),
            "stderr_truncated": bool(stderr_truncated),
        }
        if stdout_sha256 is not None:
            payload["stdout_sha256"] = stdout_sha256
        if stderr_sha256 is not None:
            payload["stderr_sha256"] = stderr_sha256
        if result_submission_hash is not None:
            payload["result_submission_hash"] = result_submission_hash
        return await self._request(
            "POST",
            f"/agent/runs/{run_id}/tools/result",
            json=payload,
        )

    async def agent_tool_cancel(
        self,
        *,
        run_id: str,
        runtime_session_id: str,
        tool_call_id: str,
        execution_token: str,
    ) -> dict:
        return await self._request(
            "POST",
            f"/agent/runs/{run_id}/tools/{tool_call_id}/cancel",
            json={
                "runtime_session_id": runtime_session_id,
                "execution_token": execution_token,
            },
        )

    async def ide_register_bridge(
        self,
        *,
        run_id: str,
        runtime_session_id: str,
        actor_id: str,
        ide_type: str,
        workspace_root: str,
        workspace_fingerprint: str,
    ) -> dict:
        return await self._request(
            "POST",
            "/ide/register",
            json={
                "run_id": run_id,
                "runtime_session_id": runtime_session_id,
                "actor_id": actor_id,
                "ide_type": ide_type,
                "workspace_root": workspace_root,
                "workspace_fingerprint": workspace_fingerprint,
            },
        )

    async def ide_open_file(
        self,
        *,
        run_id: str,
        runtime_session_id: str,
        path: str,
        line: int | None = None,
        col: int | None = None,
    ) -> dict:
        payload: dict = {
            "run_id": run_id,
            "runtime_session_id": runtime_session_id,
            "path": path,
        }
        if line is not None:
            payload["line"] = int(line)
        if col is not None:
            payload["col"] = int(col)
        return await self._request("POST", "/ide/open-file", json=payload)

    async def ide_run_command(
        self,
        *,
        run_id: str,
        runtime_session_id: str,
        command: str,
        terminal_name: str | None = None,
    ) -> dict:
        payload: dict = {
            "run_id": run_id,
            "runtime_session_id": runtime_session_id,
            "command": command,
        }
        if terminal_name:
            payload["terminal_name"] = terminal_name
        return await self._request("POST", "/ide/run-command", json=payload)

    async def ide_show_diff(
        self,
        *,
        run_id: str,
        runtime_session_id: str,
        path: str,
        new_contents: str,
        allow_partial_accept: bool = True,
    ) -> dict:
        return await self._request(
            "POST",
            "/ide/show-diff",
            json={
                "run_id": run_id,
                "runtime_session_id": runtime_session_id,
                "path": path,
                "new_contents": new_contents,
                "allow_partial_accept": bool(allow_partial_accept),
            },
        )

    async def ide_message(
        self,
        *,
        request_id: str,
        method: str,
        params: dict[str, Any],
    ) -> dict:
        return await self._request(
            "POST",
            "/ide/message",
            json={
                "id": request_id,
                "type": "request",
                "method": method,
                "params": params,
            },
        )

    async def ide_get_context(self, *, run_id: str, runtime_session_id: str) -> dict:
        return await self._request(
            "POST",
            "/ide/context",
            json={"run_id": run_id, "runtime_session_id": runtime_session_id},
        )

    async def stream_tool_output(
        self,
        *,
        run_id: str,
        tool_call_id: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        url = f"{self.base_url}/agent/runs/{run_id}/tools/{tool_call_id}/stream"
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url, headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    detail = await resp.aread()
                    raise APIError(resp.status_code, detail.decode("utf-8", errors="ignore"))
                async for line in resp.aiter_lines():
                    raw = (line or "").strip()
                    if not raw.startswith("data:"):
                        continue
                    payload = raw[5:].strip()
                    if not payload:
                        continue
                    try:
                        data = json.loads(payload)
                    except Exception:
                        continue
                    if isinstance(data, dict):
                        yield data

    async def ide_stream_events(
        self,
        *,
        run_id: str,
        runtime_session_id: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        url = (
            f"{self.base_url}/ide/events/stream"
            f"?run_id={run_id}&runtime_session_id={runtime_session_id}"
        )
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url, headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    detail = await resp.aread()
                    raise APIError(resp.status_code, detail.decode("utf-8", errors="ignore"))
                async for line in resp.aiter_lines():
                    raw = (line or "").strip()
                    if not raw.startswith("data:"):
                        continue
                    payload = raw[5:].strip()
                    if not payload:
                        continue
                    try:
                        data = json.loads(payload)
                    except Exception:
                        continue
                    if isinstance(data, dict):
                        yield data
