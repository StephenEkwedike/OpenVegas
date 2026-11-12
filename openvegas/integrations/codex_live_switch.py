"""Fail-closed coordination for an owned Codex runtime, never a provider proxy.

The grant boundary is used by the managed Supervisor today. The live transition
requires a transport bound from launch and a lifecycle owner able to attest a
detached source and a hydrated target. The legacy after-exit snapshot is *not*
a detached-peer attestation. Do not enable a same-TUI entry point without both.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import re
import secrets
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from time import monotonic

from . import codex_handoff_policy as policy
from . import native
from .broker import Broker
from .credentials import IntegrationError

HISTORY_PAGE_SIZE = 128
HISTORY_MAX_PAGES = 64
HISTORY_TIMEOUT = 10


def require(value, message):
    if not value:
        raise IntegrationError(message)


@dataclass(frozen=True)
class Selection:
    model: str
    max_tokens: int
    limit_v: Decimal | None
    quote: dict | None = field(default=None, repr=False, compare=False)

    @classmethod
    def reviewed(cls, options, model):
        from .billing_choices import wallet_ceiling
        from .billing_host import live_wallet, wallet_row

        require(
            options.get("wallet_billing") is True
            and options.get("host") == "codex"
            and options.get("provider") == "openai",
            "Unverified direct Codex wallet route.",
        )
        require(
            isinstance(options.get("models"), list)
            and sum(isinstance(r, dict) and r.get("model") == model for r in options["models"])
            == 1,
            "Expected exactly one reviewed entry for the active Codex model.",
        )
        row = wallet_row("codex", options, model)
        cap = row["max_tokens"]
        balance = native._decimal(options.get("balance_v"), allow_zero=True)
        require(
            balance > 0 and type(cap) is int and 1 <= cap <= 128000,
            "Balance or output cap unavailable.",
        )
        quote = None
        if "pricing" in row:
            require(
                (row.get("host"), row.get("provider")) == ("codex", "openai"),
                "Reviewed quote changed the native route.",
            )
            native._pricing(row["pricing"])
            quote = deepcopy(row)
        return cls(model, cap, None if live_wallet(options) else wallet_ceiling(balance), quote)


class GrantBoundary:
    """Single grant exchange through the existing Broker; never persist a bearer.

    native.launch owns pricing, model, auth-store and actual server checks.
    Record uncertainty *before* awaiting an effect. An unacknowledged POST or
    DELETE cannot be retried and cannot authorize a return to host billing.
    """

    def __init__(self, broker, selection):
        self._broker = broker
        self.selection = selection
        self.state = "new"
        self._grant_digest = None
        self._close_token = None
        self._close_lock = asyncio.Lock()

    @property
    def credentials(self):
        return self._broker.credentials

    @property
    def backend(self):
        return self._broker.backend

    async def request(self, method, path, *, token=None, data=None):
        call = method, path
        if call in {("POST", "/native/quote"), ("POST", "/native/sessions")}:
            require(
                isinstance(data, dict)
                and isinstance(token, str)
                and token.startswith("ov_device_")
                and (
                    data.get("host"),
                    data.get("provider"),
                    data.get("model"),
                    data.get("max_tokens"),
                )
                == ("codex", "openai", self.selection.model, self.selection.max_tokens),
                "Unverified Codex selection or control credential.",
            )
            if path == "/native/quote":
                require(self.state == "new", "Duplicate native quote refused.")
                self.state = "quoting"
                # Options rows use the same server selection as /quote. Issuance
                # still reauthorizes and recomputes its pricing fingerprint.
                result = (deepcopy(self.selection.quote) if self.selection.quote is not None
                          else await self._broker.request(method, path, token=token, data=data))
                self.state = "quoted"
                return result
            limit = self.selection.limit_v
            require(
                self.state == "quoted"
                and data.get("confirmed") is True
                and data.get("billing_mode") == "wallet"
                and (
                    "limit_v" not in data
                    if limit is None
                    else data.get("limit_v") == format(limit, "f")
                ),
                "Unverified or repeated wallet grant refused.",
            )
            self.state = "issue_unknown"
            result = await self._broker.request(method, path, token=token, data=data)
            bearer = result.get("token") if isinstance(result, dict) else None
            if isinstance(bearer, str) and re.fullmatch(r"ov_native_[A-Za-z0-9_-]{16,128}", bearer):
                self._grant_digest = hashlib.sha256(bearer.encode()).digest()
                self._close_token = bearer  # Ephemeral capability, only for this grant's DELETE.
                self.state = "issued"
            return result
        require(
            call in {("GET", "/native/session"), ("DELETE", "/native/session")}
            and isinstance(token, str)
            and hashlib.sha256(token.encode()).digest() == self._grant_digest,
            "Unexpected wallet operation, stale grant or credential.",
        )
        if method == "DELETE":
            async with self._close_lock:
                if self.state == "closed":
                    return {"closed": True}  # Prior positive ACK for this exact bearer.
                require(self.state == "issued", "Wallet closure unknown. No duplicate DELETE.")
                self.state = "close_unknown"
                self._close_token = None
                result = await self._broker.request(method, path, token=token)
                require(
                    isinstance(result, dict) and result.get("closed") is True,
                    "Wallet closure not acknowledged. No automatic return or retry.",
                )
                self.state = "closed"
                return result
        require(self.state == "issued", "Wallet session is no longer open.")
        return await self._broker.request(method, path, token=token)

    async def revoke_known(self):
        from .codex_billing_route import _settle

        if self._close_token is not None:
            _, cancelled = await _settle(
                asyncio.create_task(
                    self.request("DELETE", "/native/session", token=self._close_token)
                )
            )
            if cancelled:
                raise asyncio.CancelledError

    def require_released(self):
        require(
            self.state in ("new", "quoting", "quoted", "closed"),
            "Wallet grant issuance or closure is unconfirmed. No automatic return or retry.",
        )


class LiveSwitch:
    """One coordinator per peer-bound LiveTransport, not one per hook request.

    owner operates only on owned native backends. check_owner is synchronous;
    target preparation is responsible for route-auth verification and confirmed
    release of the previous grant. Neither credentials nor prompts enter the
    transport. A source refusal can thaw only before a detach attempt.
    """

    def __init__(self, transport, owner):
        self.transport = transport
        self.owner = owner
        self.busy = False
        self.uncertain = False
        self.phase = "idle"

    async def change(self, ticket):
        from .codex_live_transport import TransportRefused

        t, owner = self.transport, self.owner
        require(not self.busy and not self.uncertain, "Transition busy or uncertain; no retry.")
        generation = t.generation
        self.busy = True
        try:
            self.phase = "preflight"
            owner.check_owner(ticket, generation)
            await owner.preflight(ticket)
            owner.check_owner(ticket, generation)
            self.phase = "freeze"
            require(ticket.guard is not None, "Missing authenticated intent guard; no switch.")
            await t.freeze(generation, intent=ticket.guard.token)
            try:
                # Full idle admission, not the active hook's preview.
                self.phase = "source_idle"
                live = await policy.capture_source(owner.source_endpoint, ticket.identifier)
                require(
                    (live.thread_id, live.model, live.cwd)
                    == (ticket.identifier, ticket.host_model, owner.cwd),
                    "Source thread, model or directory changed; no detach.",
                )
                self.phase = "source_history"
                history = await owner.history(owner.source_endpoint, ticket.identifier)
                owner.check_owner(ticket, generation)
            except IntegrationError:
                await t.thaw(generation)
                raise
            self.uncertain = True  # Includes cancellation *during* detach.
            try:
                self.phase = "detach"
                proof = await t.detach(generation)
            except TransportRefused:
                if t.state == "frozen":
                    await t.thaw(generation)
                    self.uncertain = False
                raise
            self.phase = "detached_policy"
            source = await owner.detached_snapshot(ticket, generation, proof)
            policy.require_snapshot(source)
            require(
                (source.thread_id, source.model, source.cwd)
                == (live.thread_id, live.model, live.cwd),
                "Detached source identity is not the verified original.",
            )
            self.phase = "retire"
            await owner.retire_source(ticket, source)
            owner.check_owner(ticket, generation)
            self.phase = "target_prepare"
            endpoint = await owner.prepare_target(ticket, source)
            require(endpoint != source._runtime, "Source endpoint cannot be its own successor.")
            owner.check_owner(ticket, generation)
            self.phase = "attach"
            await t.attach(generation, endpoint, ticket.identifier)
            self.phase = "target_policy"
            await owner.verify_attached(ticket, endpoint, source)
            self.phase = "target_history"
            require(
                await owner.history(endpoint, ticket.identifier) == history,
                "Persisted native turn identities changed; no admission.",
            )
            owner.check_owner(ticket, generation)
            self.phase = "admit"
            await t.admit(generation, commit=lambda: owner.commit(ticket))
            self.uncertain = False
            self.phase = "committed"
        except asyncio.CancelledError:
            self.uncertain = self.phase != "preflight"
            raise
        except TransportRefused:
            raise IntegrationError(
                "Live transport refused the switch. No automatic retry."
            ) from None
        except IntegrationError:
            raise
        except Exception:  # noqa: BLE001 - Do not disclose RPC payloads or retry an ambiguous effect.
            self.uncertain = self.phase != "preflight"
            raise IntegrationError(
                "Native transition unverified. No automatic fallback or replay."
            ) from None
        finally:
            try:
                if self.uncertain:
                    # No policy/auth RPCs, including bootstrap reads, after
                    # an ambiguous detach, revocation, resume or admission.
                    await t.abort()
            finally:
                self.busy = False


@dataclass(frozen=True, eq=False, repr=False)
class IntentGuard:
    owner: RuntimeOwner
    generation: int
    token: object
    target: str

    def release(self):
        if self.owner.intent is not self:
            return False
        released = self.owner.transport.release_intent(self.generation, self.token)
        self.owner.intent = None
        return released


class RuntimeOwner:
    """Own exactly one foreground TUI and, sequentially, its native app-servers."""

    def __init__(self, supervisor, env):
        self.s, self.env = supervisor, dict(env)
        self.executable = None
        self.cwd = supervisor.launch_spec.cwd
        self.servers = AsyncExitStack()
        self.task = self.wallet_task = self.release = self.source_endpoint = None
        self.ending = False
        self.revision = self.lease = self.physical_generation = None
        self.original = None
        self.grant = None
        self.intent = None
        self._selection = None

    async def run(self, argv):
        from .codex_billing_route import host_server
        from .codex_binary import resolve_native_codex
        from .codex_live_transport import LiveTransport

        if self.env.get("OPENVEGAS_CODEX_STABLE_FRONT", "1") == "1":
            from .codex_stable_transport import StableTransport
            LiveTransport = StableTransport

        s = self.s
        self.executable = await asyncio.to_thread(
            resolve_native_codex, s.executable, s.launch_spec.version
        )
        require(
            "live_codex_executable" in inspect.signature(native.launch).parameters,
            "Wallet launcher cannot verify the live native binary. No live session started.",
        )
        require(
            callable(getattr(policy, "capture_detached_source", None))
            and callable(getattr(policy, "verify_attached_target", None)),
            "Live Codex policy verification is unavailable. No live switch started.",
        )
        try:
            try:
                self.source_endpoint = await self.servers.enter_async_context(
                    host_server(self.executable, self.env, cwd=self.cwd)
                )
                await policy.check_source_config(self.source_endpoint, self.cwd)
            except (IntegrationError, OSError, TimeoutError, ImportError) as exc:
                # No TUI has attached yet. Retire this owned server before
                # preserving the original launch, never downgrade a live chat.
                await self.servers.aclose()
                self.source_endpoint = None
                s.codex_unavailable = (str(exc) if isinstance(exc, IntegrationError)
                                       else 'Managed Codex startup unavailable.')
                s.emit(s.codex_unavailable + ' Starting the original host without handoff.')
                return await s._terminal_child([s.executable, *s.launch_spec.original_argv], self.env)
            async with LiveTransport(self.source_endpoint, on_notice=s.emit) as self.transport:
                self.switch = LiveSwitch(self.transport, self)
                s.codex_endpoint, s.live_owner = self.source_endpoint, self
                try:
                    code = await s._terminal_child(
                        [self.executable, "--remote", self.transport.endpoint, *argv], self.env
                    )
                finally:
                    self.ending = True
                    if self.intent and not (self.task and not self.task.done()):
                        self.intent.release()
                    if self.task:
                        await asyncio.shield(self.task)
                    if hasattr(self, "replace_wallet"):
                        await self.replace_wallet(None)
                require(
                    not self.switch.uncertain,
                    "Live payer uncertain. Inspect grant status; no automatic retry.",
                )
                return code
        finally:
            s.live_owner = s.codex_endpoint = None
            if self.source_endpoint is not None:
                try:
                    await policy.wait_until_idle(self.source_endpoint, s.identifier)
                except BaseException:
                    if self.grant:
                        await self.grant.revoke_known()
                    raise  # An unverified process group is not permission to terminate it.
            await self.stop_server()

    def bind(self, pid):
        self.transport.bind_peer(pid)

    def reserve(self, target):
        from .billing_host import ARM_SECONDS, ExitIntent
        from .codex_live_transport import TransportRefused, TransportUncertain

        require(
            not self.ending
            and not self.switch.uncertain
            and self.intent is None
            and not (self.task and not self.task.done()),
            "Live switch busy or uncertain. No automatic retry.",
        )
        self.revision, self.lease = self.s.intent_revision, self.s.session_lease
        self.physical_generation = self.s.generation
        s, t = self.s, self.transport
        self.check_owner(ExitIntent(s.mode, target, s.identifier, s.generation,
                                    monotonic() + ARM_SECONDS), t.generation)
        try:
            token = t.set_intent(t.generation, s.identifier)
        except (TransportRefused, TransportUncertain):
            raise IntegrationError("Cannot guard this native input route. No switch started.") from None
        self.intent = IntentGuard(self, t.generation, token, target)
        return self.intent

    def begin(self, item):
        guard = item.guard
        require(guard is not None and self.intent is guard and guard.target == item.target
                and not (self.task and not self.task.done()), "Stale or missing native intent.")
        self.check_owner(item, guard.generation)
        self.task = asyncio.create_task(self._perform(item))
        # Also runs if cancelled before the coroutine ever executes.
        self.task.add_done_callback(lambda _: guard.release())

    async def _perform(self, ticket):
        try:
            await self.switch.change(ticket)
            if not self.ending and self.s.child.returncode is None:
                await self.replace_wallet(ticket.target)
        except IntegrationError:
            self.s.local_error = True
            # No arbitrary native/transport diagnostics in the user's terminal.
            message = (
                "Live billing transition is uncertain. No fallback or retry; exit and inspect the grant."
                if self.switch.uncertain
                else "Live switch refused. Original payer unchanged; no retry."
            )
            self.s.provisional_reason = message
            self.s.emit(message + " Stage: " + self.switch.phase + ".")
            if self.switch.uncertain and self.wallet_task and self.source_endpoint:
                # The front is aborted. Do not leave a known grant usable
                # merely because the disconnected TUI has not exited yet.
                # Neither idle nor a successful DELETE permits automatic recovery.
                if self.grant:
                    await self.grant.revoke_known()
                await policy.wait_until_idle(self.source_endpoint, self.s.identifier)
                await self.stop_server()
        finally:
            self._selection = None
            self.s.wallet_status_changed.set()

    def check_owner(self, ticket, generation):
        s = self.s
        require(
            not self.ending
            and s.live_owner is self
            and s.modern_relay
            and s.runtime_lock.locked()
            and self.lease is not None
            and self.lease is s.session_lease
            and s.generation
            == self.physical_generation
            == s.registered_generation
            == ticket.generation
            and self.revision == s.intent_revision
            and self.transport.generation == generation
            and ticket.expires > monotonic()
            and ticket.identifier == s.identifier
            and (ticket.source, ticket.target) in {("host", "v"), ("v", "host")}
            and s.mode == ticket.source
            and s.child is not None
            and s.child.returncode is None
            and s.child.pid == self.transport.peer_pid
            and s.launch_spec.cwd == os.getcwd(),
            "Native session ownership, intent or generation changed.",
        )

    async def preflight(self, ticket):
        from .billing_host import exact_model

        self._selection = None
        if ticket.target == "v":
            options = await self.s.preflight_v(ticket.host_model)
            self._selection = (ticket, Selection.reviewed(options, ticket.host_model))
        else:
            require(
                self.original is not None and exact_model(ticket.host_model),
                "Cannot verify the selected native model and original policy.",
            )

    @staticmethod
    async def history(endpoint, sid):
        # Do not request message bodies. Bound complete metadata; never copy
        # turns, serialize transcripts or reconstruct a user's composer.
        params = {
            "threadId": sid,
            "limit": HISTORY_PAGE_SIZE,
            "sortDirection": "asc",
            "itemsView": "notLoaded",
        }
        cursors, ids = set(), {}
        try:
            async with asyncio.timeout(HISTORY_TIMEOUT), policy._reader(endpoint) as call:
                for _ in range(HISTORY_MAX_PAGES):
                    page = await call("thread/turns/list", dict(params))
                    require(
                        isinstance(page, dict) and "nextCursor" in page,
                        "Native history pagination status unavailable.",
                    )
                    rows = page.get("data")
                    require(
                        isinstance(rows, list) and 0 < len(rows) <= HISTORY_PAGE_SIZE,
                        "Empty or invalid native history page.",
                    )
                    for row in rows:
                        require(
                            isinstance(row, dict)
                            and isinstance(row.get("id"), str)
                            and 0 < len(row["id"]) <= 128
                            and row["id"] not in ids
                            and row.get("items") == []
                            and row.get("itemsView") == "notLoaded"
                            and row.get("status") in ("completed", "interrupted", "failed"),
                            "Invalid or duplicate native turn identity.",
                        )
                        ids[row["id"]] = row["status"]
                    cursor = page["nextCursor"]
                    if cursor is None:
                        return tuple(ids.items())
                    require(
                        isinstance(cursor, str)
                        and 0 < len(cursor) <= 4096
                        and cursor.isascii()
                        and cursor.isprintable()
                        and cursor not in cursors,
                        "Malformed or repeated native history cursor.",
                    )
                    cursors.add(cursor)
                    params["cursor"] = cursor
        except TimeoutError:
            raise IntegrationError("Complete native history verification timed out.") from None
        raise IntegrationError(
            "Native history exceeds the bounded full-verification budget; not truncated."
        )

    async def detached_snapshot(self, ticket, generation, proof):
        return await policy.capture_detached_source(
            self.source_endpoint,
            ticket.identifier,
            transport=self.transport,
            proof=proof,
            generation=generation,
            peer_pid=self.s.child.pid,
        )

    async def stop_server(self):
        if self.wallet_task:
            from .codex_billing_route import _settle

            self.release.set()
            try:
                # Never cancel a possibly submitted grant POST or its one-time
                # revoke just because the outer cleanup was cancelled.
                _, cancelled = await _settle(self.wallet_task)
                if cancelled:
                    raise asyncio.CancelledError
            finally:
                if self.wallet_task.done():
                    self.wallet_task = self.release = None
                    self.source_endpoint = None
        await self.servers.aclose()
        self.source_endpoint = None

    async def retire_source(self, ticket, source):
        if self.original:
            policy.assert_same_policy(self.original, source)
        else:
            self.original = source
        await self.replace_wallet(None)
        if self.grant is None:
            await self.stop_server()
        else:
            from .codex_billing_route import _settle

            async def retire():
                # The source is detached and idle. Revoke during native teardown,
                # but require both proofs before any successor can start.
                results = await asyncio.gather(self.grant.revoke_known(), self.stop_server(),
                                               return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result

            _, cancelled = await _settle(asyncio.create_task(retire()))
            if cancelled:
                raise asyncio.CancelledError
        self.s.codex_endpoint = None
        # Only the next private server's hook gets the new control credential.
        from .use import TOKEN

        self.env[TOKEN] = self.s.env[TOKEN] = self.s.token = secrets.token_urlsafe(32)

    async def prepare_target(self, ticket, source):
        from .codex_billing_route import host_server
        from .session_ownership import GENERATION_ENV, OWNER_ENV
        from .use import SOCKET, TOKEN

        s = self.s
        reviewed, self._selection = self._selection, None
        if reviewed is not None:
            require(reviewed[0] is ticket and reviewed[1].model == ticket.host_model,
                    "Wallet selection belongs to a different transition.")
        if ticket.target == "host":
            ep = await self.servers.enter_async_context(
                host_server(self.executable, self.env, cwd=self.cwd)
            )
            self.source_endpoint = ep
            await policy.verify_target(ep, source, model=ticket.host_model)
        else:
            broker = Broker()
            ready = asyncio.get_running_loop().create_future()
            release = self.release = asyncio.Event()
            generation = self.transport.generation

            async def launch():
                boundary = None
                try:
                    selection = (reviewed[1] if reviewed is not None
                                 else Selection.reviewed(await s.v_options(broker), ticket.host_model))
                    boundary = self.grant = GrantBoundary(broker, selection)

                    def authorize():
                        self.check_owner(ticket, generation)
                        return True

                    async def hold(argv, env):
                        authorize()
                        require(
                            argv[0] == self.executable and argv.count("--remote") == 1,
                            "Wallet native executable or endpoint changed.",
                        )
                        ep = argv[argv.index("--remote") + 1]
                        require(
                            argv
                            == native.codex_resume_argv(
                                self.executable,
                                ep,
                                ticket.host_model,
                                s.launch_spec.resume_arguments(s.identifier),
                            ),
                            "Unexpected wallet attach arguments.",
                        )
                        self.source_endpoint = ep
                        ready.set_result(ep)
                        await release.wait()
                        return 0

                    await native.launch(
                        "codex",
                        selection.model,
                        selection.limit_v,
                        selection.max_tokens,
                        host_args=s.launch_spec.resume_arguments(s.identifier),
                        broker=boundary,
                        emit=s.emit,
                        confirm=authorize,
                        local_tools_only=True,
                        provider="openai",
                        runner=hold,
                        resume_provider="openai",
                        compact=True,
                        control_environment={
                            k: self.env[k] for k in (SOCKET, TOKEN, OWNER_ENV, GENERATION_ENV)
                        },
                        wallet_mode=True,
                        codex_handoff_source=source,
                        live_codex_executable=self.executable,
                    )
                finally:
                    try:
                        if boundary:
                            boundary.require_released()
                    finally:
                        await broker.close()
                        self.grant = None

            task = self.wallet_task = asyncio.create_task(launch())
            done, _ = await asyncio.wait((ready, task), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
                raise IntegrationError("No verified wallet endpoint was produced.")
            ep = ready.result()
        return ep

    async def verify_attached(self, ticket, endpoint, source):
        await policy.verify_attached_target(
            endpoint,
            source,
            model=ticket.host_model,
            transport=self.transport,
            generation=self.transport.generation,
            peer_pid=self.s.child.pid,
        )

    def commit(self, ticket):
        s = self.s
        s.intent_revision += 1  # Invalidate pending menu/hook checks on the old payer.
        s.mode, s.codex_endpoint = ticket.target, self.source_endpoint
        if ticket.target == "v":
            s.restore_model = ticket.host_model
        s.transition_state = "MODE_COMMITTED"
        s.emit(
            "Native payer now "
            + ("$V" if s.mode == "v" else "host")
            + "; same chat, no prompt submitted."
        )
