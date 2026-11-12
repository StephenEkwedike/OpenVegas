"""Managed-launch composition for fixed-payer Claude process generations."""
from __future__ import annotations

import asyncio
import fcntl
import os
import secrets
import signal
import socket
import struct
import termios
import tty
from contextlib import AsyncExitStack, asynccontextmanager

from .broker import Broker
from .claude_generation_switch import ClaudeRuntimeOwner, PreparedWallet
from .claude_live_switch import WalletGrant
from .claude_payer_boundary import WalletEpoch
from .credentials import IntegrationError
from .session_ownership import set_relay_input
from .use import TOKEN


@asynccontextmanager
async def wallet_listener(gate):
    import uvicorn

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(16)
    server = uvicorn.Server(uvicorn.Config(gate, interface='asgi3', lifespan='off',
        log_level='critical', access_log=False, ws='none', loop='asyncio', http='h11',
        timeout_graceful_shutdown=185))
    # The terminal supervisor owns process signals, not a per-epoch HTTP server.
    from contextlib import nullcontext
    server.capture_signals = nullcontext
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if task.done():
                    await task
                    raise IntegrationError('Wallet listener did not start.')
                await asyncio.sleep(.01)
        yield PreparedWallet(f'http://127.0.0.1:{listener.getsockname()[1]}', gate)
    finally:
        gate.seal()
        server.should_exit = True
        try:
            await asyncio.shield(task)
        finally:
            listener.close()


