from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from openvegas.emotes.selection import SelectionStore
from openvegas.emotes.spool import _locked


def test_stale_compare_never_overwrites_new_choice(tmp_path):
    first = SelectionStore(tmp_path / "state")
    second = SelectionStore(tmp_path / "state")
    first.write("old.pack")
    revision = first.revision()
    second.write("new.pack")
    assert first.compare_and_write(None, expected_revision=revision) is None
    assert first.read() == "new.pack"


def test_competing_compare_writes_have_at_most_one_winner(tmp_path):
    state = SelectionStore(tmp_path / "state")
    state.write("old.pack")
    revision = state.revision()
    barrier = Barrier(4)

    def update(index):
        own = SelectionStore(tmp_path / "state")
        barrier.wait(timeout=2)
        try:
            result = own.compare_and_write(f"new-{index}.pack", expected_revision=revision)
            return index if result is not None else None
        except BlockingIOError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        winners = [i for i in pool.map(update, range(4)) if i is not None]
    assert len(winners) == 1
    assert state.read() == f"new-{winners[0]}.pack"


def test_plain_write_uses_same_nonblocking_lock(tmp_path):
    state = SelectionStore(tmp_path / "state")
    state.write("old.pack")
    with _locked(state.directory), pytest.raises(BlockingIOError):
        state.write("new.pack")
    assert state.read() == "old.pack"
