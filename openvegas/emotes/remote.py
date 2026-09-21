"""Online, account-bound emote access. Cached data is never an entitlement.

Transport owns authentication and response-byte limits; it must never open a
Touch ID prompt during background refresh. Mutating async operations reject
overlap, including across event loops/threads. authorize() performs no network.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
import re
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from openvegas.store.catalog import COSMETIC_SLOTS

from .manifest import (
    MAX_MANIFEST_BYTES,
    LoadedPack,
    PackError,
    decode_pack,
    parse_json,
    safe_token,
    validate_manifest,
)
from .resources import Catalog, CatalogEntry
from .spool import TEMP_FILE, _locked, _read, atomic_write, state_names, state_stat, state_unlink

MAX_PRIVATE_SHEET_BYTES = 2 * 1024 * 1024
MAX_BUNDLE_BYTES = 3 * 1024 * 1024
MAX_OWNED_BYTES = 512 * 1024
MAX_ENTITLEMENTS = 256
MAX_PACKS = 16
MAX_CACHE_FILES = 64
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_CACHE_SCAN = 256
CACHE_TEMP_STALE_SECONDS = 60.0
CACHE_FILE = re.compile(r"remote-[0-9a-f]{64}\.json\Z")
LEASE_SECONDS = 30.0
SLOTS = frozenset({"companion", "completion"})


class RemoteError(PackError):
    """Safe user-facing failure; never includes credentials or upstream bodies."""


def _json_bytes(value, limit):
    try:
        data = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise RemoteError("Invalid emote service response") from exc
    if not 0 < len(data) <= limit:
        raise RemoteError("Emote service response exceeds size limit")
    return data


def _scope(raw):
    if not isinstance(raw, str) or not 1 <= len(raw) <= 2048:
        raise RemoteError("Emote backend identity unavailable")
    try:
        url = urlsplit(raw)
        port = url.port
    except ValueError as exc:
        raise RemoteError("Invalid emote backend identity") from exc
    if (
        not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or url.scheme not in {"https", "http"}
        or (url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"})
        or any(ord(c) < 33 for c in raw)
    ):
        raise RemoteError("Invalid emote backend identity")
    host = url.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != {"http": 80, "https": 443}[url.scheme]:
        host += f":{port}"
    return urlunsplit((url.scheme, host, url.path.rstrip("/"), "", ""))


def _account(raw):
    try:
        if not isinstance(raw, str):
            raise TypeError
        account = UUID(raw)
        if account.int == 0:
            raise ValueError
        return str(account)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RemoteError("Sign in before syncing emotes") from exc


@dataclass(frozen=True)
class _Entitlement:
    item_id: str
    pack_id: str
    version: str
    slot: str


@dataclass(frozen=True)
class _Resource:
    entitlement: _Entitlement
    name: str
    loaded: LoadedPack


class RemoteLibrary:
    """Async transport contract: owned(), pack(item_id), equip(item_id, slot=...).

    get_identity() returns (backend_url, current_user_id); by default use
    api.identity. api.backend_scope must agree with that URL. All calls are
    online. sync/equip fetch private pack bytes from the service before using a
    disk cache; refresh renews only already verified in-memory resources.
    """

    def __init__(
        self,
        api,
        cache,
        selection,
        *,
        get_identity=None,
        clock=time.monotonic,
        request_timeout=10.0,
        operation_timeout=30.0,
        cache_max_files=MAX_CACHE_FILES,
        cache_max_bytes=MAX_CACHE_BYTES,
        cache_scan_limit=MAX_CACHE_SCAN,
    ):
        self.api = api
        self.cache = Path(cache)
        self.selection = selection
        self.get_identity = (
            get_identity or getattr(api, "identity", None) or getattr(api, "get_identity", None)
        )
        self.clock = clock
        self.request_timeout = self._timeout(request_timeout)
        self.operation_timeout = self._timeout(operation_timeout)
        self.cache_max_files = self._limit(cache_max_files, MAX_CACHE_FILES)
        self.cache_max_bytes = self._limit(cache_max_bytes, MAX_CACHE_BYTES)
        self.cache_scan_limit = self._limit(cache_scan_limit, MAX_CACHE_SCAN)
        self._state_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._epoch = 0
        self._operation_epoch = None
        self._closed = False
        self._managed_selection = None
        self._managed_slots = None
        self._managed_revision = None
        self._identity = None
        self._expires = 0.0
        self._issued = 0.0
        self._resources = {}
        self._entitlements = {}
        self._server_selected = None
        self._server_slots = None

    @staticmethod
    def _limit(value, ceiling):
        if type(value) is not int:
            raise TypeError("Emote cache limit must be an integer")
        if not 0 < value <= ceiling:
            raise ValueError("Emote cache limit exceeds allowed bounds")
        return value

    @staticmethod
    def _timeout(value):
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= 30:
            raise ValueError("Emote timeout must be between zero and 30 seconds")
        return float(value)

    def _current_identity(self):
        try:
            raw = self.get_identity() if self.get_identity else None
            if not isinstance(raw, (tuple, list)) or len(raw) != 2:
                raise RemoteError("Emote account identity unavailable")
            identity = (_scope(raw[0]), _account(raw[1]))
            if identity[0] != _scope(self.api.backend_scope):
                raise RemoteError("Emote backend identity changed")
            return identity
        except RemoteError:
            raise
        except PackError as exc:
            raise RemoteError("Session missing or expired. Run: openvegas login") from exc
        except Exception as exc:
            raise RemoteError("Emote account identity unavailable") from exc

    def _clear_selection(self):
        try:
            if (
                self._managed_revision is not None
                and self.selection.revision() == self._managed_revision
                and self.selection.read_slots() == self._managed_slots
                and self.selection.revision() == self._managed_revision
            ):
                self.selection.compare_and_write_slots(
                    {"companion": None, "completion": None}, expected_revision=self._managed_revision
                )
        except (OSError, ValueError):
            # Authorization is already gone even when local preferences cannot be written.
            pass

    def invalidate(self):
        """Explicit logout/error hook. Also called automatically on identity/lease changes."""
        with self._state_lock:
            self._epoch += 1
            self._identity = None
            self._expires = self._issued = 0.0
            self._resources = {}
            self._entitlements = {}
            self._server_selected = None
            self._server_slots = None
            if not self._closed:
                self._clear_selection()
            self._managed_selection = self._managed_revision = None
            self._managed_slots = None

    def close(self):
        """Retire permanently without changing this account's saved preference."""
        with self._state_lock:
            self._closed = True
            self.invalidate()

    def _fresh(self):
        with self._state_lock:
            if self._closed or self._identity is None:
                return False
            try:
                now = self.clock()
                valid = (
                    self._identity is not None
                    and self._identity == self._current_identity()
                    and math.isfinite(now)
                    and self._issued <= now < self._expires
                )
            except Exception:  # noqa: BLE001 - clock/identity errors must revoke access
                valid = False
            if not valid:
                self.invalidate()
            return valid

    def authorize(self, pack_id):
        """Cheap synchronous, no network, no authentication UI, no disk asset loads."""
        with self._state_lock:
            return self._fresh() and pack_id in self._resources

    @asynccontextmanager
    async def _operation(self):
        if not self._operation_lock.acquire(blocking=False):
            raise RemoteError("Another emote sync is in progress")
        try:
            if self._closed:
                raise RemoteError("Emote library is closed")
            identity = self._current_identity()
            with self._state_lock:
                if self._identity != identity:
                    self.invalidate()
                self._operation_epoch = self._epoch
                self._operation_selection_revision = self.selection.revision()
            async with asyncio.timeout(self.operation_timeout):
                yield identity
        except asyncio.CancelledError:
            self.invalidate()
            raise
        except RemoteError:
            self.invalidate()
            raise
        except PackError as exc:
            self.invalidate()
            if str(exc) == "Session missing or expired. Run: openvegas login":
                raise RemoteError(str(exc)) from exc
            raise RemoteError("Emote sync unavailable; no access was granted") from exc
        except Exception as exc:
            self.invalidate()
            raise RemoteError("Emote sync unavailable; sign in and retry online") from exc
        finally:
            self._operation_lock.release()

    def _check_identity(self, identity):
        if (
            self._closed
            or identity != self._current_identity()
            or self._epoch != self._operation_epoch
        ):
            raise RemoteError("Emote account changed during sync; retry")

    async def _call(self, method, identity, *args, **kwargs):
        self._check_identity(identity)
        result = await asyncio.wait_for(method(*args, **kwargs), self.request_timeout)
        self._check_identity(identity)
        return result

    async def _owned(self, identity):
        response = await self._call(self.api.owned, identity)
        _json_bytes(response, MAX_OWNED_BYTES)
        if not isinstance(response, dict) or _account(response.get("account_id")) != identity[1]:
            raise RemoteError("Emote ownership account mismatch")
        rows, equipped = response.get("entitlements"), response.get("equipped")
        if (
            not isinstance(rows, list)
            or len(rows) > MAX_ENTITLEMENTS
            or not isinstance(equipped, dict)
        ):
            raise RemoteError("Invalid emote ownership response")
        if equipped.keys() - COSMETIC_SLOTS:
            raise RemoteError("Invalid emote equipment slot")
        for item_id in equipped.values():
            if item_id is not None:
                safe_token(item_id)
        entries, items = {}, set()
        for row in rows:
            if not isinstance(row, dict):
                raise RemoteError("Invalid emote entitlement")
            item_id = safe_token(row.get("item_id"))
            if item_id in items:
                raise RemoteError("Duplicate emote entitlement")
            items.add(item_id)
            slot = row.get("slot")
            if not isinstance(slot, str) or slot not in COSMETIC_SLOTS:
                raise RemoteError("Invalid emote entitlement slot")
            if slot not in SLOTS:
                continue
            if row.get("effective_status") != "active" or row.get("activatable") is not True:
                continue
            pack_id = safe_token(row.get("pack_id"))
            version = safe_token(row.get("available_version"))
            if pack_id in entries:
                raise RemoteError("Ambiguous emote pack ownership")
            entries[pack_id] = _Entitlement(item_id, pack_id, version, slot)
        terminal_equipped = {slot: item_id for slot, item_id in equipped.items() if slot in SLOTS}
        return response, entries, terminal_equipped, self.clock()

    def _resource_name(self, identity, entry):
        parts = (*identity, entry.item_id, entry.pack_id, entry.version)
        return "remote-" + hashlib.sha256(_json_bytes(parts, 4096)).hexdigest()

    async def _download(self, identity, entry):
        bundle = await self._call(self.api.pack, identity, entry.item_id)
        if not isinstance(bundle, dict) or set(bundle) != {
            "schema_version",
            "item_id",
            "pack_id",
            "version",
            "manifest",
            "sheet_base64",
        }:
            raise RemoteError("Invalid emote pack bundle")
        if (
            type(bundle["schema_version"]) is not int
            or bundle["schema_version"] != 1
            or bundle["item_id"] != entry.item_id
            or bundle["pack_id"] != entry.pack_id
            or bundle["version"] != entry.version
        ):
            raise RemoteError("Emote pack identity mismatch")
        encoded = bundle["sheet_base64"]
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_PRIVATE_SHEET_BYTES + 2) // 3):
            raise RemoteError("Emote sheet exceeds size limit")
        manifest_bytes = _json_bytes(bundle["manifest"], MAX_MANIFEST_BYTES)
        manifest = validate_manifest(parse_json(manifest_bytes))
        if manifest.pack_id != entry.pack_id or manifest.version != entry.version:
            raise RemoteError("Emote manifest identity mismatch")
        try:
            sheet = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RemoteError("Invalid emote sheet encoding") from exc
        if len(sheet) > MAX_PRIVATE_SHEET_BYTES:
            raise RemoteError("Emote sheet exceeds size limit")
        loaded = decode_pack(manifest, sheet)
        data = _json_bytes(bundle, MAX_BUNDLE_BYTES)
        name = self._resource_name(identity, entry)
        # A cache hit is accepted only if byte-identical to this online response.
        # It cannot self-sign its own modified manifest/checksum into authorization.
        self._cache_bundle(name + ".json", data)
        return _Resource(entry, name, loaded)

    def _cache_bundle(self, name, data):
        if not CACHE_FILE.fullmatch(name) or len(data) > min(
            self.cache_max_bytes, MAX_BUNDLE_BYTES
        ):
            raise RemoteError("Emote bundle exceeds private cache limits")
        # The same private, nonblocking lock protects inventory, eviction and
        # atomic publication across library instances/processes. Busy fails closed.
        with _locked(self.cache) as fd:
            inventory = {}
            stale_temps = {}
            try:
                names = state_names(fd, limit=self.cache_scan_limit)
            except (OSError, ValueError) as exc:
                raise RemoteError("Emote cache exceeds bounded scan limit or cannot be read securely") from exc
            for entry_name in names:
                if entry_name == ".lock":
                    info = state_stat(fd, entry_name)
                    if info.st_size:
                        raise RemoteError("Invalid private emote cache lock file")
                    continue
                temporary = TEMP_FILE.fullmatch(entry_name) is not None
                if not CACHE_FILE.fullmatch(entry_name) and not temporary:
                    raise RemoteError("Unexpected file in private emote cache")
                info = state_stat(fd, entry_name)
                if info.st_size > MAX_BUNDLE_BYTES:
                    raise RemoteError("Emote cache file exceeds bundle limit")
                if temporary:
                    if time.time() - info.st_mtime < CACHE_TEMP_STALE_SECONDS:
                        raise RemoteError("Recent interrupted cache write; retry shortly")
                    stale_temps[entry_name] = info
                    continue
                inventory[entry_name] = info
            try:
                cached = _read(fd, name, MAX_BUNDLE_BYTES)
            except FileNotFoundError:
                cached = None
            # SIGKILL can leave atomic_write's UUID temp behind. Validate the
            # entire directory before cleanup; never delete unknown/fresh files.
            for temporary, info in stale_temps.items():
                self._unlink_cached(fd, temporary, info)
            count = len(inventory) + (name not in inventory)
            total = sum(info.st_size for info in inventory.values()) + len(data)
            if name in inventory:
                total -= inventory[name].st_size
            oldest = sorted(
                ((old_name, info) for old_name, info in inventory.items() if old_name != name),
                key=lambda pair: (pair[1].st_mtime_ns, pair[0]),
            )
            for old_name, old_info in oldest:
                if count <= self.cache_max_files and total <= self.cache_max_bytes:
                    break
                # Checked handle deletion on Windows; no-follow, directory-relative
                # identity check and unlink on POSIX. Never follow an external target.
                self._unlink_cached(fd, old_name, old_info)
                count -= 1
                total -= old_info.st_size
            if count > self.cache_max_files or total > self.cache_max_bytes:
                raise RemoteError("Private emote cache quota unavailable")
            if cached != data:
                atomic_write(fd, name, data)

    @staticmethod
    def _unlink_cached(fd, name, expected):
        try:
            state_unlink(fd, name, expected=expected)
        except (OSError, ValueError) as exc:
            raise RemoteError("Emote cache changed or became unsafe during eviction") from exc

    def _prepare_resources(self, entries, *, incoming=None, reset=False):
        """Release stale/evicted decoded frames before allocating another pack."""
        with self._state_lock:
            resources = (
                {}
                if reset
                else {
                    pack_id: resource
                    for pack_id, resource in self._resources.items()
                    if entries.get(pack_id) == resource.entitlement and pack_id != incoming
                }
            )
            limit = MAX_PACKS - (incoming is not None)
            while len(resources) > limit:
                victim = next(
                    (pack_id for pack_id in resources if pack_id != self._server_selected),
                    next(iter(resources)),
                )
                del resources[victim]
            self._resources = dict(resources)
            return resources

    def _commit(self, identity, owned, resources, *, restore=False):
        response, entries, equipped, received = owned
        self._check_identity(identity)
        now = self.clock()
        if (
            not math.isfinite(now)
            or not math.isfinite(received)
            or not received <= now < received + LEASE_SECONDS
        ):
            raise RemoteError("Emote ownership check expired; retry online")
        valid = {
            pack_id: resource
            for pack_id, resource in resources.items()
            if entries.get(pack_id) == resource.entitlement
        }
        selected_slots = {
            slot: next((pack_id for pack_id, resource in valid.items()
                        if resource.entitlement.slot == slot
                        and resource.entitlement.item_id == equipped.get(slot)), None)
            for slot in SLOTS
        }
        selected = selected_slots["companion"]
        with self._state_lock:
            self._check_identity(identity)
            self._identity, self._issued, self._expires = (
                identity,
                received,
                received + LEASE_SECONDS,
            )
            self._resources, self._entitlements = valid, entries
            previous_slots = self._server_slots
            self._server_selected = selected
            self._server_slots = selected_slots
            # A local off is deliberate. Background polls must not undo it unless
            # the server changes and this worker still owns the preference revision.
            # Merely observing another process's new selection never adopts it.
            expected = self._operation_selection_revision if restore else self._managed_revision
            wrote_revision = None
            if restore or (selected_slots != previous_slots and self._managed_revision is not None):
                wrote_revision = self.selection.compare_and_write_slots(
                    selected_slots, expected_revision=expected
                )
                if restore and wrote_revision is None:
                    raise RemoteError(
                        "Local emote selection changed during sync; retry if intended"
                    )
            actual_slots, _ = self.selection.snapshot()
            actual_selected = actual_slots["companion"]
            if wrote_revision is not None:
                self._managed_selection = selected
                self._managed_slots = selected_slots
                self._managed_revision = wrote_revision
        return {
            "owned": response,
            "available": sorted(valid),
            "needs_sync": sorted(set(entries) - valid.keys()),
            "selected": actual_selected,
            "selected_slots": actual_slots,
            "lease_seconds": LEASE_SECONDS,
        }

    async def sync(self):
        """Online restore, bounded to 16 active packs; never purchases or auto-equips."""
        async with self._operation() as identity:
            initial = await self._owned(identity)
            entries = initial[1]
            if len(entries) > MAX_PACKS:
                raise RemoteError("Too many emotes for one sync; contact support")
            resources = self._prepare_resources(entries, reset=True)
            for pack_id, entry in entries.items():
                resources[pack_id] = await self._download(identity, entry)
            # Recheck revocation, version and equipment after potentially slow downloads.
            final = await self._owned(identity)
            result = self._commit(identity, final, resources, restore=True)
            result["downloaded"] = sorted(resources)
            return result

    async def refresh(self):
        """Renew verified resources online; new versions need foreground sync()."""
        async with self._operation() as identity:
            owned = await self._owned(identity)
            resources = self._prepare_resources(owned[1])
            return self._commit(identity, owned, resources)

    async def equip(self, pack_id, *, slot="companion"):
        """Fresh ownership + pack download, server equip, then local preference."""
        if slot not in SLOTS:
            raise RemoteError("Invalid emote equipment slot")
        async with self._operation() as identity:
            initial = await self._owned(identity)
            entry = None
            if pack_id is not None:
                safe_token(pack_id)
                entry = initial[1].get(pack_id)
                if entry is None or entry.slot != slot:
                    raise RemoteError("Verified active ownership required for this emote")
            resources = self._prepare_resources(initial[1], incoming=pack_id)
            if entry is not None:
                resources[pack_id] = await self._download(identity, entry)
            # A fresh CLI instance has no decoded resources for the other slot.
            # Restore that equipped pack too, before the final ownership recheck.
            for other in initial[1].values():
                if other.slot == slot or initial[2].get(other.slot) != other.item_id:
                    continue
                if other.pack_id not in resources:
                    if len(resources) >= MAX_PACKS:
                        victim = next(key for key in resources if key != pack_id)
                        del resources[victim]
                        with self._state_lock:
                            self._resources.pop(victim, None)
                    resources[other.pack_id] = await self._download(identity, other)
            item_id = entry.item_id if entry else None
            response = await self._call(self.api.equip, identity, item_id, slot=slot)
            if (
                not isinstance(response, dict)
                or response.get("slot") != slot
                or response.get("item_id") != item_id
            ):
                raise RemoteError("Emote equipment response mismatch")
            final = await self._owned(identity)
            if final[2].get(slot) != item_id or (entry and final[1].get(pack_id) != entry):
                raise RemoteError("Emote equipment changed; sync again")
            for equipped_entry in final[1].values():
                if final[2].get(equipped_entry.slot) == equipped_entry.item_id:
                    resource = resources.get(equipped_entry.pack_id)
                    if resource is None or resource.entitlement != equipped_entry:
                        raise RemoteError("Emote equipment changed; sync again")
            result = self._commit(identity, final, resources, restore=True)
            result["equipped"] = response
            return result

    def catalog(self, base_catalog):
        return _RemoteCatalog(self, base_catalog)

    def repository(self, base_repository):
        return _RemoteRepository(self, base_repository)

    def _snapshot(self):
        with self._state_lock:
            return dict(self._resources) if self._fresh() else {}


