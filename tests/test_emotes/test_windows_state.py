"""Portable failure injections, not native Windows certification."""

import ctypes as C
import struct
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from openvegas.emotes import _windows_resources as resource
from openvegas.emotes import _windows_state as win
from openvegas.emotes import spool
from openvegas.emotes.events import Event, Phase
from openvegas.emotes.selection import SelectionStore

USER = "S-1-5-21-1-2-3-1000"
SECURITY = win.Security(USER, True, ((0, 0, win.FULL_CONTROL, USER),))


class FakeAPI:
    user = USER

    def __init__(self):
        self.nodes = {(): self.node(True)}
        self.handles, self.closed, self.calls = {}, [], []
        self.positions, self.locks = {}, set()
        self.next = 0
        self.disposed = False
        self.fail = None
        self.short_write = False
        self.on_info = None

    def node(self, directory=False, data=b""):
        return {
            "data": data,
            "directory": directory,
            "security": SECURITY,
            "attrs": 0x10 if directory else 0,
            "links": 1,
            "version": time.time_ns(),
        }

    def handle(self, key):
        self.next += 1
        self.handles[self.next] = (key, self.nodes[key])
        self.positions[self.next] = 0
        return self.next

    def open_drive(self, name):
        self.calls.append((None, name))
        return self.handle(())

    def child(self, parent, name, **kw):
        resource._component(name)
        assert parent not in self.closed
        self.calls.append((parent, name, kw))
        if self.fail == name:
            raise OSError("injected open")
        key = (*self.handles[parent][0], name)
        if key not in self.nodes:
            if kw.get("disposition", 1) == 1:
                raise FileNotFoundError
            self.nodes[key] = self.node(kw.get("directory", False))
        elif kw.get("disposition") == win.FILE_CREATE:
            raise FileExistsError
        return self.handle(key)

    def info(self, handle):
        node = self.handles[handle][1]
        return resource._Info(
            node["attrs"], len(node["data"]), (1, 0, id(node)), (0, node["version"])
        )

    def private_info(self, handle, *, directory=False):
        if self.on_info:
            self.on_info(handle)
        node = self.handles[handle][1]
        result = win.PrivateInfo(
            node["attrs"],
            len(node["data"]),
            1,
            id(node),
            node["version"],
            node["links"],
            node["security"],
            self.user,
        )
        win.validate_info(result, directory=directory)
        return result

    def read(self, handle, count):
        node = self.handles[handle][1]
        offset = self.positions[handle]
        data = node["data"][offset : offset + count]
        self.positions[handle] += len(data)
        return data

    def write(self, handle, data):
        node = self.handles[handle][1]
        node["data"] = data[:-1] if self.short_write else data
        node["version"] += 1

    def rename(self, handle, parent, name):
        if self.fail == "rename":
            raise OSError("injected rename")
        old, node = self.handles[handle]
        key = (*self.handles[parent][0], name)
        self.nodes[key] = node
        del self.nodes[old]
        self.handles[handle] = (key, node)

    def delete(self, handle):
        key, node = self.handles[handle]
        assert self.nodes[key] is node
        del self.nodes[key]

    def names(self, handle, limit):
        parent = self.handles[handle][0]
        names = [
            key[-1] for key in self.nodes if len(key) == len(parent) + 1 and key[:-1] == parent
        ]
        if len(names) > limit:
            raise OSError("scan limit")
        return names

    def lock(self, handle):
        key = self.handles[handle][0]
        if key in self.locks:
            raise BlockingIOError
        self.locks.add(key)
        return key

    def unlock(self, handle, token):
        self.locks.remove(token)

    def close(self, handle):
        assert handle not in self.closed
        self.closed.append(handle)

    def dispose(self):
        self.disposed = True


@pytest.fixture
def api(monkeypatch):
    instance = FakeAPI()
    monkeypatch.setattr(win, "WindowsStateAPI", lambda: instance)
    return instance


@pytest.fixture
def routed(api, monkeypatch):
    @contextmanager
    def directory(path):
        with win.private_windows_directory("C:\\" + path.name) as opened:
            yield opened

    monkeypatch.setattr(spool, "private_directory", directory)
    # Selection imported the helper directly; keep both callers on the same backend.
    monkeypatch.setattr("openvegas.emotes.selection.private_directory", directory)
    return api


