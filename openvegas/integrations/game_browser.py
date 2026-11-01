"""Browser actions for the owned terminal. Existing secure storage, PKCE and account checks."""

import asyncio
import base64
import hashlib
import secrets
import subprocess
import sys
import webbrowser
from urllib.parse import urlencode

from .credentials import Credentials, IntegrationError


def game_credentials(backend):
    # Domain separation from the cosmetic-only device credential.
    return Credentials(backend + "\nhuman-game-login-v1")


def use_game_login(client, backend):
    token = game_credentials(backend).load()
    if token:
        client.token = token


def open_account_browser(url):
    try:
        if sys.platform == 'darwin':
            # Launch Services does not require Apple Events automation access.
            return subprocess.run(
                ['/usr/bin/open', url], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
            ).returncode == 0
        return bool(webbrowser.open(url))
    except (OSError, subprocess.TimeoutExpired, webbrowser.Error):
        return False


async def browser_action(action, ask, client, broker):
    from .native_play import choice_model, menu_read

    if action == "purchase_v":
        url = broker.backend + "/ui/balance"
        opened = await asyncio.to_thread(open_account_browser, url)
        reply = await ask(
            ("Purchase $V in your browser: " if opened else
             "Could not open your browser. Open this wallet link: ")
            + url
            + "\nReturn here after checkout. No charge or wager is made by opening this page.",
            choice_model("BrowserAction", ["continue", "quit"]),
        )
        return reply is not None and reply.choice == "continue"
    verifier = secrets.token_urlsafe(48)
    proof = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    start = await menu_read(
        broker.request("POST", "/game-login/start", data={"code_challenge": proof})
    )
    url = broker.backend + "/ui/connect?" + urlencode({"code": start["user_code"], "mode": "game"})
    opened = await asyncio.to_thread(open_account_browser, url)
    while True:
        reply = await ask(
            ("Sign in/up in your browser. Match code " if opened else
             "Could not open your browser. Open the link below and match code ")
            + start["user_code"]
            + ".\n"
            + url
            + "\nApprove human-game access there, then Continue here. Quit returns to coding.",
            choice_model("BrowserAction", ["continue", "quit"]),
        )
        if reply is None or reply.choice == "quit":
            return False
        result = await menu_read(
            broker.request(
                "POST",
                "/game-login/exchange",
                data={"device_code": start["device_code"], "verifier": verifier},
            )
        )
        if result.get("status") == "approved":
            break
    credentials = game_credentials(broker.backend)
    try:
        credentials.save(result["game_token"])
        broker.credentials.save(result["token"])
    except IntegrationError:
        reply = await ask(
            "Your OS keychain is unavailable. Save this login in a private file readable only by your user?",
            choice_model("BrowserStorage", ["save_private_file", "quit"]),
        )
        if reply is None or reply.choice != "save_private_file":
            await broker.request("POST", "/revoke", token=result["token"])
            return False
        credentials.save(result["game_token"], allow_file=True)
        broker.credentials.save(result["token"], allow_file=True)
    broker.leases.clear()
    client.token = result["game_token"]
    return True


async def signed_out_menu(ask, client, broker):
    from .native_play import choice_model, menu_read

    while True:
        answer = await ask(
            "Welcome to OpenVegas!\nSign in to play. No wager can be placed while signed out.",
            choice_model("ChooseMode", ["sign_in", "purchase_v", "quit"]),
        )
        if answer is None or answer.choice == "quit":
            return None
        if not await browser_action(answer.choice, ask, client, broker):
            return None
        try:
            return await menu_read(client._request("GET", "/integrations/account"))
        except Exception as exc:
            from openvegas.client import APIError

            if not isinstance(exc, APIError) or exc.status not in {401, 403}:
                raise
