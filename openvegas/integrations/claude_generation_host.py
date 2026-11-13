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

SWITCH_TIMEOUT = 3.5


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
        self.wallet_refresh = None
        self.wallet_mode = 'host'
        self.wallet_closed = False
        self.stack = AsyncExitStack()
        self.failed = None
        self.starts = 0
        self.pending_input = bytearray()
        self._switch_status = None
        self._deadline = None
        self._watchdog = None
        self._reservation = None
        self._committed = False

    @property
    def switch_status(self):
        return self._switch_status

    def _status(self, value):
        self._switch_status = value
        self.s.wallet_status_changed.set()

    def _stop_watchdog(self):
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def _expire(self, *, cancel=True):
        if self._committed or self._switch_status == 'timed_out':
            return
        self._stop_watchdog()
        self._status('timed_out')
        self.s.emit('Billing switch did not complete within 3.5s. No new payer was committed. '
                    'Input stays held until cleanup is verified. Waiting prompts were not sent; '
                    're-enter after switch.')
        # The clock guard is independent of cooperative cancellation. Never
        # cancel the task from its own guard or cancel cleanup a second time.
        if cancel:
            task = self.task if self.task and not self.task.done() else self._reservation
            if task and not task.done() and not task.cancelling():
                task.cancel()
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self._refuse_reservation())

    def _check_deadline(self):
        if self._committed or self._deadline is None:
            return
        if self._switch_status == 'timed_out' or asyncio.get_running_loop().time() >= self._deadline:
            self._expire(cancel=False)
            raise IntegrationError('Billing switch timed out; no new payer was committed.')

    async def _refuse(self):
        self.pending_input.clear()
        await self.owner.cancel_reserved()
        self.s.emit('Billing is unchanged. Waiting prompts were not sent; re-enter after switch.')

    async def _refuse_reservation(self):
        from .codex_billing_route import _settle

        try:
            if self._reservation is not None:
                try:
                    await _settle(self._reservation)
                except (Exception, asyncio.CancelledError):
                    pass
            await self._refuse()
        except Exception:  # noqa: BLE001 - an unconfirmed fence never authorizes release
            self.failed = 'Claude switch cleanup is unconfirmed; input remains held.'
            self.s.emit(self.failed)
        finally:
            self.target = None
            self._stop_watchdog()
            if self.switch_status != 'timed_out':
                self._status(None)

    def before_spawn(self, mode, generation, env):
        if self.starts:
            self.s.generation += 1
        self.starts += 1
        self.s.token = secrets.token_urlsafe(32)
        self.s.env[TOKEN] = env[TOKEN] = self.s.token

    async def observe(self, **kwargs):
        self.s.child = kwargs['process']
        result = await self.observer(**kwargs)
        if kwargs.get('action') == 'refuse':
            return result
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
        self.target = target
        self._committed = False
        self._status('pending')
        loop = asyncio.get_running_loop()
        self._deadline = loop.time() + SWITCH_TIMEOUT
        self._watchdog = loop.call_at(self._deadline, self._expire)
        self._reservation = asyncio.create_task(self.owner.reserve_change())
        try:
            # Unlike wait_for, this deadline does not await cancellation of a
            # reservation which may be slow or ignore cancellation entirely.
            done, _ = await asyncio.wait((self._reservation,), timeout=SWITCH_TIMEOUT)
            if not done:
                self._expire()
            self._check_deadline()
            await self._reservation
        except BaseException:
            self._stop_watchdog()
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self._refuse_reservation())
            if self.switch_status == 'timed_out':
                raise IntegrationError('Billing switch timed out; cleanup remains held.') from None
            raise
        return True

    async def preflight(self):
        broker = Broker()
        try:
            await self.s.v_options(broker)
        finally:
            await broker.close()

    def begin(self):
        if self.target is not None and (self.task is None or self.task.done()):
            if self.switch_status == 'timed_out':
                self.task = asyncio.create_task(self._refuse_reservation())
                return
            self.task = asyncio.create_task(self.change(self.target))

    async def cancel_arm(self):
        self._stop_watchdog()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._refuse_reservation())
        await asyncio.shield(self.task)

    async def replace_wallet(self, mode):
        from .terminal_wallet import CredentialReader, monitor

        if self.monitor:
            previous, self.monitor = self.monitor, None
            if not previous.cancelling():
                previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
        if (mode is None or self.wallet_closed or mode != self.wallet_mode
                or self.env.get('OPENVEGAS_LOCAL_CONTROLS') == '0'):
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
            if not self.monitor.cancelling():
                self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None
            self.s.local_error = True
            return False
        return True

    def refresh_wallet(self, mode):
        if self.wallet_closed:
            return
        self.wallet_mode = mode
        if self.monitor and not self.monitor.cancelling():
            self.monitor.cancel()
        if self.wallet_refresh is None or self.wallet_refresh.done():
            self.wallet_refresh = asyncio.create_task(self._refresh_wallet())

    async def _refresh_wallet(self):
        # One display owner coalesces rapid commits; no display I/O gates input.
        while not self.wallet_closed:
            mode = self.wallet_mode
            ready = False
            try:
                ready = await self.replace_wallet(mode)
            except Exception:  # noqa: BLE001 - Optional display errors cannot undo billing.
                self.s.local_error = True
            if mode == self.wallet_mode:
                if ready is not False and self._committed and self.switch_status == 'refreshing':
                    self._status(None)
                return

    async def stop_wallet(self):
        self.wallet_closed = True
        if self.monitor and not self.monitor.cancelling():
            self.monitor.cancel()
        if self.wallet_refresh:
            await asyncio.gather(self.wallet_refresh, return_exceptions=True)
        await self.replace_wallet(None)

    async def commit(self, target, snapshot):
        self._check_deadline()
        # No await between the last clock check, watchdog retirement and payer
        # update. Post-commit display/transport cleanup is not a failed switch.
        self._committed = True
        self._stop_watchdog()
        self.s.mode = target
        self.s.original_model = snapshot.model
        self.s.transition_state = 'MODE_COMMITTED'
        self._status('refreshing')
        self.refresh_wallet(target)

    async def change(self, target):
        from .codex_billing_route import _settle

        broker = None
        new_grant = None
        prepared = None
        old_admission_changed = False
        try:
            self._check_deadline()
            before = await self.owner.capture()
            self._check_deadline()
            if target == 'v':
                broker = Broker()
                new_grant = WalletGrant(broker)
                route = await new_grant.open(before.snapshot.model)
                self._check_deadline()
                prepared = await self.stack.enter_async_context(wallet_listener(WalletEpoch(route)))
            else:
                old_admission_changed = True
                self.wallet.gate.seal()
                await self.grant.close()
            self._check_deadline()
            await self.owner.change(target, wallet=prepared, check=self._check_deadline)
            self.grant, self.wallet = new_grant, prepared
            previous_broker, self.broker = self.broker, broker
            broker = None  # Keep the issuer alive until the grant is closed.
            if previous_broker:
                try:
                    await previous_broker.close()
                except Exception:  # noqa: BLE001 - transport cleanup cannot undo a committed payer
                    self.s.local_error = True
        except BaseException as exc:  # no credentials/native diagnostics in terminal
            if prepared is not None:
                prepared.gate.seal()
            if new_grant is not None and new_grant.state == 'issued':
                try:
                    await _settle(asyncio.create_task(new_grant.close()))
                except Exception:  # noqa: BLE001 - unknown grant close stays fenced
                    self.failed = 'Wallet closure is unconfirmed; input remains held.'
            if (self.failed is None and self.owner.state == 'reserved'
                    and not old_admission_changed and (new_grant is None or new_grant.released)):
                try:
                    await _settle(asyncio.create_task(self._refuse()))
                except Exception:  # noqa: BLE001 - ambiguous refusal stays fenced
                    self.failed = 'Claude switch cleanup is unconfirmed; input remains held.'
                    self.s.emit(self.failed)
                self.s.emit(str(exc) if isinstance(exc, IntegrationError)
                            else 'Wallet connection unavailable. Billing is unchanged.')
            else:
                self.failed = self.failed or 'Claude billing handoff could not be verified. Input remains held.'
                self.s.emit(self.failed)
            if not isinstance(exc, Exception):
                raise
        finally:
            self.target = None
            self._stop_watchdog()
            if self.switch_status not in ('timed_out', 'refreshing'):
                self._status(None)
            if broker:
                try:
                    await broker.close()
                except Exception:  # noqa: BLE001 - no new billing effect or credential diagnostics
                    self.s.local_error = True

    async def run(self, argv, env):
        def refuse_input():
            self.pending_input.clear()
            termios.tcflush(0, termios.TCIFLUSH)

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
            observer=self.observe, input_fence=fence, before_spawn=self.before_spawn,
            commit=self.commit, refuse_input=refuse_input)
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
            self.refresh_wallet('host')
            while not self.failed:
                if self.owner.state == 'failed':
                    raise IntegrationError('Claude handoff could not preserve the input boundary.')
                if self.owner.state == 'active' and self.owner.process.returncode is not None:
                    natural_exit = self.owner.process.returncode == 0
                    return self.owner.process.returncode
                await asyncio.sleep(.03)
            raise IntegrationError(self.failed)
        finally:
            self._stop_watchdog()
            loop.remove_reader(0)
            loop.remove_writer(self.owner.master_fd)
            loop.remove_reader(self.owner.master_fd)
            try:
                if self.task and not self.task.done():
                    await asyncio.shield(self.task)
            finally:
                try:
                    await self.stop_wallet()
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
