from types import SimpleNamespace

import pytest

from openvegas.emotes import spool


def test_unsupported_private_state_fails_before_fcntl_import(monkeypatch, tmp_path):
    monkeypatch.setattr(spool, "os", SimpleNamespace(name="unsupported"))
    with (
        pytest.raises(spool.SpoolError, match="unavailable on this platform"),
        spool._locked(tmp_path / "must-not-create"),
    ):
        pytest.fail("Unsupported spool was opened")
    assert not (tmp_path / "must-not-create").exists()
