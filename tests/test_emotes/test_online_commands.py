import asyncio
import threading
import time

import pytest
from click.testing import CliRunner

from openvegas.emotes.commands import EmoteServices, emote
from openvegas.emotes.manifest import PackError
from openvegas.emotes.online import LeaseRefresher
from openvegas.emotes.resources import Catalog, PackRepository
from openvegas.emotes.selection import SelectionStore
from openvegas.emotes.spool import EventSpool


class Library:
    def __init__(self, selection):
        self.selection = selection
        self.calls = []
        self.closed = 0

    async def sync(self):
        self.calls.append("sync")
        self.selection.write("fixture.pack")
        return {"available": ["fixture.pack"], "selected": "fixture.pack"}

    async def refresh(self):
        self.calls.append("refresh")
        return {
            "owned": {
                "entitlements": [
                    {
                        "pack_id": "fixture.pack",
                        "activatable": True,
                        "effective_status": "active",
                    }
                ]
            }
        }

    async def equip(self, pack_id):
        self.calls.append(("equip", pack_id))
        self.selection.write(pack_id)

    def close(self):
        self.closed += 1


@pytest.fixture
def services(tmp_path):
    selection = SelectionStore(tmp_path / "state")
    library = Library(selection)
    result = EmoteServices(
        PackRepository(tmp_path / "empty"),
        Catalog(),
        selection,
        EventSpool(tmp_path / "events"),
        lambda selection: library,
    )
    return result, library


@pytest.mark.parametrize("command", ["sync", "restore"])
def test_sync_commands_report_restore_not_purchase(services, command):
    service, library = services
    result = CliRunner().invoke(emote, [command], obj=service)
    assert result.exit_code == 0, result.output
    assert "Restored 1" in result.output and "No purchase" in result.output
    assert "fixture.pack" in result.output and "online verification" in result.output
    assert library.calls == ["sync"] and library.closed == 1
    assert service.selection.read() == "fixture.pack"


def test_owned_does_not_equip(services):
    service, library = services
    result = CliRunner().invoke(emote, ["owned"], obj=service)
    assert result.exit_code == 0, result.output
    assert "fixture.pack  [available]" in result.output
    assert library.calls == ["refresh"] and library.closed == 1
    assert service.selection.read() is None


def test_equip_uses_authoritative_library(services):
    service, library = services
    result = CliRunner().invoke(emote, ["equip", "fixture.pack"], obj=service)
    assert result.exit_code == 0, result.output
    assert library.calls == [("equip", "fixture.pack")] and library.closed == 1


def test_local_off_does_not_contact_backend(services):
    service, library = services
    service.selection.write("fixture.pack")
    result = CliRunner().invoke(emote, ["off"], obj=service)
    assert result.exit_code == 0
    assert service.selection.read() is None and library.calls == []


@pytest.mark.parametrize("args", [["sync"], ["owned"], ["equip", "fixture.pack"]])
def test_errors_return_usable_action_not_traceback(services, args):
    service, library = services

    async def fail(*args):
        raise PackError("Session expired. Run: openvegas login")

    library.sync = library.refresh = library.equip = fail
    result = CliRunner().invoke(emote, args, obj=service)
    assert result.exit_code == 1
    assert "openvegas login" in result.output and "Traceback" not in result.output
    assert library.closed == 1


def test_malicious_owned_label_not_sent_to_terminal(services):
    service, library = services

    async def evil():
        return {"owned": {"entitlements": [{"pack_id": "\x1b[2J"}]}}

    library.refresh = evil
    result = CliRunner().invoke(emote, ["owned"], obj=service)
    assert result.exit_code == 1 and "\x1b" not in result.output


def test_lease_refresh_off_renderer_thread_and_stops_after_failure():
    called = threading.Event()
    revoked = threading.Event()
    main = threading.get_ident()

    class Fake:
        async def refresh(self):
            assert threading.get_ident() != main
            called.set()
            raise PackError("offline")

        def invalidate(self):
            revoked.set()

        def close(self):
            pass

    lease = LeaseRefresher(Fake(), interval=0.001).start()
    assert called.wait(1) and revoked.wait(1)
    lease.close()
    assert not lease._thread.is_alive()


def test_lease_close_does_not_wait_for_network_or_invalidate_new_preferences():
    called = threading.Event()
    released = threading.Event()
    invalidated = []

    class Fake:
        async def refresh(self):
            called.set()
            while not released.is_set():
                await asyncio.sleep(0.005)
            raise PackError("late failed request")

        def invalidate(self):
            invalidated.append(True)

        def close(self):
            pass

    lease = LeaseRefresher(Fake(), interval=0.001).start()
    assert called.wait(1)
    before = time.monotonic()
    lease.close()
    assert time.monotonic() - before < 0.5
    released.set()
    lease._thread.join(timeout=1)
    assert not lease._thread.is_alive() and invalidated == []