@pytest.mark.parametrize(
    "security",
    [
        replace(SECURITY, owner="S-1-1-0"),
        replace(SECURITY, protected=False),
        replace(SECURITY, entries=()),
        replace(SECURITY, entries=((0, 0, win.FULL_CONTROL, "S-1-1-0"),)),
        replace(SECURITY, entries=((0, 0x10, win.FULL_CONTROL, USER),)),
        replace(SECURITY, entries=((0, 1, win.FULL_CONTROL, USER),)),
        replace(SECURITY, entries=((1, 0, win.FULL_CONTROL, USER),)),
        replace(SECURITY, entries=((5, 0, win.FULL_CONTROL, USER),)),
        replace(SECURITY, entries=((0, 0, 1, USER),)),
        replace(SECURITY, entries=SECURITY.entries * 65),
    ],
)
def test_unsafe_acl_fail_closed(security):
    with pytest.raises(OSError):
        win.validate_security(security, USER)


def test_owner_system_admin_acl_allowed():
    entries = SECURITY.entries + tuple(
        (0, 0, win.FULL_CONTROL, sid) for sid in win.TRUSTED_SERVICE_SIDS
    )
    win.validate_security(replace(SECURITY, entries=entries), USER)


@pytest.mark.parametrize(
    "override", [{"attributes": 0x400}, {"attributes": 0x40}, {"attributes": 0x10}, {"st_nlink": 2}]
)
def test_unsafe_handle_type_or_links(override):
    info = win.PrivateInfo(0, 0, 1, 2, 0, 1, SECURITY, USER)
    with pytest.raises(OSError):
        win.validate_info(replace(info, **override), directory=False)


def test_private_walk_and_close_order(api):
    with win.private_windows_directory(r"C:\new\private") as directory:
        assert directory.handle == api.next
        assert not api.closed
        assert ("new", "private") in api.nodes
    assert api.closed == list(reversed(api.handles))
    assert api.disposed


@pytest.mark.parametrize("location", [(), ("state",), ("state", "nested")])
def test_reparse_at_every_component_rejected(api, location):
    api.nodes[("state",)] = api.node(True)
    api.nodes[("state", "nested")] = api.node(True)
    api.nodes[location]["attrs"] |= 0x400
    with pytest.raises(OSError), win.private_windows_directory(r"C:\state\nested"):
        pytest.fail("unsafe root opened")
    assert api.closed == list(reversed(api.handles))


def test_existing_directory_never_repaired(api):
    api.nodes[("state",)] = api.node(True)
    api.nodes[("state",)]["security"] = replace(SECURITY, protected=False)
    with pytest.raises(OSError), win.private_windows_directory(r"C:\state"):
        pytest.fail("unsafe root opened")
    assert api.nodes[("state",)]["security"].protected is False


def test_atomic_state_read_revision_delete(api):
    with win.private_windows_directory(r"C:\state") as directory:
        directory.atomic_write("a.json", b"first")
        first = directory.stat("a.json")
        directory.atomic_write("a.json", b"second")
        assert directory.stat("a.json").st_ino != first.st_ino
        assert directory.read("a.json", 6) == b"second"
        with pytest.raises(OSError):
            directory.unlink("a.json", expected=first)
        directory.unlink("a.json", expected=directory.stat("a.json"))
        assert directory.names(1) == []


@pytest.mark.parametrize("failure", ["rename", "short"])
def test_failed_write_removes_only_owned_temporary(api, failure):
    with win.private_windows_directory(r"C:\state") as directory:
        directory.atomic_write("a.json", b"old")
        api.fail, api.short_write = failure, failure == "short"
        with pytest.raises(OSError):
            directory.atomic_write("a.json", b"new")
        assert directory.read("a.json", 3) == b"old"
        assert directory.names(2) == ["a.json"]


