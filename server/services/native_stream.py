"""Finish metering an already-dispatched stream after its client disconnects.

Never submits a request or executes a tool. The existing gateway owns the
reservation, response-size bound, receipt validation and settlement.
"""
import asyncio
from contextlib import aclosing

from openvegas.gateway.inference import InferenceResult
from openvegas.gateway.native_wire import MAX_RESPONSE, TIMEOUT

_pending = set()
_admitted = set()
MAX_ACTIVE = 16
_END = object()


class Admission:
    def __init__(self):
        self.task = None
        _admitted.add(self)

    def attach(self, task):
        self.task = task
        task.add_done_callback(lambda _: _admitted.discard(self))

    def release(self):
        if self.task is None:
            _admitted.discard(self)


def admit():
    # Event-loop-local atomic admission, before any reservation or dispatch.
    return Admission() if len(_admitted) < MAX_ACTIVE else None


class NativeStream:
    def __init__(self, iterator, first, settle, error_frame, *, timeout=TIMEOUT):
        self.iterator, self.first, self.settle = iterator, first, settle
        self.error_frame, self.timeout = error_frame, timeout
        self.queue = asyncio.Queue()
        self.buffered_bytes = 0
        self.detached = False
        self.task = asyncio.create_task(self._produce())
        _pending.add(self.task)
        self.task.add_done_callback(_pending.discard)

    def _send(self, item):
        if not self.detached:
            size = len(item.encode('utf-8'))
            if self.buffered_bytes + size > MAX_RESPONSE * 2 or self.queue.qsize() >= 65536:
                raise BufferError('Native client did not consume its bounded stream')
            self.buffered_bytes += size
            self.queue.put_nowait(item)

    async def _produce(self):
        try:
            async with aclosing(self.iterator), asyncio.timeout(self.timeout):
                value = self.first
                while True:
                    if isinstance(value, InferenceResult):
                        await self.settle(value.v_cost)
                    else:
                        self._send(value)
                    try:
                        value = await anext(self.iterator)
                    except StopAsyncIteration:
                        break
        except asyncio.CancelledError:
            raise  # Shutdown retains the existing gateway's uncertain hold.
        except Exception:  # noqa: BLE001 - Detached task boundary; report only a static error.
            if not self.detached:
                self.buffered_bytes += len(self.error_frame.encode('utf-8'))
                self.queue.put_nowait(self.error_frame)  # No upstream diagnostics.
        finally:
            if not self.detached:
                self.queue.put_nowait(_END)

    async def output(self):
        try:
            while True:
                value = await self.queue.get()
                if value is _END:
                    break
                self.buffered_bytes -= len(value.encode('utf-8'))
                yield value
        finally:
            self.detached = True
            # Discard later frames while obtaining the final usage receipt.
            while not self.queue.empty():
                self.queue.get_nowait()
            self.buffered_bytes = 0


async def shutdown(*, grace=5):
    """Drain before closing DB/Redis; cancellation never invents a refund."""
    tasks = list(_pending)
    if not tasks:
        return
    _, unfinished = await asyncio.wait(tasks, timeout=grace)
    for task in unfinished:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
