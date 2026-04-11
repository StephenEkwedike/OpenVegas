"""Auth middleware — Supabase JWT for humans, custom tokens for agents."""

from __future__ import annotations

import hashlib
import os

from fastapi import Depends, HTTPException, Request
from jose import jwt, JWTError

from server.services.dependencies import request_with_http_client


def _supabase_jwt_secret() -> str:
    return os.environ.get("SUPABASE_JWT_SECRET", "").strip()


def _supabase_url() -> str:
    return os.environ.get("SUPABASE_URL", "").strip().rstrip("/")


def _supabase_anon_key() -> str:
    return os.environ.get("SUPABASE_ANON_KEY", "").strip()


async def _validate_with_supabase(token: str) -> dict:
    """Fallback validation for modern Supabase JWT signing (e.g. ES256)."""
    url = _supabase_url()
    anon = _supabase_anon_key()
    if not url or not anon:
        raise HTTPException(
            500,
            "Supabase token validation is not configured (SUPABASE_URL/SUPABASE_ANON_KEY).",
        )

    try:
        resp = await request_with_http_client(
            "GET",
            f"{url}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": anon,
            },
            timeout=10,
        )
    except Exception:
        raise HTTPException(503, "Unable to reach Supabase Auth for token validation")

    if resp.status_code != 200:
        raise HTTPException(401, "Invalid or expired token")

    payload = resp.json()
    user_id = payload.get("id")
    if not user_id:
        raise HTTPException(401, "Invalid or expired token")

    return {
        "user_id": user_id,
        "role": payload.get("role", "authenticated"),
        "account_type": "human",
    }


async def get_current_user(request: Request) -> dict:
    """Extract and verify Supabase JWT from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")

    token = auth.removeprefix("Bearer ")
    alg = ""
    try:
        header = jwt.get_unverified_header(token)
        alg = str(header.get("alg", ""))
    except JWTError:
        pass

    # Legacy HS256 projects can verify locally with shared secret.
    if alg == "HS256":
        secret = _supabase_jwt_secret()
        if secret:
            try:
                payload = jwt.decode(
                    token,
                    secret,
                    algorithms=["HS256"],
                    audience="authenticated",
                )
                return {
                    "user_id": payload["sub"],
                    "role": payload.get("role", "authenticated"),
                    "account_type": "human",
                }
            except JWTError:
                # Secret mismatch/rotation fallback.
                pass

    # Modern Supabase projects often use asymmetric signing (e.g. ES256).
    return await _validate_with_supabase(token)


async def get_current_agent(request: Request) -> dict:
    """Validate agent bearer token (ov_agent_* prefix, not Supabase JWT).
    Looks up token_hash in agent_tokens, checks expiry and revocation."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ov_agent_"):
        raise HTTPException(401, "Invalid agent token format — expected ov_agent_* prefix")

    token = auth.removeprefix("Bearer ")
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    from server.services.dependencies import get_db
    db = get_db()
    row = await db.fetchrow(
        """SELECT at.agent_account_id, at.scopes, at.expires_at,
                  aa.org_id, aa.name AS agent_name, aa.status AS agent_status
           FROM agent_tokens at
           JOIN agent_accounts aa ON at.agent_account_id = aa.id
           WHERE at.token_hash = $1
             AND at.revoked_at IS NULL
             AND at.expires_at > now()
             AND aa.status = 'active'""",
        token_hash,
    )
    if not row:
        raise HTTPException(401, "Agent token invalid, expired, or revoked")

    return {
        "agent_account_id": str(row["agent_account_id"]),
        "org_id": str(row["org_id"]),
        "agent_name": row["agent_name"],
        "scopes": list(row["scopes"]),
        "account_type": "agent",
    }


def require_scope(scope: str):
    """FastAPI Depends wrapper — checks agent token has the required scope."""
    async def _check(agent: dict = Depends(get_current_agent)):
        if scope not in agent["scopes"]:
            raise HTTPException(403, f"Missing required scope: {scope}")
        return agent
    return _check


async def reject_human_users(request: Request):
    """Dependency for casino routes — rejects Supabase human JWTs.
    Agent tokens use ov_agent_* prefix; Supabase JWTs start with ey (base64)."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ey"):
        raise HTTPException(
            403, "Casino mode is restricted to agent service accounts. Human JWTs are not allowed."
        )
