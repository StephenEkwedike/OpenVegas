"""Opt-in stock Windows rehearsal entry; never the production managed factory.

With --windows-fixture-port, OPENVEGAS_WINDOWS_MANAGED_REHEARSAL=1 selects
the real Customer/RootIngress/stdio topology. An external disposable runner
must enforce no egress and serve the synthetic broker AND supplier protocol.
There is no alternate executable, vault, native ownership or ledger policy.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from openvegas.config import FALLBACK_DEFAULT_BACKEND_URL as BACKEND
from .broker import Broker as DeviceBroker
from .credentials import IntegrationError
from .inference_router_fixture import SYNTHETIC_KEY

DEVICE = "ov_device_windows_qa_not_a_credential"
GRANT = "ov_native_fixture_windows_conpty_only"
_AUTH = {"authorization", "x-api-key", "cookie", "proxy-authorization", "chatgpt-account-id",
         "openai-organization", "openai-project", "x-openvegas-local-auth"}
_DEVICE = {("GET", "/integrations/account"), ("GET", "/integrations/native/options?host=codex"),
           ("POST", "/integrations/native/quote"), ("POST", "/integrations/native/sessions")}
_GRANT = {("GET", "/integrations/native/session"), ("DELETE", "/integrations/native/session"),
          ("GET", "/integrations/native/codex/catalog?client_version=0.166.0")}


def need(ok):
    if not ok:
        raise IntegrationError("Synthetic managed Windows rehearsal refused; no fallback.")


def auth(headers):
    return [(k.lower(), v) for k, v in headers if k.lower() in _AUTH]


class LocalTransport(httpx.AsyncBaseTransport):
    """Keep the broker's logical production origin; every physical dial is numeric loopback."""
    def __init__(self, port):
        need(type(port) is int and 0 < port <= 65535)
        self.endpoint = f"http://127.0.0.1:{port}"
        self.until = time.monotonic() + 600
        self.local = httpx.AsyncHTTPTransport(trust_env=False, retries=0)

    def live(self):
        need(time.monotonic() < self.until)

    async def handle_async_request(self, request):
        self.live()
        path = request.url.raw_path.decode("ascii")
        key = (request.method, path)
        expected = DEVICE if key in _DEVICE else GRANT if key in _GRANT else None
        need(expected is not None and str(request.url) == BACKEND + path and
             auth(request.headers.multi_items()) == [("authorization", "Bearer " + expected)])
        # Do not forward the original Host header or permit redirects/proxy routing.
        headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() != "host"]
        local = httpx.Request(request.method, self.endpoint + path, headers=headers,
                              content=await request.aread(), extensions=request.extensions)
        return await self.local.handle_async_request(local)

    async def aclose(self):
        self.until = 0
        await self.local.aclose()

    def destination(self, plan, mode):
        self.live()
        parsed = urlsplit(plan.endpoint)
        base = "https://api.openai.com/v1" if mode == "host" else BACKEND + "/integrations/native"
        prefix = base.replace("https://", "wss://", 1) if plan.transport == "websocket" else base
        need(mode in ("host", "v") and plan.transport in ("http", "websocket") and
             plan.endpoint.startswith(prefix + "/") and not parsed.fragment and
             auth(plan.headers) == [("authorization", "Bearer " + (SYNTHETIC_KEY if mode == "host" else GRANT))])
        scheme = "ws" if plan.transport == "websocket" else "http"
        return (self.endpoint.replace("http", scheme, 1) + ("/_host" if mode == "host" else "") +
                parsed.path + ("?" + parsed.query if parsed.query else ""))


class SyntheticDevice:
    def load(self):
        return DEVICE


class Broker(DeviceBroker):
    def __init__(self, transport):
        # Do not even construct Credentials: its profile/vault belongs to the real owner.
        self.backend, self.credentials, self.leases = BACKEND, SyntheticDevice(), {}
        self.http = httpx.AsyncClient(base_url=BACKEND, timeout=15, follow_redirects=False,
                                      trust_env=False, transport=transport)


def prepare(env, cwd):
    from openvegas.emotes._windows_state import private_windows_directory
    from .windows_billing_host import _fixture_policy
    need(_fixture_policy("codex", env, cwd))
    root = Path(env["USERPROFILE"])
    with private_windows_directory(root / "codex") as profile:
        need(not profile.names(1))
        profile.atomic_write("auth.json", json.dumps({"OPENAI_API_KEY": SYNTHETIC_KEY}).encode("ascii"))
        profile.atomic_write("config.toml", (
            'model_provider="openai"\nmodel="gpt-6-astra"\nmodel_reasoning_effort="low"\n'
            'cli_auth_credentials_store="file"\nsandbox_mode="read-only"\napproval_policy="on-request"\n'
            'web_search="disabled"\ncheck_for_update_on_startup=false\n[projects.' +
            json.dumps(str(root / "work")) + ']\ntrust_level="trusted"\n'
            '[tui]\nscreen_reader_detection_done=true\n').encode("ascii"))


async def run(exe, port, env, cwd):
    from . import windows_customer as customer, windows_customer_codex as native
    prepare(env, cwd)
    env = customer.environment(env)
    transport = LocalTransport(port)
    broker, owner = Broker(transport), None
    try:
        upstream = await native.original_upstream(exe, env, cwd)
        need(upstream == "https://api.openai.com/v1")
        owner = customer.Customer("codex", broker, upstream, cwd)
        need(native.root_bounded(owner.session) and owner.session.router.inner.fixture is None)
        owner.session.router.inner.fixture = transport
        return await native.run(owner, exe, env)
    finally:
        try:
            if owner is not None:
                await owner.close()
        finally:
            await broker.close()
