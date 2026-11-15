"""Injected boundary tests run everywhere; TestNativeWindows requires Windows."""

import ctypes as C
import os
import subprocess
from dataclasses import replace

import pytest

from openvegas.emotes import _windows_resources as win
from openvegas.emotes.manifest import PackError, read_local
from openvegas.emotes.resources import PackRepository


class FakeAPI:
    def __init__(self, data=b"safe"):
        self.data, self.offset = data, 0
        self.opened, self.closed, self.calls, self.reads = [], [], [], []
        self.infos = {}
        self.fail_open = self.fail_info = self.fail_read = self.fail_close = None
        self.override = {}

    def opened_handle(self, parent, name, directory):
        handle = len(self.opened) + 1
        if handle == self.fail_open:
            raise OSError("injected open failure")
        self.opened.append(handle)
        self.calls.append((parent, name, directory))
        self.infos[handle] = win._Info(
            win.FILE_ATTRIBUTE_DIRECTORY if directory else 0,
            0 if directory else len(self.data),
            (1, 0, handle),
            (0, 0),
        )
        return handle

    def open_drive(self, drive):
        return self.opened_handle(None, drive, True)

    def open_child(self, parent, name, directory):
        assert parent in self.opened and parent not in self.closed
        assert "/" not in name and "\\" not in name
        return self.opened_handle(parent, name, directory)

    def info(self, handle):
        if handle == self.fail_info:
            raise OSError("injected handle query failure")
        info = self.infos[handle]
        return replace(info, **self.override.get(handle, {}))

    def read(self, handle, count):
        assert handle == self.opened[-1]
        self.reads.append(count)
        if self.fail_read:
            raise OSError("injected read failure")
        chunk = self.data[self.offset : self.offset + count]
        self.offset += len(chunk)
        return chunk

    def close(self, handle):
        self.closed.append(handle)
        if handle == self.fail_close:
            raise OSError("injected close failure")


@pytest.fixture
def fake(monkeypatch):
    api = FakeAPI()
    monkeypatch.setattr(win, "_WindowsAPI", lambda: api)
    return api


def read_fixture(limit=4):
    return win.read_windows(r"C:\packs\fixture", ["nested", "sheet.png"], limit)


def test_walk_retains_parents_and_reads_same_handle(fake):
    assert read_fixture() == b"safe"
    assert fake.calls == [
        (None, "C:\\", True),
        (1, "packs", True),
        (2, "fixture", True),
        (3, "nested", True),
        (4, "sheet.png", False),
    ]
    assert fake.closed == [5, 4, 3, 2, 1]


@pytest.mark.parametrize("stage", ["open", "info"])
@pytest.mark.parametrize("handle", range(1, 6))
def test_injected_open_and_query_failures_close_all(fake, stage, handle):
    setattr(fake, "fail_" + stage, handle)
    with pytest.raises(OSError):
        read_fixture()
    assert fake.closed == list(reversed(fake.opened))
    assert not fake.reads


@pytest.mark.parametrize("handle", range(1, 6))
def test_every_reparse_component_is_rejected_before_read(fake, handle):
    fake.override[handle] = {"attributes": win.FILE_ATTRIBUTE_REPARSE_POINT | 0x10}
    with pytest.raises(OSError, match="attributes"):
        read_fixture()
    assert fake.closed == list(reversed(fake.opened))
    assert not fake.reads


@pytest.mark.parametrize("handle,attributes", [(2, 0), (5, 0x10), (5, 0x40)])
def test_wrong_type_or_device_is_rejected(fake, handle, attributes):
    fake.override[handle] = {"attributes": attributes}
    with pytest.raises(OSError, match="attributes"):
        read_fixture()
    assert not fake.reads
    assert fake.closed == list(reversed(fake.opened))


def test_stat_oversize_never_reads(fake):
    with pytest.raises(OSError, match="size"):
        read_fixture(3)
    assert not fake.reads
    assert fake.closed == [5, 4, 3, 2, 1]