class ManagedClaude:
    def __init__(self, supervisor, env, observer):
        self.s, self.env, self.observer = supervisor, dict(env), observer
        self.owner = None
        self.task = None
        self.target = None
        self.grant = None
        self.broker = None
        self.wallet = None
        self.monitor = None
        self.stack = AsyncExitStack()
        self.failed = None
        self.starts = 0
        self.pending_input = bytearray()

    def before_spawn(self, mode, generation, env):
        if self.starts:
            self.s.generation += 1
        self.starts += 1
        self.s.token = secrets.token_urlsafe(32)
        self.s.env[TOKEN] = env[TOKEN] = self.s.token

    async def observe(self, **kwargs):
        self.s.child = kwargs['process']
        result = await self.observer(**kwargs)
        self.s.bind_session(result.snapshot.session_id)
        self.s.registered_generation = self.s.generation
        return result

    async def arm(self, target):
        if self.target == target and not self.failed:
            return False
        if self.failed or self.target is not None or (self.task and not self.task.done()):
            raise IntegrationError('A Claude billing handoff is already in progress.')
        if target == self.s.mode:
            return False
        await self.owner.reserve_change()
        self.target = target
        return True

    async def preflight(self):
        broker = Broker()
        try:
            await self.s.v_options(broker)
        finally:
            await broker.close()

    def begin(self):
        if self.target is not None:
            self.task = asyncio.create_task(self.change(self.target))

    async def cancel_arm(self):
        self.target = None
        await self.owner.cancel_reserved()

    async def replace_wallet(self, mode):
        from .terminal_wallet import CredentialReader, monitor

        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None
        if mode is None or self.env.get('OPENVEGAS_LOCAL_CONTROLS') == '0':
            return
        if self.s.wallet_credentials is None:
            self.s.wallet_credentials = CredentialReader()
        ready = asyncio.Event()
        self.monitor = asyncio.create_task(monitor(self.env, mode, ready=ready,
            credentials=self.s.wallet_credentials, control=self.s.control_event,
            status=self.s.local_status, status_changed=self.s.wallet_status_changed))
        try:
            await asyncio.wait_for(ready.wait(), 3.5)
        except TimeoutError:
            # A read-only balance display is not part of the payer boundary.
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None

    async def commit(self, target, snapshot):
        self.s.mode = target
        self.s.original_model = snapshot.model
        self.s.transition_state = 'MODE_COMMITTED'
        self.s.wallet_status_changed.set()
        await self.replace_wallet(target)

    async def change(self, target):
        broker = None
        new_grant = None
        old_admission_changed = False
        try:
            before = await self.owner.capture()
            prepared = None
            if target == 'v':
                broker = Broker()
                new_grant = WalletGrant(broker)
                route = await new_grant.open(before.snapshot.model)
                prepared = await self.stack.enter_async_context(wallet_listener(WalletEpoch(route)))
            else:
                old_admission_changed = True
                self.wallet.gate.seal()
                await self.grant.close()
            await self.owner.change(target, wallet=prepared)
            self.grant, self.wallet = new_grant, prepared
            previous_broker, self.broker = self.broker, broker
            broker = None  # Keep the issuer alive until the grant is closed.
            if previous_broker:
                try:
                    await previous_broker.close()
                except Exception:  # noqa: BLE001 - transport cleanup cannot undo a committed payer
                    self.s.local_error = True
        except Exception as exc:  # noqa: BLE001 - no credentials/native diagnostics in terminal
            if new_grant is not None and new_grant.state == 'issued':
                try:
                    await new_grant.close()
                except Exception:  # noqa: BLE001 - unknown grant close stays fenced
                    self.failed = 'Wallet closure is unconfirmed; input remains held.'
            if (self.failed is None and self.owner.state == 'reserved'
                    and not old_admission_changed and (new_grant is None or new_grant.released)):
                await self.owner.cancel_reserved()
                self.s.emit(str(exc) if isinstance(exc, IntegrationError)
                            else 'Wallet connection unavailable. Billing is unchanged.')
            else:
                self.failed = self.failed or 'Claude billing handoff could not be verified. Input remains held.'
                self.s.emit(self.failed)
        finally:
            self.target = None
            if broker:
                try:
                    await broker.close()
                except Exception:  # noqa: BLE001 - no new billing effect or credential diagnostics
                    self.s.local_error = True

    async def run(self, argv, env):
        async def fence(**kwargs):
            if kwargs['hold']:
                loop.remove_reader(0)
                loop.remove_writer(self.owner.master_fd)
            await set_relay_input(self.env, **kwargs)
            if not kwargs['hold']:
                loop.add_reader(0, input_ready)
                if self.pending_input:
                    loop.add_writer(self.owner.master_fd, flush_input)

        self.owner = ClaudeRuntimeOwner(self.s.executable, argv, env, self.s.launch_spec.cwd,
            observer=self.observe, input_fence=fence, before_spawn=self.before_spawn, commit=self.commit)
        loop = asyncio.get_running_loop()
        attributes = termios.tcgetattr(0)
        previous_resize = signal.getsignal(signal.SIGWINCH)

        def resize(*_):
            self.owner.resize(*struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8))))

        def output():
            try:
                data = self.owner.read_output()
                if data:
                    from .terminal_relay import write_all
                    write_all(1, data)
            except (OSError, IntegrationError):
                self.failed = 'Native terminal output disconnected.'

        def flush_input():
            if self.owner.state != 'active':
                return
            try:
                sent = self.owner.write_input(bytes(self.pending_input))
                del self.pending_input[:sent]
            except BlockingIOError:
                return
            except (OSError, IntegrationError):
                self.failed = 'Native terminal input disconnected.'
                loop.remove_writer(self.owner.master_fd)
                loop.remove_reader(0)
                return
            if not self.pending_input:
                loop.remove_writer(self.owner.master_fd)
            if len(self.pending_input) < 1024 * 1024:
                loop.add_reader(0, input_ready)

        def input_ready():
            if self.owner.state != 'active':
                return
            space = 1024 * 1024 - len(self.pending_input)
            if space <= 0:
                loop.remove_reader(0)
                return
            try:
                data = os.read(0, min(65536, space))
            except OSError:
                self.failed = 'Native terminal input disconnected.'
                loop.remove_reader(0)
                return
            if not data:
                self.failed = 'Native terminal input disconnected.'
            else:
                self.pending_input.extend(data)
                if len(self.pending_input) >= 1024 * 1024:
                    loop.remove_reader(0)
                loop.add_writer(self.owner.master_fd, flush_input)

        natural_exit = False
        try:
            resize()
            signal.signal(signal.SIGWINCH, resize)
            tty.setraw(0, termios.TCSANOW)
            loop.add_reader(self.owner.master_fd, output)
            await self.owner.start()
            self.s.child = self.owner.process
            loop.add_reader(0, input_ready)
            await self.replace_wallet('host')
            while not self.failed:
                if self.owner.state == 'failed':
                    raise IntegrationError('Claude handoff could not preserve the input boundary.')
                if self.owner.state == 'active' and self.owner.process.returncode is not None:
                    natural_exit = self.owner.process.returncode == 0
                    return self.owner.process.returncode
                await asyncio.sleep(.03)
            raise IntegrationError(self.failed)
        finally:
            loop.remove_reader(0)
            loop.remove_writer(self.owner.master_fd)
            loop.remove_reader(self.owner.master_fd)
            try:
                if self.task and not self.task.done():
                    await asyncio.shield(self.task)
            finally:
                try:
                    await self.replace_wallet(None)
                finally:
                    try:
                        await self.owner.close(release_input=natural_exit)
                    finally:
                        try:
                            if self.grant and self.grant.state == 'issued':
                                await self.grant.close()
                        finally:
                            try:
                                await self.stack.aclose()
                            finally:
                                try:
                                    if self.broker:
                                        await self.broker.close()
                                finally:
                                    signal.signal(signal.SIGWINCH, previous_resize)
                                    termios.tcsetattr(0, termios.TCSANOW, attributes)