@pytest.mark.parametrize("corruption", ["hardlink", "acl", "reparse"])
def test_unsafe_existing_file_neither_read_replaced_nor_deleted(api, corruption):
    with win.private_windows_directory(r"C:\state") as directory:
        directory.atomic_write("a.json", b"old")
        node = api.nodes[("state", "a.json")]
        if corruption == "hardlink":
            node["links"] = 2
        elif corruption == "acl":
            node["security"] = replace(SECURITY, protected=False)
        else:
            node["attrs"] = 0x400
        for operation in (
            lambda: directory.read("a.json", 10),
            lambda: directory.atomic_write("a.json", b"new"),
            lambda: directory.unlink("a.json"),
        ):
            with pytest.raises(OSError):
                operation()
        assert node["data"] == b"old"


def test_read_limit_and_mutation_detection(api):
    with win.private_windows_directory(r"C:\state") as directory:
        directory.atomic_write("a.json", b"old")
        with pytest.raises(OSError, match="size"):
            directory.read("a.json", 2)
        count = 0

        def mutate(handle):
            nonlocal count
            count += 1
            if count == 3:
                api.handles[handle][1]["version"] += 1

        api.on_info = mutate
        with pytest.raises(OSError, match="changed"):
            directory.read("a.json", 3)


@pytest.mark.parametrize("name", ["..", "a/b", "a\\b", "a:stream", "CON", "a.", "a ", "*"])
def test_writes_reject_unsafe_names_before_open(api, name):
    with win.private_windows_directory(r"C:\state") as directory:
        before = len(api.calls)
        with pytest.raises(OSError):
            directory.atomic_write(name, b"x")
        assert len(api.calls) == before


def test_lock_nonblocking_release_even_on_exception(api):
    with win.private_windows_directory(r"C:\state") as directory:
        with pytest.raises(ValueError), directory.locked():
            with pytest.raises(BlockingIOError), directory.locked():
                pytest.fail("lock should not wait or acquire")
            raise ValueError("test")
        assert not api.locks
        with directory.locked():
            assert api.locks


def test_selection_slots_cas_and_busy_on_windows_abstraction(routed, tmp_path):
    selection = SelectionStore(tmp_path / "state")
    assert selection.revision() is None
    selection.write_slots({"companion": "fixture.pack", "completion": "fixture.finish"})
    slots, revision = selection.snapshot()
    selection.disable()
    assert selection.compare_and_write_slots(slots, expected_revision=revision) is None
    assert selection.read_slots() == {"companion": None, "completion": None}
    with spool._locked(selection.directory), pytest.raises(BlockingIOError):
        selection.write("fixture.pack")


def test_spool_roundtrip_filter_and_runner_counter(routed, tmp_path):
    from openvegas.emotes.runner import reserve_generation

    queue = spool.EventSpool(tmp_path / "events")
    start = Event("test", "session", "turn", "e0", Phase.START, 0, 0)
    stop = Event("test", "session", "turn", "e1", Phase.COMPLETE, 0, 1, "success")
    assert queue.publish(start) and queue.publish(stop)
    assert queue.drain(source="test", session_id="other") == []
    assert queue.drain(source="test", session_id="session") == [start, stop]
    assert queue.drain(source="test", session_id="session") == []
    assert reserve_generation(queue, source="test", session_id="session") == 1
    assert reserve_generation(queue, source="test", session_id="session") == 2
    with spool._locked(queue.directory):
        assert not queue.publish(start)


def test_helpers_dispatch_without_posix_syscalls(api, monkeypatch):
    with win.private_windows_directory(r"C:\state") as directory:
        monkeypatch.setattr(spool, "os", SimpleNamespace())
        spool.atomic_write(directory, "x.json", b"x")
        assert spool._read(directory, "x.json", 1) == b"x"
        assert spool.state_names(directory) == ["x.json"]
        info = spool.state_stat(directory, "x.json")
        spool._private(info, directory=False)
        spool.state_unlink(directory, "x.json", expected=info)


def test_lock_abi_has_fail_immediately_and_exclusive_flags():
    api = object.__new__(win.WindowsStateAPI)
    calls = []
    api._lock = lambda *args: calls.append(args) or 1
    token = api.lock(42)
    assert calls[0][:5] == (42, 3, 0, 1, 0)
    assert token.Offset == token.OffsetHigh == 0 and not token.hEvent
    assert C.sizeof(win._Overlapped) == (32 if C.sizeof(C.c_void_p) == 8 else 20)


