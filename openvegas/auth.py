"""Supabase Auth for CLI token issuance and refresh."""

from __future__ import annotations

import httpx
from supabase import Client, create_client

from openvegas.config import clear_session, get_backend_url, get_session, load_config, save_session
from openvegas.auth_config import public_auth_config


class AuthError(Exception):
    pass


class AuthRefreshTimeout(AuthError):
    pass


class AuthRefreshMalformed(AuthError):
    pass


class AuthRefreshRejected(AuthError):
    pass


def _session_expires_at(session: object) -> int:
    if session is None:
        return 0
    raw = getattr(session, "expires_at", None)
    if raw is not None:
        try:
            return int(raw)
        except Exception:
            return 0
    raw_in = getattr(session, "expires_in", None)
    if raw_in is not None:
        try:
            return int(raw_in)
        except Exception:
            return 0
    return 0


class SupabaseAuth:
    def __init__(self):
        config = load_config()
        url = config.get("supabase_url", "")
        key = config.get("supabase_anon_key", "")
        if not url or not key:
            try:
                response = httpx.get(get_backend_url().rstrip("/") + "/auth/config", timeout=10,
                                     follow_redirects=False)
                response.raise_for_status()
                discovered = response.json()
                safe = public_auth_config(discovered.get("supabase_url", ""),
                                          discovered.get("supabase_anon_key", ""))
                url, key = safe["supabase_url"], safe["supabase_anon_key"]
            except Exception as exc:
                raise AuthError("Cannot load OpenVegas login settings. Check backend connectivity and try again.") from exc
        self.client: Client = create_client(url, key)

    def login_with_email(self, email: str, password: str) -> dict:
        resp = self.client.auth.sign_in_with_password(
            {
                "email": email,
                "password": password,
            }
        )
        if not getattr(resp, "session", None):
            raise AuthError("Login returned no session.")
        exp = _session_expires_at(resp.session)
        save_session(resp.session.access_token, resp.session.refresh_token, access_expires_at=exp)
        return {"user_id": resp.user.id, "email": resp.user.email}

    def login_with_otp(self, email: str) -> None:
        self.client.auth.sign_in_with_otp({"email": email})

    def signup(self, email: str, password: str) -> dict:
        resp = self.client.auth.sign_up(
            {
                "email": email,
                "password": password,
            }
        )
        if resp.session:
            exp = _session_expires_at(resp.session)
            save_session(resp.session.access_token, resp.session.refresh_token, access_expires_at=exp)
        return {"user_id": resp.user.id, "email": resp.user.email}

    def refresh_token(self) -> str:
        session = get_session()
        refresh = str(session.get("refresh_token", "")).strip()
        if not refresh:
            raise AuthRefreshRejected("No refresh token. Please login again.")
        try:
            resp = self.client.auth.refresh_session(refresh)
        except Exception as e:
            if isinstance(e, (TimeoutError, httpx.TimeoutException)):
                raise AuthRefreshTimeout("Session refresh timed out.") from e
            msg = str(e).lower()
            if any(token in msg for token in ("expired", "invalid", "refresh token", "jwt")):
                raise AuthRefreshRejected("Session expired. Please login again.") from e
            raise AuthRefreshRejected(f"Session refresh failed: {e}") from e

        if not getattr(resp, "session", None):
            raise AuthRefreshMalformed("Refresh response missing session.")
        access_token = str(getattr(resp.session, "access_token", "") or "").strip()
        refresh_token = str(getattr(resp.session, "refresh_token", "") or "").strip()
        if not access_token or not refresh_token:
            raise AuthRefreshMalformed("Refresh response missing token fields.")

        exp = _session_expires_at(resp.session)
        save_session(access_token, refresh_token, access_expires_at=exp)
        return access_token

    def logout(self) -> None:
        try:
            self.client.auth.sign_out()
        except Exception:
            pass
        clear_session()