def test_growing_file_hits_limit_plus_one(fake):
    fake.data = b"x" * 100
    fake.override[5] = {"size": 4}
    with pytest.raises(OSError, match="changed|size"):
        read_fixture()
    assert fake.offset == 5
    assert fake.reads == [5]
    assert fake.closed == [5, 4, 3, 2, 1]


def test_truncated_file_fails(fake):
    fake.override[5] = {"size": 5}
    with pytest.raises(OSError, match="changed"):
        read_fixture(5)
    assert fake.closed == [5, 4, 3, 2, 1]


def test_short_reads_are_completed_in_bounded_chunks(fake, monkeypatch):
    fake.data = b"x" * 70000
    original = fake.read
    requested = []

    def short_read(handle, count):
        requested.append(count)
        return original(handle, min(count, 997))

    monkeypatch.setattr(fake, "read", short_read)
    assert read_fixture(70000) == fake.data
    assert max(requested) == 65536


def test_overlong_read_is_rejected(fake, monkeypatch):
    monkeypatch.setattr(fake, "read", lambda handle, count: b"x" * (count + 1))
    with pytest.raises(OSError, match="count"):
        read_fixture()
    assert fake.closed == [5, 4, 3, 2, 1]


def test_excessive_root_depth_fails_before_open(fake):
    with pytest.raises(OSError, match="depth"):
        win.read_windows("C:\\" + "p\\" * 257, ["sheet.png"], 4)
    assert not fake.opened


def test_read_failure_releases_handles(fake):
    fake.fail_read = True
    with pytest.raises(OSError, match="read"):
        read_fixture()
    assert fake.closed == [5, 4, 3, 2, 1]


def test_close_failure_does_not_leak_other_handles(fake):
    fake.fail_close = 5
    with pytest.raises(OSError, match="close"):
        read_fixture()
    assert fake.closed == [5, 4, 3, 2, 1]


@pytest.mark.parametrize(
    "change", [{"modified": (1, 0)}, {"attributes": 0x400}, {"identity": (2, 0, 1)}]
)
def test_postread_handle_mutation_fails(fake, monkeypatch, change):
    original = fake.read

    def mutate(handle, count):
        data = original(handle, count)
        fake.override[handle] = change
        return data

    monkeypatch.setattr(fake, "read", mutate)
    with pytest.raises(OSError):
        read_fixture()
    assert fake.closed == [5, 4, 3, 2, 1]


@pytest.mark.parametrize(
    "root",
    [
        r"\\server\share\pack",
        r"\\?\C:\pack",
        r"\\.\pipe\pack",
        r"C:pack",
        r"C:\pack\..\elsewhere",
        r"C:\pack.\fixture",
        r"C:\pack:stream",
    ],
)
def test_unsupported_roots_fail_before_open(fake, root):
    with pytest.raises(OSError):
        win.read_windows(root, ["sheet.png"], 4)
    assert not fake.opened


@pytest.mark.parametrize(
    "part", ["..", "a/b", "a\\b", "sheet.png:stream", "NUL.png", "COM1", "x.", "x ", "x\0"]
)
def test_component_aliases_fail_before_open(fake, part):
    with pytest.raises(OSError):
        win.read_windows(r"C:\packs", [part], 4)
    assert not fake.opened


