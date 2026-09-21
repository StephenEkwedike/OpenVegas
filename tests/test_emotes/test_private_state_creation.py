"""Fresh-profile state creation; no dotenv, credentials, or network."""

import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from openvegas.emotes import spool
from openvegas.emotes.commands import emote
from openvegas.emotes.events import Event, Phase
from openvegas.emotes.selection import SelectionStore


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "fresh-home"
    home.mkdir(mode=0o700)
    for key, value in {
        "HOME": home,
        "USERPROFILE": home,
        "APPDATA": home / "appdata",
        "LOCALAPPDATA": home / "localappdata",
        "XDG_CONFIG_HOME": home / ".config",
    }.items():
        monkeypatch.setenv(key, str(value))
    assert spool.default_state_dir().is_relative_to(home)
    return home


def assert_private_new_ancestors(path, home):
    current = path
    while current != home:
        assert current.is_relative_to(home)
        if os.name == "posix":
            assert stat.S_IMODE(current.stat().st_mode) == 0o700
            assert current.stat().st_uid == os.getuid()
        current = current.parent


def test_fresh_home_event_first_then_selection(fresh_home):
    event = Event("test", "session", "turn", "event", Phase.START, 0, 0)
    queue = spool.EventSpool()
    assert queue.publish(event)
    assert_private_new_ancestors(queue.directory, fresh_home)
    selection = SelectionStore()
    assert selection.directory == queue.directory.parent
    selection.write("fixture.pack")
    assert selection.read() == "fixture.pack"
    assert queue.drain(source="test", session_id="session") == [event]


def test_fresh_home_doctor_then_events_and_selection(fresh_home):
    result = CliRunner().invoke(emote, ["doctor"])
    assert result.exit_code == 0, result.output
    assert result.output.count("local lock/read/write/delete probe passed") == 2
    assert_private_new_ancestors(spool.default_spool_dir(), fresh_home)
    selection = SelectionStore()
    selection.write("fixture.pack")
    assert selection.read() == "fixture.pack"
    event = Event("test", "session", "turn", "event", Phase.START, 0, 0)
    queue = spool.EventSpool()
    assert queue.publish(event)
    assert queue.drain(source="test", session_id="session") == [event]


if os.name == "posix":

    @pytest.mark.parametrize("mask", [0, 0o022, 0o077])
    def test_posix_new_parents_private_without_chmod(tmp_path, monkeypatch, mask):
        existing = tmp_path / "existing"
        existing.mkdir(mode=0o755)
        existing.chmod(0o755)

        def no_chmod(*args, **kwargs):
            pytest.fail("Private creation must never repair existing permissions")

        monkeypatch.setattr(os, "chmod", no_chmod)
        monkeypatch.setattr(os, "fchmod", no_chmod)
        previous = os.umask(mask)
        try:
            with spool.private_directory(existing / "app" / "emotes" / "events") as directory:
                assert stat.S_IMODE(os.fstat(directory).st_mode) == 0o700
        finally:
            os.umask(previous)
        assert_private_new_ancestors(existing / "app" / "emotes" / "events", existing)
        assert stat.S_IMODE(existing.stat().st_mode) == 0o755

    def test_posix_existing_unsafe_state_not_repaired(tmp_path):
        path = tmp_path / "unsafe"
        path.mkdir(mode=0o755)
        path.chmod(0o755)
        with pytest.raises(spool.SpoolError), spool.private_directory(path):
            pytest.fail("Existing unsafe state accepted")
        assert stat.S_IMODE(path.stat().st_mode) == 0o755

    @pytest.mark.parametrize("position", ["parent", "leaf"])
    def test_posix_symlink_in_creation_chain_rejected(tmp_path, position):
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        link = tmp_path / "linked"
        link.symlink_to(outside, target_is_directory=True)
        target = link / "new-child" if position == "parent" else link
        with pytest.raises((OSError, ValueError)), spool.private_directory(target):
            pytest.fail("Symlink accepted")
        assert list(outside.iterdir()) == []

    @pytest.mark.parametrize("replacement", ["symlink", "unsafe-directory"])
    def test_posix_creation_race_revalidates_winner(tmp_path, monkeypatch, replacement):
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        real_mkdir = os.mkdir

        def race(path, mode=0o777, *, dir_fd=None):
            if str(path) == "raced" and dir_fd is not None:
                if replacement == "symlink":
                    os.symlink(str(outside), "raced", dir_fd=dir_fd)
                else:
                    real_mkdir("raced", mode=0o755, dir_fd=dir_fd)
                    os.chmod("raced", 0o755, dir_fd=dir_fd)
                raise FileExistsError
            return real_mkdir(path, mode=mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "mkdir", race)
        with (
            pytest.raises((OSError, ValueError)),
            spool.private_directory(tmp_path / "raced" / "events"),
        ):
            pytest.fail("Unsafe race winner accepted")
        assert list(outside.iterdir()) == []

    def test_posix_relative_creation_uses_retained_parent(tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with spool.private_directory(Path("new") / "emotes" / "events") as directory:
            assert stat.S_IMODE(os.fstat(directory).st_mode) == 0o700
        assert_private_new_ancestors(tmp_path / "new" / "emotes" / "events", tmp_path)