class _RemoteCatalog(Catalog):
    def __init__(self, library, base):
        self.library, self.base = library, base
        self.authorize = library.authorize
        self._leased = set()

    @property
    def entries(self):
        entries = dict(self.base.entries)
        for pack_id, resource in self.library._snapshot().items():
            entries[pack_id] = CatalogEntry(
                pack_id,
                resource.name,
                resource.loaded.manifest.display_name,
                access="premium",
            )
        return entries

    def resource_for(self, pack_id, *, preview=False):
        with self.library._state_lock:
            if self.library.authorize(pack_id):
                self._leased.add(pack_id)
                return super().resource_for(pack_id, preview=preview)
            if pack_id in self._leased:
                # A consumer may still hold the private frames it loaded earlier.
                # A public fallback must not pass that consumer's lease check.
                raise RemoteError("Online emote ownership verification required")
            return self.base.resource_for(pack_id, preview=preview)


class _RemoteRepository:
    def __init__(self, library, base):
        self.library, self.base = library, base

    def names(self):
        return sorted(set(self.base.names()) | {r.name for r in self.library._snapshot().values()})

    def load(self, name):
        safe_token(name)
        if not name.startswith("remote-"):
            return self.base.load(name)
        for pack_id, resource in self.library._snapshot().items():
            if resource.name == name and self.library.authorize(pack_id):
                return resource.loaded
        raise RemoteError("Online emote ownership verification required")