def test_native_open_contract():
    api = win._WindowsAPI.__new__(win._WindowsAPI)

    def native_open(
        out, access, attributes, status, allocation, attrs, share, disposition, options, ea, ea_size
    ):
        value = C.cast(attributes, C.POINTER(win._ObjectAttributes)).contents
        assert value.RootDirectory == 123
        assert C.wstring_at(value.ObjectName.contents.Buffer) == "sheet.png"
        assert value.ObjectName.contents.Length == len("sheet.png") * 2
        assert value.Length == C.sizeof(win._ObjectAttributes)
        assert value.Attributes == win.OBJ_CASE_INSENSITIVE | win.OBJ_DONT_REPARSE
        assert not value.SecurityDescriptor and not value.SecurityQualityOfService
        assert access == win.FILE_READ_DATA | win.FILE_READ_ATTRIBUTES | win.SYNCHRONIZE
        assert share == win.FILE_SHARE_READ
        assert disposition == win.FILE_OPEN
        assert options == win.FILE_OPEN_REPARSE_POINT | win.FILE_SYNCHRONOUS_IO_NONALERT
        C.cast(out, C.POINTER(win.HANDLE)).contents.value = 456
        return 0

    api._open = native_open
    assert api.open_child(123, "sheet.png", False) == 456


def test_drive_open_contract():
    api = win._WindowsAPI.__new__(win._WindowsAPI)
    api._drive_type = lambda path: win.DRIVE_FIXED

    def create(path, access, share, security, disposition, flags, template):
        assert path == "\\\\?\\C:\\"
        assert access == win.FILE_TRAVERSE | win.FILE_READ_ATTRIBUTES | win.SYNCHRONIZE
        assert share == win.FILE_SHARE_READ
        assert disposition == 3 and security is None and template is None
        assert flags == win.FILE_FLAG_BACKUP_SEMANTICS | win.FILE_FLAG_OPEN_REPARSE_POINT
        return 123

    api._create = create
    assert api.open_drive("C:\\") == 123
    api._drive_type = lambda path: 4  # DRIVE_REMOTE
    with pytest.raises(OSError, match="local"):
        api.open_drive("C:\\")


@pytest.mark.parametrize("file_type", [0, 2, 3])
def test_native_non_disk_types_fail(file_type):
    api = win._WindowsAPI.__new__(win._WindowsAPI)
    api._type = lambda handle: file_type
    with pytest.raises(OSError, match="disk"):
        api.info(123)


def test_native_error_status_and_invalid_handle_fail():
    api = win._WindowsAPI.__new__(win._WindowsAPI)
    for status in (-1073741790, 0):  # access denied; success without a handle
        api._open = lambda *args, status=status: status
        with pytest.raises(OSError):
            api.open_child(123, "sheet.png", False)


def test_native_info_layout_size_and_failures():
    assert C.sizeof(win._FileInformation) == 52
    assert C.sizeof(win.DWORD) == 4
    assert C.sizeof(win._IOStatusBlock) == 2 * C.sizeof(win.HANDLE)
    api = win._WindowsAPI.__new__(win._WindowsAPI)
    api._type = lambda handle: win.FILE_TYPE_DISK

    def info(handle, pointer):
        value = C.cast(pointer, C.POINTER(win._FileInformation)).contents
        value.size_high, value.size_low = 1, 7
        value.volume, value.index_high, value.index_low = 3, 4, 5
        return 1

    api._info = info
    result = api.info(123)
    assert result.size == 2**32 + 7
    assert result.identity == (3, 4, 5)
    api._info = lambda *args: 0
    with pytest.raises(OSError, match="inspect"):
        api.info(123)


def test_native_read_failures_and_short_count():
    api = win._WindowsAPI.__new__(win._WindowsAPI)
    api._read = lambda *args: 0
    with pytest.raises(OSError, match="read"):
        api.read(123, 4)

    def short_read(handle, buffer, count, result, overlapped):
        C.memmove(buffer, b"ok", 2)
        C.cast(result, C.POINTER(win.DWORD)).contents.value = 2
        return 1

    api._read = short_read
    assert api.read(123, 4) == b"ok"

    def overlong(handle, buffer, count, result, overlapped):
        C.cast(result, C.POINTER(win.DWORD)).contents.value = count + 1
        return 1

    api._read = overlong
    with pytest.raises(OSError, match="count"):
        api.read(123, 4)


