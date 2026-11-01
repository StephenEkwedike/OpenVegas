"""Separate, fail-closed Supabase assurance verification for the business console."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import OrderedDict, deque
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, Request
from jose import JWTError, jwt

from server.services.dependencies import get_http_client

_jwks: dict = {}
_jwks_url = ""
_jwks_at = 0.0
_jwks_attempt_at = 0.0
_jwks_lock = asyncio.Lock()
_user_requests: OrderedDict = OrderedDict()


def auth_url() -> str:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    parsed = urlsplit(url)
    # Only operator configuration selects the destination, never JWT jku/x5u/iss.
    runtime = os.getenv("OPENVEGAS_RUNTIME_ENV", "").strip().lower()
    local_runtime = runtime in {"local", "dev", "development", "test"}
    local_host = parsed.hostname in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
    # Compose reaches the host Supabase stack through this exact Docker hostname.
    # Missing/unknown/production runtime must never opt into plaintext auth transport.
    local_http = local_runtime and local_host and parsed.scheme == "http"
    if (parsed.scheme != "https" and not local_http) or (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise HTTPException(503, "Admin authentication is not configured")
    return url


async def auth_request(method: str, path: str, *, token: str = "", data=None) -> dict:
    """Bounded upstream reads; caller supplies only fixed paths or validated UUIDs."""
    headers = {"Accept": "application/json"}
    if token:
        key = os.getenv("SUPABASE_ANON_KEY", "").strip()
        if not key:
            raise HTTPException(503, "Admin authentication is not configured")
        headers.update({"apikey": key, "Authorization": f"Bearer {token}"})
    url = auth_url() + "/auth/v1" + path
    client = get_http_client()
    owned = client is None
    if owned:
        client = httpx.AsyncClient()
    try:
        async with asyncio.timeout(8):
            async with client.stream(
                method,
                url,
                headers=headers,
                json=data,
                timeout=5,
                follow_redirects=False,
            ) as response:
                if response.status_code == 429:
                    raise HTTPException(429, "Authentication rate limit; try again later")
                if response.status_code not in {200, 201}:
                    status = 400 if response.status_code in {400, 404, 422} else 503
                    if response.status_code in {401, 403}:
                        status = 401
                    raise HTTPException(status, "Authentication request rejected")
                # Supabase's enrollment SVG can exceed 500 KB for long account names.
                # Keep ordinary auth responses small; enrollment remains bounded.
                limit = 1048576 if method == "POST" and path == "/factors" else 131072
                raw = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    raw.extend(chunk)
                    if len(raw) > limit:
                        raise ValueError("oversized auth response")
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise TypeError("invalid auth response")
                return body
    except HTTPException:
        raise
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
        raise HTTPException(503, "Authentication service unavailable") from None
    finally:
        if owned:
            await client.aclose()


async def _signing_key(header: dict):
    global _jwks, _jwks_at, _jwks_url, _jwks_attempt_at
    alg = header.get("alg")
    if alg == "HS256":
        secret = os.getenv("SUPABASE_JWT_SECRET", "").strip()
        if not secret:
            raise HTTPException(401, "Unsupported signing key")
        return secret
    if alg not in {"ES256", "RS256"}:
        raise HTTPException(401, "Unsupported signing algorithm")
    kid = header.get("kid")
    if not isinstance(kid, str) or not 1 <= len(kid) <= 200:
        raise HTTPException(401, "Invalid signing key")
    url = auth_url()
    async with _jwks_lock:
        now = time.monotonic()
        if url != _jwks_url:
            _jwks, _jwks_at, _jwks_attempt_at, _jwks_url = {}, 0, 0, url
        expired = not _jwks or now - _jwks_at >= 300
        if expired or kid not in _jwks:
            if _jwks_attempt_at == 0 or now - _jwks_attempt_at >= 30:
                _jwks_attempt_at = now
                body = await auth_request("GET", "/.well-known/jwks.json")
                keys = body.get("keys")
                if not isinstance(keys, list) or not 1 <= len(keys) <= 20:
                    raise HTTPException(503, "Signing keys unavailable")
                valid = [
                    key for key in keys if isinstance(key, dict) and isinstance(key.get("kid"), str)
                ]
                _jwks = {key["kid"]: key for key in valid}
                _jwks_at = now
            elif expired:
                raise HTTPException(503, "Signing keys temporarily unavailable")
        key = _jwks.get(kid)
        expected_type = "EC" if alg == "ES256" else "RSA"
        if (
            not key
            or key.get("kty") != expected_type
            or key.get("alg", alg) != alg
            or key.get("use", "sig") != "sig"
            or (alg == "ES256" and key.get("crv") != "P-256")
            or "verify" not in key.get("key_ops", ["verify"])
        ):
            raise HTTPException(401, "Invalid signing key")
        return key


async def verify_admin_token(token: str) -> dict:
    try:
        allowed = {
            str(UUID(item.strip()))
            for item in os.getenv("OPENVEGAS_ADMIN_USER_IDS", "").split(",")
            if item.strip()
        }
    except ValueError:
        allowed = set()
    if not allowed or len(allowed) > 100:
        raise HTTPException(403, "Admin access is disabled")
    if not isinstance(token, str) or not 1 <= len(token) <= 16384:
        raise HTTPException(401, "Invalid bearer token")
    try:
        header = jwt.get_unverified_header(token)
        key = await _signing_key(header)
        issuer = (
            os.getenv("SUPABASE_JWT_ISSUER", "").strip()
            or (os.getenv("SUPABASE_PUBLIC_URL", "").strip().rstrip("/") or auth_url()) + "/auth/v1"
        )
        claims = jwt.decode(
            token,
            key,
            algorithms=[header["alg"]],
            audience="authenticated",
            issuer=issuer,
            options={
                "require_exp": True,
                "require_iat": True,
                "require_sub": True,
                "require_aud": True,
                "require_iss": True,
            },
        )
        user_id = str(UUID(claims["sub"]))
        if (
            claims.get("role") != "authenticated"
            or claims.get("is_anonymous") is True
            or claims.get("aal") not in {"aal1", "aal2"}
            or float(claims["iat"]) > time.time() + 30
        ):
            raise ValueError("invalid user claims")
    except (JWTError, ValueError, TypeError, KeyError):
        raise HTTPException(401, "Invalid or expired bearer token") from None
    if user_id not in allowed:
        raise HTTPException(403, "Admin access required")
    return {"user_id": user_id, "aal": claims["aal"], "expires_at": claims["exp"]}


async def get_admin_identity(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    identity = await verify_admin_token(auth[7:])
    bucket = "mfa" if request.method == "POST" and "/mfa/" in request.url.path else "read"
    key = (identity["user_id"], bucket)
    now = time.monotonic()
    history = _user_requests.setdefault(key, deque())
    _user_requests.move_to_end(key)
    while len(_user_requests) > 200:
        _user_requests.popitem(last=False)
    while history and history[0] < now - 60:
        history.popleft()
    if len(history) >= (12 if bucket == "mfa" else 60):
        raise HTTPException(429, "Admin request limit; retry in a minute")
    history.append(now)
    return identity


async def require_admin(identity: Annotated[dict, Depends(get_admin_identity)]) -> dict:
    if identity["aal"] != "aal2":
        raise HTTPException(403, "MFA required")
    return identity