@pytest.mark.parametrize("name", ["state.json", "state-\u00e9.json", "state-\U0001f600.json"])
def test_rename_abi_is_source_handle_relative_exact_utf16(name):
    api = object.__new__(win.WindowsStateAPI)
    calls = []

    def apply(handle, io, buffer, size, kind):
        info = win._Rename.from_buffer(buffer)
        data = C.string_at(C.addressof(buffer) + win._Rename.FileName.offset, info.FileNameLength)
        assert size >= C.sizeof(win._Rename) + len(data)
        assert C.cast(io, C.POINTER(resource._IOStatusBlock)).contents.Result.Status == 0
        calls.append((handle, kind, info.Flags, info.RootDirectory, data))
        return 0

    api._set_native = apply
    api.rename(3, 2, name)
    assert calls == [(3, 10, 1, None, name.encode("utf-16-le"))]


@pytest.mark.parametrize("status,error", [(-1073741790, 5), (-1073741811, 87), (259, 997)])
def test_rename_native_failure_or_pending_is_not_success(status, error):
    api = object.__new__(win.WindowsStateAPI)
    api._set_native = lambda *args: status
    api._dos_error = lambda actual: error if actual == status else pytest.fail("wrong status")
    with pytest.raises(OSError) as caught:
        api.rename(3, 2, "state.json")
    assert str(caught.value) == (
        "Cannot atomically publish private state "
        f"(NTSTATUS=0x{status & 0xFFFFFFFF:08X}, Win32={error})"
    )


@pytest.mark.parametrize("name", ["../target", "C:\\target", "a\\b", "a/b", "a:stream", ".."])
def test_rename_rejects_paths_before_native_call(name):
    api = object.__new__(win.WindowsStateAPI)
    api._set_native = lambda *args: pytest.fail("unsafe name reached kernel")
    with pytest.raises(OSError):
        api.rename(3, 2, name)


def test_open_abi_is_relative_private_nofollow_noninheritable():
    api = object.__new__(win.WindowsStateAPI)
    api.descriptor = C.c_void_p(123)

    def create(
        handle, access, attrs, io, allocation, attributes, share, disposition, options, ea, size
    ):
        attrs = C.cast(attrs, C.POINTER(resource._ObjectAttributes)).contents
        assert attrs.RootDirectory == 42 and attrs.SecurityDescriptor == 123
        assert attrs.Attributes == resource.OBJ_CASE_INSENSITIVE | resource.OBJ_DONT_REPARSE
        assert share == 5 and disposition == win.FILE_CREATE
        assert options & resource.FILE_OPEN_REPARSE_POINT
        assert options & win.FILE_NON_DIRECTORY_FILE
        assert access & win.READ_CONTROL and access & win.DELETE
        C.cast(handle, C.POINTER(resource.HANDLE))[0] = 99
        return 0

    api._open = create
    assert api.child(42, "new.json", writable=True, delete=True, disposition=win.FILE_CREATE) == 99


@pytest.mark.parametrize(
    "error,exception",
    [
        (2, FileNotFoundError),
        (3, FileNotFoundError),
        (32, BlockingIOError),
        (33, BlockingIOError),
        (5, OSError),
    ],
)
def test_status_errors_controlled(error, exception):
    api = object.__new__(win.WindowsStateAPI)
    api._dos_error = lambda status: error
    with pytest.raises(exception):
        api._raise_status(-1)


def test_directory_enumeration_abi_is_bounded_handle_relative():
    api = object.__new__(win.WindowsStateAPI)
    calls = []
    names = iter([".", "..", "one.json", None])

    def query(handle, event, apc, context, io, buffer, size, kind, single, pattern, restart):
        calls.append((handle, event, apc, context, kind, single, pattern, restart))
        name = next(names)
        if name is None:
            return -2147483642  # STATUS_NO_MORE_FILES
        encoded = name.encode("utf-16-le")
        data = struct.pack("<III", 0, 0, len(encoded)) + encoded
        C.memmove(buffer, data, len(data))
        C.cast(io, C.POINTER(resource._IOStatusBlock)).contents.Information = len(data)
        return 0

    api._query = query
    assert api.names(42, 1) == ["one.json"]
    assert calls[0] == (42, None, None, None, 12, 1, None, True)
    assert all(call[-1] is False for call in calls[1:])