def test_api_unavailable_fails_closed(fake, monkeypatch):
    def unavailable():
        raise OSError("API unavailable")

    monkeypatch.setattr(win, "_WindowsAPI", unavailable)
    with pytest.raises(OSError, match="unavailable"):
        read_fixture()
    assert not fake.opened


@pytest.mark.skipif(os.name != "nt", reason="Native Windows security gate; not exercised on POSIX")
class TestNativeWindows:
    def test_fixture_and_package_resource_path(self, pack_dir):
        assert read_local(pack_dir, "manifest.json", 65536).startswith(b"{")
        repo = PackRepository()
        assert repo.names()
        for name in repo.names():
            assert repo.load(name).manifest.pack_id

    def test_unicode_nested_and_size(self, tmp_path):
        root = tmp_path / "unicode-\u03bb-\U0001f642"
        (root / "nested").mkdir(parents=True)
        (root / "nested" / "sheet.png").write_bytes(b"safe")
        assert read_local(root, "nested/sheet.png", 4) == b"safe"
        with pytest.raises(PackError):
            read_local(root, "nested/sheet.png", 3)
        with pytest.raises(PackError):
            read_local(root, "nested", 4)

    @pytest.mark.parametrize("position", ["root", "ancestor", "nested"])
    def test_junction_rejected(self, tmp_path, position):
        target = tmp_path / "target"
        (target / "pack").mkdir(parents=True)
        (target / "sheet.png").write_bytes(b"outside")
        (target / "pack" / "sheet.png").write_bytes(b"outside")
        junction = tmp_path / "junction"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        try:
            root, relative = {
                "root": (junction, "sheet.png"),
                "ancestor": (junction / "pack", "sheet.png"),
                "nested": (tmp_path, "junction/sheet.png"),
            }[position]
            with pytest.raises(PackError):
                read_local(root, relative, 100)
        finally:
            junction.rmdir()

    @pytest.mark.parametrize("position", ["root", "nested", "file"])
    def test_retained_handle_blocks_replacement(self, tmp_path, monkeypatch, position):
        root = tmp_path / "pack"
        nested = root / "nested"
        nested.mkdir(parents=True)
        path = nested / "sheet.png"
        path.write_bytes(b"safe")
        target = {"root": root, "nested": nested, "file": path}[position]
        original = win._WindowsAPI.info
        attempted = []

        def info(api, handle):
            value = original(api, handle)
            if not value.attributes & win.FILE_ATTRIBUTE_DIRECTORY and not attempted:
                with pytest.raises(OSError):
                    target.rename(target.with_name(target.name + ".moved"))
                with pytest.raises(OSError):
                    path.write_bytes(b"evil")
                attempted.append(True)
            return value

        monkeypatch.setattr(win._WindowsAPI, "info", info)
        assert read_local(root, "nested/sheet.png", 4) == b"safe"
        assert attempted == [True]
        # Handles really were released, including ancestors.
        target.rename(target.with_name(target.name + ".moved"))

    def test_existing_writer_fails_closed(self, tmp_path):
        path = tmp_path / "sheet.png"
        path.write_bytes(b"safe")
        with path.open("r+b"), pytest.raises(PackError):
            read_local(tmp_path, "sheet.png", 4)

    def test_swap_before_open_to_junction_fails(self, tmp_path, monkeypatch):
        root, outside = tmp_path / "pack", tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        nested = root / "nested"
        nested.mkdir()
        (nested / "sheet.png").write_bytes(b"safe")
        (outside / "sheet.png").write_bytes(b"evil")
        original = win._WindowsAPI.open_child

        def swap(api, parent, name, directory):
            if name == "nested":
                nested.rename(root / "moved")
                result = subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(nested), str(outside)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                assert result.returncode == 0, result.stderr
            return original(api, parent, name, directory)

        monkeypatch.setattr(win._WindowsAPI, "open_child", swap)
        try:
            with pytest.raises(PackError):
                read_local(root, "nested/sheet.png", 4)
        finally:
            if (root / "moved").exists():
                nested.rmdir()
