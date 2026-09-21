"""Bounded, same-origin emote requests. Never prompt for biometrics in a watcher."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import math
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from .manifest import PackError, parse_json, safe_token

MAX_RESPONSE_BYTES = 3 * 1024 * 1024
REQUEST_TIMEOUT = 12.0


def backend_scope(value: str) -> str:
    try:
        if not isinstance(value, str) or any(ord(c) < 33 for c in value):
            raise ValueError
        parts = urlsplit(value)
        host = parts.hostname
        try:
            loopback = host == "localhost" or bool(host and ipaddress.ip_address(host).is_loopback)
        except ValueError:
            loopback = False
        if (
            not host
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
            or not (parts.scheme == "https" or (parts.scheme == "http" and loopback))
        ):
            raise ValueError
        port = parts.port
    except ValueError as exc:
        raise PackError("Emote backend must use HTTPS, or loopback HTTP for local testing") from exc
    hostname = f"[{host.lower()}]" if ":" in host else host.lower()
    suffix = f":{port}" if port and port != (443 if parts.scheme == "https" else 80) else ""
    return f"{parts.scheme}://{hostname}{suffix}"


def credentials() -> tuple[str, str, str]:
    # Read the access token only. get_session() can contact the platform keychain.
    from openvegas.config import get_backend_url, load_config

    scope = backend_scope(get_backend_url())
    session = load_config().get("session", {})
    token = session.get("access_token", "") if isinstance(session, dict) else ""
    try:
        if not isinstance(token, str) or not 0 < len(token) <= 16384:
            raise ValueError
        if len(token.split(".")) != 3:
            raise ValueError
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        account_uuid = UUID(claims["sub"])
        if not account_uuid.int:
            raise ValueError
        account = str(account_uuid)
        if (
            type(claims.get("exp")) not in {int, float}
            or not math.isfinite(claims["exp"])
            or not claims["exp"] > time.time()
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
        raise PackError("Session missing or expired. Run: openvegas login") from exc
    # Unverified JWT claims are ONLY a cache-isolation hint. The backend checks auth.
    return scope, account, token


def current_identity() -> tuple[str, str]:
    scope, account, _ = credentials()
    return scope, account


class EmoteAPI:
    def __init__(self, *, credential_reader=credentials, http=None):
        self._credentials = credential_reader
        self._http = http
        self.backend_scope, self.account_id, _ = credential_reader()
        self.backend_scope = backend_scope(self.backend_scope)

    def identity(self) -> tuple[str, str]:
        scope, account, _ = self._credentials()
        return backend_scope(scope), account

    async def _request(self, method: str, path: str, *, body=None) -> dict:
        scope, account, token = self._credentials()
        if (backend_scope(scope), account) != (self.backend_scope, self.account_id):
            raise PackError("Account or backend changed. Run: openvegas emote sync")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-store",
        }

        async def send(http):
            async with http.stream(
                method,
                self.backend_scope + path,
                json=body,
                headers=headers,
                follow_redirects=False,
                timeout=httpx.Timeout(10, connect=5),
            ) as response:
                status = response.status_code
                if status == 401:
                    raise PackError("Session missing or expired. Run: openvegas login")
                if status in {403, 410}:
                    raise PackError("Emote access unavailable: ownership expired or was revoked")
                if status == 409:
                    raise PackError("This pack is not available for activation yet")
                if status != 200:
                    raise PackError(
                        "Emote service unavailable; no access was granted. Try again later"
                    )
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise PackError("Unsupported emote response encoding")
                if (
                    response.headers.get("content-type", "").split(";")[0].lower()
                    != "application/json"
                ):
                    raise PackError("Invalid emote service response")
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        if not 0 <= int(length) <= MAX_RESPONSE_BYTES:
                            raise ValueError
                    except ValueError as exc:
                        raise PackError("Emote response exceeds supported size") from exc
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise PackError("Emote response exceeds supported size")
                    data.extend(chunk)
                if self.identity() != (self.backend_scope, self.account_id):
                    raise PackError("Account or backend changed during emote request")
                return parse_json(bytes(data), MAX_RESPONSE_BYTES)

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                if self._http is not None:
                    return await send(self._http)
                async with httpx.AsyncClient() as http:
                    return await send(http)
        except (TimeoutError, httpx.HTTPError) as exc:
            raise PackError(
                "Emote service unreachable; cached files do not grant offline access"
            ) from exc

    async def owned(self) -> dict:
        return await self._request("GET", "/store/emotes/owned")

    async def pack(self, item_id: str) -> dict:
        return await self._request("GET", f"/store/emotes/{safe_token(item_id)}/pack")

    async def equip(self, item_id: str | None, slot: str = "companion") -> dict:
        if slot not in {"companion", "completion"}:
            raise PackError("Unsupported terminal emote slot")
        return await self._request(
            "POST",
            "/store/emotes/equip",
            body={
                "item_id": safe_token(item_id) if item_id is not None else None,
                "slot": slot,
            },
        )
