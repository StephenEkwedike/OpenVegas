"""Opt-in Windows payer controller. The native front end owns all user input."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import Enum

import httpx

from .billing_host import exact_model, session_id
from .broker import Broker as DeviceBroker
from .credentials import IntegrationError
from .inference_router_adapter import VERSIONS, ProviderAdapter
from .inference_router_session import RouterSession, claude_wallet_callbacks
from .session_ownership import SessionLease
from .terminal_wallet import wallet_value


def require(condition):
    if not condition:
        raise IntegrationError(
            "Windows native ownership, selection or routing could not be verified."
        )


class Check(Enum):
    ROUTE = "native-route"
    INVENTORY = "native-inventory"
    IDENTITY = "native-root"
    MODEL = "native-model"
    WORKSPACE = "native-workspace"
    FENCE = "native-fence"
    LEASE = "session-lease"


class CheckRefused(IntegrationError):
    def __init__(self, step):
        self.step = step
        super().__init__("Control check: " + step.value + ".")


@contextmanager
def checking(step):
    try:
        yield
    except CheckRefused:
        raise
    except Exception:  # noqa: BLE001 - Only the fixed check name can reach the composer.
        raise CheckRefused(step) from None


def same_workspace(left, right):
    # Rust may serialize a native absolute drive path with a verbatim prefix.
    # Never resolve a UNC name, follow a junction, or fold directory case.
    def path(value):
        if not isinstance(value, str):
            return None
        value = value.replace("/", "\\").removeprefix("\\\\?\\")
        return value if not value.startswith("\\\\") else None

    a, b = path(left), path(right)
    return a is not None and a == b


@dataclass(frozen=True, repr=False)
class Selection:
    identifier: str
    model: str
    cwd: str
    base_url: str


class WindowsDevice:
    """Use exactly the OS vault, under the existing backend/profile file-refusal policy."""

    def __init__(self, credentials):
        self.credentials = credentials
        self._device = None

    def load(self):
        if self._device is None:
            from keyring import get_keyring
            from keyring.backends.Windows import WinVaultKeyring

            require(type(get_keyring()) is WinVaultKeyring)
            device = self.credentials.load()
            require(isinstance(device, str) and device.startswith("ov_device_"))
            self._device = device
        return self._device


class Broker(DeviceBroker):
    def __init__(self):
        from openvegas.config import FALLBACK_DEFAULT_BACKEND_URL

        from .credentials import backend_url

        require(backend_url() == FALLBACK_DEFAULT_BACKEND_URL)
        super().__init__(
            backend=FALLBACK_DEFAULT_BACKEND_URL,
            transport=httpx.AsyncHTTPTransport(trust_env=False),
        )
        self.credentials = WindowsDevice(self.credentials)


class Customer:
    def __init__(self, host, broker, upstream, cwd, *, version=None):
        self.host, self.broker, self.cwd = host, broker, cwd
        self.version = version or VERSIONS[host]
        self.current = self.observe = self.guard = self.lease = self.sid = None
        self.initial_model = None
        self.initial_access = None
        self.refusal = None
        self.busy = False
        if host == "codex":
            from .inference_router_controls import WalletPreparationRejected
            from .inference_router_wallet import codex_wallet_callbacks
            from .windows_customer_codex import RootAdapter, RootIngress

            prepare_codex, revoke_codex = codex_wallet_callbacks(
                broker, active_model=self.model, version=self.version
            )

            async def prepare_codex_wallet(ticket):
                if not self.session._native_wallet_launch:
                    raise WalletPreparationRejected(
                        "Windows wallet billing requires a dedicated wallet-first launch; "
                        "no grant opened and no host fallback.")
                return await prepare_codex(ticket)

            prepare_codex_wallet.retained_grants = prepare_codex.retained_grants
            callbacks = prepare_codex_wallet, revoke_codex
            adapter = RootAdapter(
                host, upstream, broker.backend + "/integrations/native", lambda: self.sid
            )
        else:
            require(host == "claude")
            callbacks = claude_wallet_callbacks(broker, active_model=self.model)
            adapter = ProviderAdapter(host, upstream, broker.backend + "/integrations/native")
        self.prepare, revoke = callbacks
        if host == "codex":
            switch_revoke = revoke

            async def revoke(access):
                if self.initial_access is not None and access is self.initial_access:
                    await access.grant.close()
                    return access.grant.released
                return await switch_revoke(access)

        self.session = RouterSession(
            adapter,
            prepare_wallet=self.prepare,
            revoke_wallet=revoke,
            request_boundary=True,
            local_auth="peer" if host == "codex" else "header",
            task_boundary=self.boundary,
            commit_guard=self.commit_guard,
        )
        if host == "codex":
            from .codex_title_origin import TitleOrigins
            self.session.router.title_origins = TitleOrigins(self.session.state)
            self.session.router = RootIngress(self.session.router, adapter.root)

    def model(self):
        require(self.current is not None)
        return self.current.model

    def bind_initial_root(self, value):
        require(
            self.host == "codex" and self.initial_model is not None
            and self.session.state.mode == "v" and self.session.state.pending is None
            and self.sid is None and self.lease is None
        )
        with checking(Check.IDENTITY):
            require(isinstance(value, Selection) and session_id(value.identifier))
        with checking(Check.WORKSPACE):
            require(same_workspace(value.cwd, self.cwd))
        with checking(Check.ROUTE):
            require(value.base_url == self.session._url)
        with checking(Check.MODEL):
            require(exact_model(value.model))
        with checking(Check.LEASE):
            self.lease = SessionLease(self.host, value.identifier)
        self.sid = value.identifier

    async def boundary(self, ticket):
        try:
            return await self._boundary(ticket)
        except CheckRefused as exc:
            if self.session.state.pending is ticket:
                self.refusal = exc.step
            raise

    async def _boundary(self, ticket):
        guard, observe = self.guard, self.observe
        require(self.busy and self.session.state.pending is ticket)
        guard()
        value = await observe()
        require(self.busy and self.session.state.pending is ticket and self.observe is observe)
        guard()
        with checking(Check.IDENTITY):
            require(isinstance(value, Selection) and session_id(value.identifier))
        with checking(Check.WORKSPACE):
            require(same_workspace(value.cwd, self.cwd))
        with checking(Check.ROUTE):
            require(value.base_url == self.session._url)
        with checking(Check.MODEL):
            require(
                exact_model(value.model)
                and (self.host != "claude" or value.model.startswith("claude-"))
            )
        if self.sid is None:
            with checking(Check.LEASE):
                self.lease = SessionLease(self.host, value.identifier)
            self.sid = value.identifier
        with checking(Check.FENCE):
            require(self.sid == value.identifier and self.current in (None, value))
        self.current = value
        return True

    def commit_guard(self, ticket):
        self.guard()
        require(
            self.busy
            and self.session.state.pending is ticket
            and self.current is not None
            and self.lease is not None
        )

    async def balance(self):
        try:
            async with asyncio.timeout(3):
                account = await self.broker.account()
                return "$V " + wallet_value(account["balance"])
        except (ImportError, IntegrationError, ValueError, TypeError, KeyError, TimeoutError):
            return "balance unavailable"

    async def command(self, action, *, observe, guard):
        require(action in ("host", "v", "status"))
        guard()
        if self.busy:
            return "Billing control busy; no new switch started."
        if self.session.state.pending is not None:
            return "Billing outcome unconfirmed; inference held. No automatic retry."
        if action == "status":
            balance = await self.balance()
            if self.session.state.pending is not None:
                return "Billing change pending or unconfirmed; check status."
            return f"Payer: {self.session.state.mode}; {balance}."
        self.busy = True
        self.refusal = None
        self.current, self.observe, self.guard = None, observe, guard
        try:
            result = await self.session.command("openvegas use " + action)
            detail = ""
            if self.host == "codex" and result.billing != action:
                # Router outcomes may contain several kinds of refusal. Never print an
                # exception or suggest grant creation/noncreation from a stage name.
                reason = (
                    self.refusal.value
                    if self.refusal
                    else {
                        "wallet_not_created": "wallet-prepare",
                        "wallet_outcome_unconfirmed": "wallet-outcome",
                    }.get(result.outcome, "request-boundary")
                )
                detail = " Control check: " + reason + "."
            if self.session.state.pending is not None:
                return "Billing outcome unconfirmed; inference held. No automatic retry." + detail
            if result.billing != action:
                return "Switch refused; payer unchanged. No prompt sent." + detail
            return "Payer: " + result.billing + ". No prompt sent."
        finally:
            self.current = self.observe = self.guard = None
            self.refusal = None
            self.busy = False

    async def close(self):
        try:
            await self.session.controls.settle_callbacks()
            # A validated issue can fail before RouterSession receives WalletAccess.
            grants = list(self.prepare.retained_grants)
            if self.initial_access is not None:
                grants.append(self.initial_access.grant)
            for grant in grants:
                owner = getattr(grant, "boundary", grant)
                if owner is not None and owner.state == "issued":
                    await grant.close()
                if not grant.released:
                    raise IntegrationError("Wallet release unconfirmed; no automatic retry.")
        finally:
            if self.lease is not None:
                self.lease.close()


@asynccontextmanager
async def customer(host, upstream, cwd, *, version=None):
    from openvegas.config import FALLBACK_DEFAULT_BACKEND_URL

    broker = Broker()
    try:
        # Neither a fixture URL nor a custom destination is a customer backend.
        require(broker.backend == FALLBACK_DEFAULT_BACKEND_URL)
        owner = Customer(host, broker, upstream, cwd, version=version)
        try:
            yield owner
        finally:
            await owner.close()
    finally:
        await broker.close()


def environment(env):
    # Windows environment names are case-insensitive. Do not leak OpenVegas
    # controls/credentials into either tool-capable client or its subprocesses.
    upper = {k.upper(): v for k, v in env.items()}
    require(
        not any(
            upper.get(k)
            for k in (
                "OPENVEGAS_NATIVE_TOKEN",
                "OPENVEGAS_SWITCH_TOKEN",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "OPENVEGAS_NATIVE_FIXTURE",
            )
        )
    )
    return {k.upper(): v for k, v in env.items() if not k.upper().startswith(("OPENVEGAS_", "OV_"))}


async def run(host, exe, env, cwd, *, version=None):
    if host == "claude":
        from .windows_customer_claude import run_fixed
        return await run_fixed(exe, env, cwd, version=version)
    initial = "host"
    if host == "codex":
        from .wallet_startup import initial_billing

        initial = initial_billing({k.upper(): v for k, v in env.items()})
    env = environment(env)
    if host == "codex":
        from . import windows_customer_codex as client

        if initial == "v":
            upstream, model = await client.original_upstream(
                exe, env, cwd, include_model=True, require_login=False
            )
        else:
            upstream = await client.original_upstream(exe, env, cwd)
    else:
        from . import windows_customer_claude as client

        client.check_settings(env, cwd)
        upstream = "https://api.anthropic.com"
    async with customer(host, upstream, cwd, version=version) as owner:
        if initial == "v":
            from .wallet_startup import prepare_wallet

            access = await prepare_wallet(owner.broker, model, owner.version)
            # Retain ownership even if installation or listener startup fails.
            owner.initial_access = access
            owner.session.install_initial_wallet(access)
            owner.initial_model = model
        return await client.run(owner, exe, env)
