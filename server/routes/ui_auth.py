"""UI auth routes for browser credential login and cookie-backed refresh."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from openvegas.telemetry import emit_metric, emit_once_process
from server.services.dependencies import current_flags

router = APIRouter()
logger = logging.getLogger(__name__)

RUNTIME_FLAGS = current_flags()  # frozen at process boot via @lru_cache
TRUST_PROXY_HEADERS = bool(RUNTIME_FLAGS.trusted_proxy_headers_enabled)
COOKIE_MAX_AGE_SEC = 60 * 60 * 24 * 30


class UiLoginRequest(BaseModel):
    email: str
    password: str


class UiImportSessionRequest(BaseModel):
    refresh_token: str


def _as_bool(raw: str, default: bool = False) -> bool:
    value = str(raw or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _runtime_env_name() -> str:
    return str(os.getenv("OPENVEGAS_RUNTIME_ENV", os.getenv("ENV", "local"))).strip().lower()


def _is_production_env() -> bool:
    return _runtime_env_name() in {"prod", "production"}


def _cookie_secure() -> bool:
    default_secure = _is_production_env()
    return _as_bool(os.getenv("OPENVEGAS_COOKIE_SECURE", "1" if default_secure else "0"), default_secure)


def _cookie_samesite() -> str:
    mode = str(os.getenv("OPENVEGAS_COOKIE_SAMESITE", "lax")).strip().lower()
    return "strict" if mode == "strict" else "lax"


def _refresh_cookie_name() -> str:
    # __Host- cookies require Secure + Path=/ + no Domain. Keep local dev usable.
    return "__Host-ov_refresh_token" if _cookie_secure() else "ov_refresh_token"


def _cookie_kwargs() -> dict[str, Any]:
    return {
        "path": "/",
        "secure": _cookie_secure(),
        "samesite": _cookie_samesite(),
        "httponly": True,
    }


def _set_refresh_cookie(resp: Response, refresh_token: str) -> None:
    resp.set_cookie(
        key=_refresh_cookie_name(),
        value=str(refresh_token),
        max_age=COOKIE_MAX_AGE_SEC,
        **_cookie_kwargs(),
    )


def _clear_refresh_cookie(resp: Response) -> None:
    resp.delete_cookie(key=_refresh_cookie_name(), **_cookie_kwargs())


def _clear_refresh_cookie_all_variants(resp: Response) -> None:
    base = _cookie_kwargs()
    for key in ("__Host-ov_refresh_token", "ov_refresh_token"):
        for secure_mode in (True, False):
            kw = dict(base)
            kw["secure"] = secure_mode
            resp.delete_cookie(key=key, **kw)


def _no_store(resp: Response) -> None:
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"


def _emit_cookie_mode_once() -> None:
    emit_once_process("auth_cookie_name_mode_total", {"cookie_name": _refresh_cookie_name()})


def _request_origin_base(request: Request) -> str:
    if TRUST_PROXY_HEADERS:
        f_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        f_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
        scheme = f_proto or request.url.scheme
        host = f_host or request.url.netloc
    else:
        scheme = request.url.scheme
        host = request.url.netloc
    return f"{scheme}://{host}"


def _assert_same_origin(request: Request) -> None:
    expected = _request_origin_base(request)
    origin = (request.headers.get("origin") or "").strip()
    referer = (request.headers.get("referer") or "").strip()
    default_require_origin = _is_production_env()
    require_origin = _as_bool(
        os.getenv("OPENVEGAS_REQUIRE_ORIGIN_ON_POST", "1" if default_require_origin else "0"),
        default_require_origin,
    )

    if request.method.upper() == "POST" and require_origin and not origin:
        emit_metric(
            "auth_csrf_block_total",
            {"reason": "missing_origin", "proxy_trust_enabled": "1" if TRUST_PROXY_HEADERS else "0"},
        )
        if _as_bool(os.getenv("OPENVEGAS_TOOL_DEBUG", "0")):
            logger.warning(
                "origin_reject missing_origin expected=%s trust_proxy=%s",
                expected,
                TRUST_PROXY_HEADERS,
            )
        raise HTTPException(status_code=403, detail="Missing origin")

    if origin and origin != expected:
        emit_metric(
            "auth_csrf_block_total",
            {"reason": "origin_mismatch", "proxy_trust_enabled": "1" if TRUST_PROXY_HEADERS else "0"},
        )
        if _as_bool(os.getenv("OPENVEGAS_TOOL_DEBUG", "0")):
            logger.warning(
                "origin_reject origin_mismatch expected=%s got_origin=%s trust_proxy=%s",
                expected,
                origin,
                TRUST_PROXY_HEADERS,
            )
        raise HTTPException(status_code=403, detail="Invalid origin")

    if not origin and referer and not referer.startswith(expected):
        emit_metric(
            "auth_csrf_block_total",
            {"reason": "referer_mismatch", "proxy_trust_enabled": "1" if TRUST_PROXY_HEADERS else "0"},
        )
        if _as_bool(os.getenv("OPENVEGAS_TOOL_DEBUG", "0")):
            logger.warning(
                "origin_reject referer_mismatch expected=%s got_referer=%s trust_proxy=%s",
                expected,
                referer,
                TRUST_PROXY_HEADERS,
            )
        raise HTTPException(status_code=403, detail="Invalid referer")


def _refresh_trigger(request: Request) -> str:
    trig = str(request.headers.get("x-openvegas-refresh-trigger", "")).strip().lower()
    if trig in {"bootstrap", "proactive", "retry_401"}:
        return trig
    return "retry_401"


def _supabase_cfg() -> tuple[str, str]:
    supabase_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    supabase_anon = os.getenv("SUPABASE_ANON_KEY", "").strip()
    if not supabase_url or not supabase_anon:
        raise HTTPException(status_code=500, detail="Supabase auth is not configured")
    return supabase_url, supabase_anon


def _extract_expires_at(payload: dict[str, Any]) -> int:
    raw = payload.get("expires_at")
    if isinstance(raw, (int, float)):
        v = int(raw)
        if v > 0:
            return v
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)):
        ttl = max(1, int(expires_in))
        return int(time.time()) + ttl
    return int(time.time()) + 3600


async def _supabase_token_password(*, email: str, password: str) -> dict[str, Any]:
    supabase_url, supabase_anon = _supabase_cfg()
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            res = await client.post(
                f"{supabase_url}/auth/v1/token?grant_type=password",
                headers={"apikey": supabase_anon, "Content-Type": "application/json"},
                json={"email": email, "password": password},
            )
    except Exception as e:  # pragma: no cover - defensive network wrapper
        raise HTTPException(status_code=503, detail="Unable to reach auth provider") from e

    body = res.json() if res.content else {}
    if res.status_code in (400, 401):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if res.status_code >= 400:
        msg = body.get("msg") or body.get("error_description") or "Auth login failed"
        raise HTTPException(status_code=502, detail=str(msg))
    return body if isinstance(body, dict) else {}


def _signup_email_redirect(request: Request) -> str:
    explicit = str(os.getenv("OPENVEGAS_AUTH_EMAIL_REDIRECT_URL", "")).strip()
    if explicit:
        return explicit

    path = str(
        os.getenv(
            "OPENVEGAS_AUTH_EMAIL_REDIRECT_PATH",
            "/ui/login?mode=signup&next=%2Fui%2Fbalance",
        )
    ).strip()
    if not path:
        path = "/ui/login?mode=signup&next=%2Fui%2Fbalance"
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{_request_origin_base(request)}{path}"


async def _supabase_signup(
    *,
    email: str,
    password: str,
    email_redirect_to: str | None = None,
) -> dict[str, Any]:
    supabase_url, supabase_anon = _supabase_cfg()
    payload: dict[str, Any] = {"email": email, "password": password}
    if email_redirect_to:
        # GoTrue compatibility across versions: include both fields.
        payload["email_redirect_to"] = email_redirect_to
        payload["redirect_to"] = email_redirect_to
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            res = await client.post(
                f"{supabase_url}/auth/v1/signup",
                headers={"apikey": supabase_anon, "Content-Type": "application/json"},
                json=payload,
            )
    except Exception as e:  # pragma: no cover - defensive network wrapper
        raise HTTPException(status_code=503, detail="Unable to reach auth provider") from e

    body = res.json() if res.content else {}
    if res.status_code in (400, 401, 422):
        msg = body.get("msg") or body.get("error_description") or body.get("error") or "Signup failed"
        raise HTTPException(status_code=400, detail=str(msg))
    if res.status_code >= 400:
        msg = body.get("msg") or body.get("error_description") or "Signup failed"
        raise HTTPException(status_code=502, detail=str(msg))
    return body if isinstance(body, dict) else {}


async def _supabase_token_refresh(refresh_token: str) -> dict[str, Any]:
    supabase_url, supabase_anon = _supabase_cfg()
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            res = await client.post(
                f"{supabase_url}/auth/v1/token?grant_type=refresh_token",
                headers={"apikey": supabase_anon, "Content-Type": "application/json"},
                json={"refresh_token": str(refresh_token)},
            )
    except Exception as e:  # pragma: no cover - defensive network wrapper
        raise HTTPException(status_code=503, detail="Unable to reach auth provider") from e

    body = res.json() if res.content else {}
    if res.status_code in (400, 401):
        raise HTTPException(status_code=401, detail="Session expired")
    if res.status_code >= 400:
        msg = body.get("msg") or body.get("error_description") or "Auth refresh failed"
        raise HTTPException(status_code=502, detail=str(msg))
    return body if isinstance(body, dict) else {}


async def revoke_refresh_session(refresh_token: str) -> None:
    if not refresh_token:
        return
    body = await _supabase_token_refresh(refresh_token)
    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("refresh_revoke_missing_access_token")

    supabase_url, supabase_anon = _supabase_cfg()
    async with httpx.AsyncClient(timeout=12) as client:
        res = await client.post(
            f"{supabase_url}/auth/v1/logout",
            headers={
                "apikey": supabase_anon,
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"scope": "global"},
        )
    if res.status_code >= 400:
        raise RuntimeError(f"refresh_revoke_failed_{res.status_code}")


def _read_refresh_cookie(request: Request) -> str:
    return str(
        request.cookies.get(_refresh_cookie_name())
        or request.cookies.get("__Host-ov_refresh_token")
        or request.cookies.get("ov_refresh_token")
        or ""
    ).strip()


@router.post("/ui/auth/login")
async def ui_login(payload: UiLoginRequest, request: Request):
    _emit_cookie_mode_once()
    _assert_same_origin(request)

    email = payload.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="Invalid email")

    body = await _supabase_token_password(email=email, password=payload.password)
    access_token = str(body.get("access_token") or "").strip()
    refresh_token = str(body.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise HTTPException(status_code=502, detail="Auth provider returned incomplete token payload")

    user = body.get("user") or {}
    resp = JSONResponse(
        {
            "access_token": access_token,
            "expires_at": _extract_expires_at(body),
            "user_id": user.get("id"),
            "email": user.get("email"),
        }
    )
    _set_refresh_cookie(resp, refresh_token)
    _no_store(resp)
    return resp


@router.post("/ui/auth/signup")
async def ui_signup(payload: UiLoginRequest, request: Request):
    _emit_cookie_mode_once()
    _assert_same_origin(request)

    email = payload.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="Invalid email")
    if len(payload.password or "") < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    redirect_to = _signup_email_redirect(request)
    body = await _supabase_signup(email=email, password=payload.password, email_redirect_to=redirect_to)
    access_token = str(body.get("access_token") or "").strip()
    refresh_token = str(body.get("refresh_token") or "").strip()
    expires_at = _extract_expires_at(body) if access_token else 0
    pending_verification = not bool(access_token and refresh_token)

    resp = JSONResponse(
        {
            "access_token": access_token,
            "expires_at": expires_at,
            "pending_verification": pending_verification,
        },
        status_code=200 if not pending_verification else 202,
    )
    if refresh_token:
        _set_refresh_cookie(resp, refresh_token)
    _no_store(resp)
    return resp


@router.post("/ui/auth/session/import")
async def ui_import_session(payload: UiImportSessionRequest, request: Request):
    _emit_cookie_mode_once()
    _assert_same_origin(request)

    refresh_token = str(payload.refresh_token or "").strip()
    if not refresh_token:
        raise HTTPException(status_code=422, detail="Missing refresh token")

    body = await _supabase_token_refresh(refresh_token)
    access_token = str(body.get("access_token") or "").strip()
    rotated_refresh = str(body.get("refresh_token") or "").strip()
    if not access_token or not rotated_refresh:
        raise HTTPException(status_code=502, detail="Refresh provider payload missing required fields")

    resp = JSONResponse({"access_token": access_token, "expires_at": _extract_expires_at(body)})
    _set_refresh_cookie(resp, rotated_refresh)
    _no_store(resp)
    return resp


@router.post("/ui/auth/refresh")
async def ui_refresh(request: Request):
    _emit_cookie_mode_once()
    _assert_same_origin(request)

    trigger = _refresh_trigger(request)
    refresh_token = _read_refresh_cookie(request)
    if not refresh_token:
        emit_metric(
            "auth_refresh_attempt_total",
            {"surface": "browser", "trigger": trigger, "outcome": "failure", "reason": "refresh_rejected"},
        )
        emit_metric("auth_flow_finalize_total", {"surface": "browser", "result": "login_required", "reason": "missing_refresh_cookie"})
        raise HTTPException(status_code=401, detail="No refresh session")

    body = await _supabase_token_refresh(refresh_token)
    access_token = str(body.get("access_token") or "").strip()
    rotated_refresh = str(body.get("refresh_token") or "").strip()
    if not access_token or not rotated_refresh:
        emit_metric(
            "auth_refresh_attempt_total",
            {"surface": "browser", "trigger": trigger, "outcome": "failure", "reason": "refresh_malformed"},
        )
        raise HTTPException(status_code=502, detail="Refresh provider payload missing required fields")

    emit_metric("auth_refresh_attempt_total", {"surface": "browser", "trigger": trigger, "outcome": "success"})
    resp = JSONResponse({"access_token": access_token, "expires_at": _extract_expires_at(body)})
    _set_refresh_cookie(resp, rotated_refresh)
    _no_store(resp)
    return resp


@router.post("/ui/auth/logout")
async def ui_logout(request: Request):
    _emit_cookie_mode_once()
    _assert_same_origin(request)
    refresh_token = _read_refresh_cookie(request)

    revoke_ok = True
    revoke_error: str | None = None
    if refresh_token:
        try:
            await revoke_refresh_session(refresh_token)
        except Exception as e:  # pragma: no cover - exercised via route tests
            revoke_ok = False
            revoke_error = str(e)

    resp = JSONResponse(
        {
            "ok": True,
            "local_logout_succeeded": True,
            "upstream_revoke_succeeded": revoke_ok,
            "upstream_revoke_error": revoke_error,
        }
    )
    _clear_refresh_cookie(resp)
    _clear_refresh_cookie_all_variants(resp)
    _no_store(resp)
    return resp
