"""Public read boundary and POSIX descriptor regression/failure injections."""

import os
from types import SimpleNamespace

import pytest

from openvegas.emotes import manifest


@pytest.mark.parametrize(
    "relative",
    [
        None,
        "",
        "../sheet.png",
        "/sheet.png",
        "a\\sheet.png",
        "a:stream",
        "a//b",
        "a/" * 8 + "b",
        "a" * 201,
    ],
)
def test_reject_unsafe_resource_paths(tmp_path, relative):
    with pytest.raises(manifest.PackError, match="path"):
        manifest.read_local(tmp_path, relative, 4)


@pytest.mark.parametrize("limit", [-1, True, 1.5, None])
def test_reject_invalid_limit(tmp_path, limit):
    with pytest.raises(manifest.PackError, match="limit"):
        manifest.read_local(tmp_path, "sheet.png", limit)


def test_missing_directory_and_bounded_regular_file(tmp_path):
    with pytest.raises(manifest.PackError):
        manifest.read_local(tmp_path, "missing.png", 4)
    (tmp_path / "dir.png").mkdir()
    with pytest.raises(manifest.PackError):
        manifest.read_local(tmp_path, "dir.png", 4)
    (tmp_path / "sheet.png").write_bytes(b"safe")
    assert manifest.read_local(tmp_path, "sheet.png", 4) == b"safe"
    with pytest.raises(manifest.PackError):
        manifest.read_local(tmp_path, "sheet.png", 3)
    (tmp_path / "empty.png").write_bytes(b"")
    assert manifest.read_local(tmp_path, "empty.png", 0) == b""


def test_windows_dispatch_failure_is_sanitized(tmp_path, monkeypatch):
    from openvegas.emotes import _windows_resources

    monkeypatch.setattr(manifest, "os", SimpleNamespace(name="nt"))

    def fail(*args):
        raise OSError("secret attacker-controlled input")

    monkeypatch.setattr(_windows_resources, "read_windows", fail)
    with pytest.raises(manifest.PackError, match="^Pack resource missing or unsafe$"):
        manifest.read_local(tmp_path, "sheet.png", 4)


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor regression")
class TestPosixDescriptors:
    def test_unavailable_nofollow_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delattr(os, "O_NOFOLLOW")
        with pytest.raises(manifest.PackError, match="unavailable"):
            manifest.read_local(tmp_path, "sheet.png", 4)

    def test_read_error_closes_every_descriptor(self, tmp_path, monkeypatch):
        root = tmp_path / "pack"
        (root / "nested").mkdir(parents=True)
        (root / "nested" / "sheet.png").write_bytes(b"safe")
        opened, closed = [], []
        original_open, original_close = os.open, os.close

        def open_file(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def close_file(fd):
            closed.append(fd)
            original_close(fd)

        def fail_read(*args):
            raise OSError("injected read failure")

        monkeypatch.setattr(os, "open", open_file)
        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {open_file})
        monkeypatch.setattr(os, "close", close_file)
        monkeypatch.setattr(os, "read", fail_read)
        with pytest.raises(manifest.PackError):
            manifest.read_local(root, "nested/sheet.png", 4)
        assert len(opened) == 3 and closed == opened[::-1]

    def test_growth_after_fstat_is_still_bounded(self, tmp_path, monkeypatch):
        path = tmp_path / "sheet.png"
        path.write_bytes(b"safe")
        original = os.read
        counts = []

        def read(fd, count):
            if not counts:
                path.write_bytes(b"x" * 100)
            counts.append(count)
            return original(fd, count)

        monkeypatch.setattr(os, "read", read)
        with pytest.raises(manifest.PackError, match="size"):
            manifest.read_local(tmp_path, "sheet.png", 4)
        assert counts == [5]

    def test_directory_swap_keeps_original_descriptor(self, tmp_path, monkeypatch):
        root, outside = tmp_path / "pack", tmp_path / "outside"
        nested = root / "nested"
        nested.mkdir(parents=True)
        outside.mkdir()
        (nested / "sheet.png").write_bytes(b"safe")
        (outside / "sheet.png").write_bytes(b"evil")
        original = os.open

        def open_file(path, *args, **kwargs):
            fd = original(path, *args, **kwargs)
            if path == "nested":
                nested.rename(root / "moved")
                nested.symlink_to(outside, target_is_directory=True)
            return fd

        monkeypatch.setattr(os, "open", open_file)
        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {open_file})
        assert manifest.read_local(root, "nested/sheet.png", 4) == b"safe"
