"""Payer provenance for ephemeral title threads on an owned Codex connection."""
import asyncio
import json
from uuid import UUID

from .credentials import IntegrationError


def _need(ok):
    if not ok:
        raise IntegrationError("Native background payer could not be verified; nothing was sent.")


def _object(raw):
    def unique(pairs):
        result = dict(pairs)
        _need(len(result) == len(pairs))
        return result

    _need(isinstance(raw, str) and len(raw) <= 16384)
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, RecursionError):
        raise IntegrationError("Invalid native billing metadata.") from None
    _need(isinstance(value, dict))
    return value


def turn_scope(headers, outer=None):
    detail = [v for k, v in headers if k.lower() == "x-codex-turn-metadata"]
    _need(len(detail) <= 1)
    meta = _object(detail[0]) if detail else {}
    outer = outer or {}
    tid = meta.get("turn_id", outer.get("turn_id"))
    if tid is None:
        _need(outer.get("turn_id") is None)
        return None
    sid = meta.get("thread_id", outer.get("thread_id"))
    for name, value in (("thread_id", sid), ("turn_id", tid)):
        _need(isinstance(value, str) and len(value) == 36 and outer.get(name, value) == value)
        try:
            _need(str(UUID(value)) == value)
        except ValueError:
            raise IntegrationError("Invalid native turn identity.") from None
    return sid, tid


class TitleOrigins:
    def __init__(self, state):
        self.state = state
        self.starts = {}
        self.origins = {}
        self._changed = asyncio.Event()

    def stamp(self):
        return (self.state.mode, self.state.generation) if self.state.pending is None else None

    def start(self, key):
        _need(key not in self.starts and len(self.starts) < 256)
        self.starts[key] = self.stamp()

    def result(self, key, frame):
        if key not in self.starts:
            return
        stamp = self.starts.pop(key)
        self._changed.set()
        self._changed = asyncio.Event()
        thread = (frame.get("result") or {}).get("thread") or {}
        if thread.get("threadSource") != "thread_title":
            return
        sid = thread.get("id")
        _need(thread.get("ephemeral") is True and thread.get("parentThreadId") is None
              and isinstance(sid, str) and len(sid) == 36)
        try:
            _need(str(UUID(sid)) == sid)
        except ValueError:
            raise IntegrationError("Invalid native background thread identity.") from None
        _need(sid not in self.origins and len(self.origins) < 4096)
        # Keep retired entries so an old retry cannot become new work.
        self.origins[sid] = stamp if stamp == self.stamp() else None

    def claim(self, headers):
        direct = [v for k, v in headers if k.lower() == "thread-id"]
        detail = [v for k, v in headers if k.lower() == "x-codex-turn-metadata"]
        _need(len(direct) <= 1 and len(detail) <= 1)
        meta = _object(detail[0]) if detail else {}
        sid = meta.get("thread_id", direct[0] if direct else None)
        _need(sid is None or isinstance(sid, str))
        _need(not direct or direct == [sid])
        if meta.get("thread_source") == "thread_title" or sid in self.origins:
            sessions = [v for k, v in headers if k.lower() == "session-id"]
            _need(sid in self.origins and meta.get("thread_source") == "thread_title"
                  and meta.get("session_id", sid) == sid and (not sessions or sessions == [sid])
                  and self.origins[sid] is not None and self.origins[sid] == self.stamp())
            return sid
        return None

    async def admit(self, headers):
        # Native prewarm can race the local thread/start answer. Wait only for
        # an already-observed start; never take the request's ID as authority.
        detail = [v for k, v in headers if k.lower() == "x-codex-turn-metadata"]
        if len(detail) == 1 and self.starts:
            meta = _object(detail[0])
            sid, stamp = meta.get("thread_id"), self.stamp()
            if meta.get("thread_source") == "thread_title" and isinstance(sid, str) and stamp is not None:
                try:
                    async with asyncio.timeout(.25):
                        while (sid not in self.origins and self.stamp() == stamp
                               and stamp in self.starts.values()):
                            await self._changed.wait()
                except TimeoutError:
                    pass
                _need(self.stamp() == stamp)
        return self.claim(headers)

    def frame(self, value, handshake):
        meta = value.get("client_metadata", {})
        _need(isinstance(meta, dict))
        headers = [(k, meta[v]) for k, v in (("thread-id", "thread_id"), ("session-id", "session_id"),
                   ("x-codex-turn-metadata", "x-codex-turn-metadata")) if v in meta]
        _need(self.claim(headers) == handshake)
        return turn_scope(headers, meta) if value.get("generate") is not False else None
