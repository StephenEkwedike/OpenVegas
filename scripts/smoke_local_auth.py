#!/usr/bin/env python3
"""Opt-in real local Auth/wallet smoke. Creates a local-only account; no paid calls."""

import argparse
import asyncio
import html
import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from urllib.parse import urlsplit

import asyncpg
import httpx
from run_local import ROOT, local_environment

BASE = "http://127.0.0.1:8000"
MAIL = "http://127.0.0.1:54324"
ACCOUNT = ROOT / ".local" / "smoke-account.json"


def require(condition, label):
    if not condition:
        raise RuntimeError(label)


def passed(label):
    print("PASS " + label, flush=True)


def confirmation_url(client, email):
    for _ in range(30):
        response = client.get(MAIL + "/api/v1/messages")
        response.raise_for_status()
        for message in response.json().get("messages", []):
            if not any(item.get("Address") == email for item in message.get("To", [])):
                continue
            detail = client.get(MAIL + "/api/v1/message/" + message["ID"]).json()
            text = html.unescape(detail.get("HTML", "") + "\n" + detail.get("Text", ""))
            for url in re.findall(r'https?://[^\s<>"\']+', text):
                parsed = urlsplit(url)
                if parsed.hostname in {"localhost", "127.0.0.1"} and parsed.path == "/auth/v1/verify":
                    return url
        time.sleep(0.3)
    raise RuntimeError("Local confirmation email missing")


async def verify_database(env, user_id, expected):
    conn = await asyncpg.connect(env["DATABASE_URL"], timeout=5)
    try:
        balance = await conn.fetchval("SELECT balance FROM wallet_accounts WHERE account_id=$1", "user:" + user_id)
        grants = await conn.fetchval("SELECT count(*) FROM user_starter_grants WHERE user_id=$1", uuid.UUID(user_id))
        require(balance == expected and grants == 1, "Durable wallet/grant mismatch")
    finally:
        await conn.close()


def run(reuse):
    env = local_environment(ROOT / ".env.local")
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
        require(client.get(BASE + "/health/ready").status_code == 200, "API not ready")
        require(client.get(BASE + "/wallet/balance").status_code == 401, "Unauthenticated wallet accessible")
        passed("readiness and unauthenticated rejection")
        if reuse:
            account = json.loads(ACCOUNT.read_text())
        else:
            account = {"email": "restoration-" + uuid.uuid4().hex + "@local.test",
                       "password": secrets.token_urlsafe(24)}
            response = client.post(BASE + "/ui/auth/signup", json=account)
            require(response.status_code == 202 and response.json().get("pending_verification"), "Signup did not require verification")
            url = confirmation_url(client, account["email"])
            response = client.get(url)
            location = response.headers.get("location", "")
            target = urlsplit(location)
            require(response.status_code in {302, 303} and target.scheme == "http"
                    and target.hostname == "127.0.0.1" and target.port == 8000
                    and target.path == "/ui/login", "Confirmation must return to local login page")
            fd = os.open(ACCOUNT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(account, output)
            passed("signup, captured email and confirmation redirect")
        response = client.post(BASE + "/ui/auth/login", json=account)
        require(response.status_code == 200, "Browser login failed")
        body = response.json()
        require("httponly" in response.headers.get("set-cookie", "").lower(), "Refresh cookie is not HttpOnly")
        user_id = body["user_id"]
        headers = {"Authorization": "Bearer " + body["access_token"]}
        first = client.post(BASE + "/wallet/bootstrap", headers=headers)
        again = client.post(BASE + "/wallet/bootstrap", headers=headers)
        require(first.status_code == again.status_code == 200, "Wallet bootstrap failed")
        require(Decimal(first.json()["balance"]) == Decimal(100) and
                Decimal(again.json()["balance"]) == Decimal(100) and
                not again.json()["starter_grant_applied"], "Starter grant was not exactly once /100")
        asyncio.run(verify_database(env, user_id, Decimal(100)))
        require(client.get(BASE + "/wallet/history", headers=headers).status_code == 200, "History failed")
        require(client.get(BASE + "/billing/activity", headers=headers).status_code == 200, "Billing activity failed")
        passed("browser login, durable 100-credit wallet, bootstrap replay, history")
        response = client.post(BASE + "/ui/auth/refresh")
        require(response.status_code == 200 and response.json().get("access_token"), "Session refresh failed")
        passed("real Supabase session refresh")
        response = client.post(BASE + "/ui/auth/logout")
        require(response.status_code == 200 and response.json().get("upstream_revoke_succeeded"), "Session revocation failed")
        require(client.post(BASE + "/ui/auth/refresh").status_code == 401, "Logout did not clear refresh session")
        passed("logout and refresh rejection")
        command = [sys.executable, str(ROOT / "scripts/run_local.py"), "cli"]
        response = subprocess.run(command + ["login"], input=account["email"] + "\n" + account["password"] + "\n",
                                  text=True, capture_output=True, timeout=40, check=False)
        require(response.returncode == 0, "Isolated CLI login failed")
        for action in ("balance", "history", "whoami"):
            response = subprocess.run(command + [action], text=True, capture_output=True, timeout=25, check=False)
            require(response.returncode == 0, "CLI " + action + " failed")
            require(not any(word in response.stdout.lower() for word in
                            ("internal server error", "not logged in", "session expired", "request failed")),
                    "CLI " + action + " returned an error despite exit zero")
            if action == "balance":
                require("100" in response.stdout and "$V" in response.stdout, "CLI balance disagrees with DB")
            if action == "whoami":
                require(user_id in response.stdout, "CLI/browser identity mismatch")
        asyncio.run(verify_database(env, user_id, Decimal(100)))
        passed("isolated host CLI login/balance/history/identity agree with browser/DB")
    print("No Stripe/AI requests, external emails, real wagers or production changes made.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse", action="store_true", help="Verify saved local test account after restart")
    args = parser.parse_args()
    try:
        run(args.reuse)
    except Exception as exc:  # noqa: BLE001 -- Redact transport and credential-bearing errors.
        message = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        print("FAIL " + message)
        raise SystemExit(1)
