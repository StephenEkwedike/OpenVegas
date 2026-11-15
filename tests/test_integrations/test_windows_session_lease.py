import os
from contextlib import contextmanager
from multiprocessing import get_context
from uuid import uuid4

import pytest

from openvegas.integrations import windows_session_lease as lease
from openvegas.integrations.credentials import IntegrationError


class State(dict):
    held = False

    @contextmanager
    def locked(self):
        if self.held:
            raise BlockingIOError
        self.held = True
        try:
            yield self
        finally:
            self.held = False

    def read(self, key, limit):
        if key not in self:
            raise FileNotFoundError
        data = self[key]
        assert len(data) <= limit
        return data

    def atomic_write(self, key, data):
        assert self.held
        self[key] = data


@pytest.fixture
def state(monkeypatch):
    states = {}

    @contextmanager
    def private(path):
        yield states.setdefault(path.name, State())

    monkeypatch.setattr(lease, "private_windows_directory", private)
    return states


def test_same_chat_excludes_second_owner_other_chat_is_independent(state, tmp_path):
    with lease.acquire("one", tmp_path) as generation:
        assert generation == 1
        with pytest.raises(BlockingIOError), lease.acquire("one", tmp_path):
            pytest.fail("duplicate owner")
        with lease.acquire("two", tmp_path) as other:
            assert other == 1
    with lease.acquire("one", tmp_path) as generation:
        assert generation == 2


@pytest.mark.parametrize("previous", [b"", b"0", b"01", b"-1", b"bad", b"9223372036854775807"])
def test_bad_generation_never_becomes_a_new_owner(state, tmp_path, previous):
    state["one"] = State(generation=previous)
    with pytest.raises(IntegrationError), lease.acquire("one", tmp_path):
        pytest.fail("invalid generation admitted")
    assert state["one"]["generation"] == previous and not state["one"].held


def _hold_chat(sid, directory, status, stop):
    from openvegas.integrations.session_ownership import SessionLease

    with SessionLease("codex", sid, directory=directory) as held:
        status.send(held.generation)
        stop.wait(30)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows kernel lock")
def test_default_lease_directory_independent_of_launcher_env(monkeypatch, tmp_path):
    original = lease._local_appdata()
    for key in ("HOME", "TEMP", "TMP", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path))
    assert original.is_absolute() and original != tmp_path
    assert lease._local_appdata() == original


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows kernel lock")
def test_second_process_refused_until_owner_exit(tmp_path):
    from openvegas.integrations.session_ownership import SessionLease

    sid = str(uuid4())
    directory = tmp_path / "owners"
    ctx = get_context("spawn")
    incoming, outgoing = ctx.Pipe(duplex=False)
    stop = ctx.Event()
    child = ctx.Process(target=_hold_chat, args=(sid, directory, outgoing, stop))
    child.start()
    outgoing.close()
    try:
        assert incoming.poll(15) and incoming.recv() == 1
        with pytest.raises(IntegrationError, match="Another OpenVegas runtime"):
            SessionLease("codex", sid, directory=directory)
        child.kill()
        child.join(10)
        assert not child.is_alive()
        with SessionLease("codex", sid, directory=directory) as held:
            assert held.generation == 2
    finally:
        incoming.close()
        if child.is_alive():
            child.kill()
            child.join(10)