@pytest.mark.parametrize(
    "length,following,size", [(3, 0, 20), (100, 0, 20), (2, 1, 20), (2, 0, 5000)]
)
def test_bad_native_directory_record_rejected(length, following, size):
    api = object.__new__(win.WindowsStateAPI)

    def query(handle, event, apc, context, io, buffer, capacity, kind, single, pattern, restart):
        C.memmove(buffer, struct.pack("<III", following, 0, length), 12)
        C.cast(io, C.POINTER(resource._IOStatusBlock)).contents.Information = size
        return 0

    api._query = query
    with pytest.raises(OSError):
        api.names(42, 1)


def test_delete_api_uses_opened_handle_not_path():
    api = object.__new__(win.WindowsStateAPI)
    calls = []

    def remove(handle, kind, flag, length):
        calls.append((handle, kind, C.cast(flag, C.POINTER(C.c_ubyte))[0], length))
        return 1

    api._set = remove
    api.delete(42)
    assert calls == [(42, 4, 1, 1)]


def test_windows_dispatch_missing_api_is_not_posix_fallback(monkeypatch, tmp_path):
    from contextlib import contextmanager

    @contextmanager
    def unavailable(path):
        raise OSError("Windows API unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(spool, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(spool, "private_windows_directory", unavailable)
    with pytest.raises(OSError, match="Windows API unavailable"), spool._locked(tmp_path):
        pytest.fail("Unavailable Windows API accepted")


def cache_library(path, **kwargs):
    from openvegas.emotes.remote import RemoteLibrary

    # Cache I/O does not establish ownership or perform transport requests.
    return RemoteLibrary(
        SimpleNamespace(), path, SelectionStore(path.parent / "selection"), **kwargs
    )


def cache_name(index):
    return f"remote-{index:064x}.json"


def test_windows_cache_quota_replacement_and_identity_checked_eviction(routed, tmp_path):
    library = cache_library(tmp_path / "cache", cache_max_files=2, cache_max_bytes=20)
    library._cache_bundle(cache_name(1), b"a" * 5)
    library._cache_bundle(cache_name(2), b"b" * 5)
    library._cache_bundle(cache_name(3), b"c" * 5)
    assert ("cache", cache_name(1)) not in routed.nodes
    library._cache_bundle(cache_name(2), b"d" * 20)
    assert ("cache", cache_name(3)) not in routed.nodes
    with spool._locked(library.cache) as directory:
        assert spool._read(directory, cache_name(2), 20) == b"d" * 20
        old = spool.state_stat(directory, cache_name(2))
        spool.atomic_write(directory, cache_name(2), b"new")
        from openvegas.emotes.remote import RemoteError

        with pytest.raises(RemoteError, match="changed"):
            library._unlink_cached(directory, cache_name(2), old)
        assert spool._read(directory, cache_name(2), 3) == b"new"


def test_windows_cache_stale_cleanup_waits_for_full_inventory(routed, tmp_path):
    from openvegas.emotes.remote import RemoteError

    library = cache_library(tmp_path / "cache")
    name = "." + "a" * 32 + ".tmp"
    with spool._locked(library.cache) as directory:
        spool.atomic_write(directory, name, b"interrupted")
        spool.atomic_write(directory, "unrelated.txt", b"keep")
    routed.nodes[("cache", name)]["version"] -= 90_000_000_000
    with pytest.raises(RemoteError, match="Unexpected"):
        library._cache_bundle(cache_name(1), b"test")
    assert ("cache", name) in routed.nodes
    with spool._locked(library.cache) as directory:
        spool.state_unlink(directory, "unrelated.txt")
    library._cache_bundle(cache_name(1), b"test")
    assert ("cache", name) not in routed.nodes


@pytest.mark.parametrize("corruption", ["acl", "hardlink", "reparse"])
def test_windows_cache_unsafe_inventory_never_evicted(routed, tmp_path, corruption):
    library = cache_library(tmp_path / "cache", cache_max_files=1)
    library._cache_bundle(cache_name(1), b"keep")
    node = routed.nodes[("cache", cache_name(1))]
    if corruption == "acl":
        node["security"] = replace(SECURITY, protected=False)
    elif corruption == "hardlink":
        node["links"] = 2
    else:
        node["attrs"] = 0x400
    with pytest.raises(OSError):
        library._cache_bundle(cache_name(2), b"new")
    assert node["data"] == b"keep" and ("cache", cache_name(2)) not in routed.nodes


def test_windows_cache_scan_bound_and_busy_are_not_empty_success(routed, tmp_path):
    from openvegas.emotes.remote import RemoteError

    library = cache_library(tmp_path / "cache", cache_scan_limit=1)
    library._cache_bundle(cache_name(1), b"one")
    with pytest.raises(RemoteError, match="scan"):
        library._cache_bundle(cache_name(2), b"two")
    with spool._locked(library.cache), pytest.raises(BlockingIOError):
        library._cache_bundle(cache_name(2), b"two")


def test_windows_watch_cleanup_only_selected_session_and_cas(routed, tmp_path, monkeypatch):
    from openvegas.emotes import commands

    queue = spool.EventSpool(tmp_path / "events")
    own = Event("test", "session", "turn", "e0", Phase.START, 4, 0)
    other = Event("test", "other", "turn", "e1", Phase.START, 7, 0)
    assert queue.publish(own) and queue.publish(other)
    assert commands._watch_boundary(queue, "test", "session") == 4
    assert queue.drain(source="test", session_id="other") == [other]
    assert queue.publish(own)
    actual = commands.state_unlink

    def replace_before_delete(directory, name, *, expected):
        spool.atomic_write(directory, name, other.to_bytes())
        actual(directory, name, expected=expected)

    monkeypatch.setattr(commands, "state_unlink", replace_before_delete)
    with pytest.raises(OSError, match="changed"):
        commands._watch_boundary(queue, "test", "session")
    assert queue.drain(source="test", session_id="other") == [other]


def doctor_services(tmp_path):
    from openvegas.emotes.commands import EmoteServices

    return EmoteServices(
        SimpleNamespace(names=list),
        None,
        SelectionStore(tmp_path / "selection"),
        spool.EventSpool(tmp_path / "events"),
    )


def test_windows_doctor_probes_real_wrappers_and_keeps_state(routed, tmp_path, monkeypatch):
    from click.testing import CliRunner

    from openvegas.emotes import commands

    monkeypatch.setattr(commands, "os", SimpleNamespace(name="nt", environ={}))
    services = doctor_services(tmp_path)
    services.selection.write("fixture.pack")
    revision = services.selection.revision()
    event = Event("test", "session", "turn", "e0", Phase.START, 0, 0)
    assert services.spool.publish(event)
    result = CliRunner().invoke(commands.emote, ["doctor"], obj=services)
    assert result.exit_code == 0, result.output
    assert "Windows ACL/handle backend" in result.output
    assert result.output.count("local lock/read/write/delete probe passed") == 2
    assert "automatic hook setup unavailable" in result.output
    assert "do not certify native terminal UX" in result.output
    assert services.selection.revision() == revision
    assert services.selection.read() == "fixture.pack"
    assert services.spool.drain(source="test", session_id="session") == [event]
    assert not any(key[-1].endswith(".tmp") for key in routed.nodes if key)


@pytest.mark.parametrize("failure", ["busy", "unsafe"])
def test_windows_doctor_fails_without_leaking_errors(routed, tmp_path, monkeypatch, failure):
    from click.testing import CliRunner

    from openvegas.emotes import commands

    services = doctor_services(tmp_path)

    def unavailable(path):
        if failure == "busy":
            raise BlockingIOError("secret-path-and-error")
        raise OSError("secret-path-and-error")

    monkeypatch.setattr(commands, "_probe_private_state", unavailable)
    monkeypatch.setattr(commands, "os", SimpleNamespace(name="nt", environ={}))
    result = CliRunner().invoke(commands.emote, ["doctor"], obj=services)
    assert result.exit_code == 1
    assert "probe passed" not in result.output and "secret-path-and-error" not in result.output
    assert ("busy" if failure == "busy" else "unavailable or unsafe") in result.output
