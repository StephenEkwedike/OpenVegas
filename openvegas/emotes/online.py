"""Foreground restore and a bounded background lease check for owned companions."""

from __future__ import annotations

import asyncio
import threading

from .spool import default_state_dir


def remote_library(selection):
    from .remote import RemoteLibrary
    from .transport import EmoteAPI

    api = EmoteAPI()
    return RemoteLibrary(
        api,
        default_state_dir() / "packs",
        selection,
        get_identity=api.identity,
        request_timeout=12.5,
    )


class LeaseRefresher:
    """One worker per companion. HTTP never runs in the renderer's frame loop."""

    def __init__(self, library, *, interval=5.0):
        self.library = library
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Lease refresher already started")
        self._thread = threading.Thread(target=self._run, name="emote-lease", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                asyncio.run(self.library.refresh())
            except Exception:  # noqa: BLE001 - cosmetics fail closed without crashing the host
                if not self._stop.is_set():
                    self.library.invalidate()
                return

    def close(self):
        self._stop.set()
        self.library.close()
        # A bounded in-flight request can finish in the daemon, but close retires
        # its epoch so it cannot restore access or change a later selection.
        if self._thread is not None:
            self._thread.join(timeout=0.1)
