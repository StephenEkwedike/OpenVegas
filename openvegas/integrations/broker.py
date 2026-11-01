"""Device approval and short-lived host leases, without provider credentials."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from urllib.parse import urlencode

import httpx

from .credentials import Credentials, IntegrationError, backend_url


class Broker:
    def __init__(self, *, backend=None, transport=None):
        self.backend = backend or backend_url()
        self.credentials = Credentials(self.backend)
        self.http = httpx.AsyncClient(base_url=self.backend, timeout=15,
                                     follow_redirects=False, transport=transport)
        self.leases = {}

    async def close(self):
        await self.http.aclose()

    async def request(self, method, path, *, token=None, data=None):
        try:
            response = await self.http.request(method, "/integrations" + path, json=data,
                                               headers={"Authorization": "Bearer " + token} if token else {})
        except httpx.HTTPError:
            raise IntegrationError("Cannot reach the OpenVegas backend; no credentials were changed.") from None
        if response.status_code in {401, 403}:
            raise IntegrationError("Authorization expired, declined or not permitted. Run openvegas go.", response.status_code)
        if response.status_code == 503:
            raise IntegrationError("Native integrations are not enabled on this backend yet.")
        if not response.is_success:
            raise IntegrationError(f"OpenVegas integration request failed (HTTP {response.status_code}).")
        try:
            return response.json()
        except ValueError:
            raise IntegrationError("OpenVegas returned an invalid integration response.") from None

    async def account(self):
        token = self.credentials.load()
        if not token:
            raise IntegrationError("No integration login. Run openvegas go or openvegas login --browser.")
        return await self.request("GET", "/account", token=token)

    async def _existing_session_request(self, method, path):
        from openvegas.auth import AuthError
        from openvegas.client import APIError, OpenVegasClient
        client = OpenVegasClient()
        client.base_url = self.backend
        client._http_client.follow_redirects = False
        try:
            return await client._request(method, "/integrations" + path)
        except APIError as exc:
            if exc.status not in {401, 403}:
                raise IntegrationError(f"Existing-session request failed (HTTP {exc.status}).") from None
        except AuthError:
            pass
        finally:
            await client._http_client.aclose()
        return None

    async def authorize(self, *, allow_file=False, emit=print, open_browser=webbrowser.open):
        from openvegas.config import get_session
        # Public setup uses backend-bound browser credentials, never a legacy
        # CLI session inherited from a different development server.
        session = {} if os.getenv("OPENVEGAS_HOSTED_LAUNCH") == "1" else get_session() or {}
        existing_session = (session.get("access_token") or session.get("refresh_token") or
                            session.get("refresh_storage") == "platform_credential_store")
        token = self.credentials.load()
        if token:
            try:
                account = await self.account()
                if existing_session:
                    primary = await self._existing_session_request("GET", "/account")
                    if primary and primary["user_id"] != account["user_id"]:
                        raise IntegrationError("Your integration and CLI are signed in to different accounts. "
                                               "Run openvegas integration logout, then openvegas go to reconnect.")
                return account
            except IntegrationError as exc:
                # Only an actually rejected credential should start another login.
                if exc.status != 401:
                    raise
                self.credentials.clear()
        result = None
        if existing_session:
            result = await self._existing_session_request("POST", "/device/enroll")
            if result:
                emit("Reusing your existing OpenVegas account; browser login skipped.")
        if result is None:
            verifier = secrets.token_urlsafe(48)
            proof = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            start = await self.request("POST", "/device/start", data={"code_challenge": proof})
            url = self.backend + "/ui/connect?" + urlencode({"code": start["user_code"]})
            emit("Approve only the matching code: " + start["user_code"])
            emit("Sign in or sign up in your browser: " + url)
            try:
                open_browser(url)
            except (OSError, webbrowser.Error):
                emit("Open that link on a browser; this terminal will wait up to five minutes.")
            deadline = time.monotonic() + min(start["expires_in"], 300)
            while time.monotonic() < deadline:
                await asyncio.sleep(max(2, start["interval"]))
                result = await self.request("POST", "/device/exchange", data={
                    "device_code": start["device_code"], "verifier": verifier})
                if result.get("status") == "approved":
                    break
            else:
                raise IntegrationError("Browser approval expired. No host integrations were installed.")
        try:
            storage = self.credentials.save(result["token"], allow_file=allow_file)
        except Exception:
            await self.request("POST", "/revoke", token=result["token"])
            raise
        emit("Account connected; integration credential saved in " + storage + ".")
        return await self.account()

    async def host_token(self, host):
        cached = self.leases.get(host)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        device = self.credentials.load()
        if not device:
            raise IntegrationError("OpenVegas sign-in required. Run openvegas go outside the host.")
        result = await self.request("POST", "/lease", token=device, data={"host": host})
        self.leases[host] = result["token"], time.monotonic() + result["expires_in"] - 30
        return result["token"]

    async def state(self, host):
        value = await self.request("GET", "/state", token=await self.host_token(host))
        token = os.getenv("OPENVEGAS_NATIVE_TOKEN", "")
        if token:
            try:
                status = await self.request("GET", "/native/session", token=token)
                if status.get("host") != host or status.get("user_id") != value.get("user_id"):
                    raise IntegrationError("Host spending session mismatch.")
                value.update(billing_mode="openvegas_v", spending=status,
                             notice="$V funds this explicitly approved API session, not subscription quota.")
            except IntegrationError:
                value.update(billing_mode="openvegas_v_unavailable", spending=None,
                             notice="$V session status unavailable; no automatic switch to subscription billing.")
        return value

    async def equip(self, host, slot, item_id):
        return await self.request("POST", "/cosmetics/equip", token=await self.host_token(host),
                                  data={"slot": slot, "item_id": item_id})

    async def _cosmetics_request(self, host, path, limit):
        """Bound bytes before parsing private assets; never follow signed redirects."""
        token = await self.host_token(host)
        try:
            async with asyncio.timeout(3):
                async with self.http.stream("GET", "/integrations/cosmetics" + path,
                                            headers={"Authorization": "Bearer " + token}) as response:
                    if not response.is_success:
                        if response.status_code in {401, 403}:
                            self.leases.pop(host, None)
                        raise IntegrationError("Owned cosmetics unavailable; art cleared.", response.status_code)
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > limit:
                            raise IntegrationError("Cosmetic response exceeds size limit.")
                        data.extend(chunk)
                    return json.loads(data)
        except (httpx.HTTPError, TimeoutError, ValueError, RecursionError):
            raise IntegrationError("Owned cosmetics unavailable; art cleared.") from None

    async def cosmetics_owned(self, host):
        from openvegas.emotes.remote import MAX_OWNED_BYTES
        return await self._cosmetics_request(host, "/owned", MAX_OWNED_BYTES)

    async def cosmetics_pack(self, host, item_id):
        from openvegas.emotes.manifest import safe_token
        from openvegas.emotes.remote import MAX_BUNDLE_BYTES
        return await self._cosmetics_request(host, "/" + safe_token(item_id) + "/pack", MAX_BUNDLE_BYTES)

    async def revoke(self):
        token = self.credentials.load()
        if token:
            await self.request("POST", "/revoke", token=token)
        self.credentials.clear()
        from .game_browser import game_credentials
        game_credentials(self.backend).clear()
        self.leases.clear()
