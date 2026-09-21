"""Offline regressions for selection snapshots, equipment restore and watch startup."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from click.testing import CliRunner
from PIL import Image

from openvegas.emotes import commands
from openvegas.emotes.controller import EmoteController, State
from openvegas.emotes.events import Event, Phase
from openvegas.emotes.manifest import load_pack
from openvegas.emotes.remote import RemoteError, RemoteLibrary
from openvegas.emotes.selection import SelectionStore
from openvegas.emotes.spool import EventSpool, _locked, atomic_write


@pytest.fixture
def fixture_pack(tmp_path):
    root = tmp_path / "art"
    root.mkdir()
    Image.new("RGBA", (2, 1), (10, 20, 30, 255)).save(root / "sheet.png")
    raw = {
        "schema_version": 1,
        "pack_id": "fixture.pack",
        "version": "1.0.0",
        "display_name": "Synthetic Test",
        "license_id": "synthetic-test-data",
        "sheet": "sheet.png",
        "sha256": hashlib.sha256((root / "sheet.png").read_bytes()).hexdigest(),
        "frame": {"width": 1, "height": 1, "anchor": [0, 0]},
        "animations": {
            "idle": {"frames": [0], "frame_ms": 200, "loop": True},
            "waiting": {"frames": [0, 1], "frame_ms": 100, "loop": True},
            "complete": {"frames": [1, 0, 1], "frame_ms": 1600, "loop": False},
        },
        "reduced_motion_frame": 0,
        "tags": ["test-only"],
    }
    (root / "manifest.json").write_text(json.dumps(raw))
    return root, load_pack(root)


def event(phase, generation, sequence=0, *, session="session"):
    return Event(
        "test",
        session,
        f"turn-{generation}",
        f"event-{generation}-{sequence}",
        phase,
        generation,
        sequence,
        "success" if phase == Phase.COMPLETE else None,
    )


def test_snapshot_v1_migration_and_cas_reject_concurrent_off(tmp_path):
    selection = SelectionStore(tmp_path / "state")
    with _locked(selection.directory) as fd:
        atomic_write(fd, "selection.json", b'{"schema_version":1,"pack_id":"old.pack"}')
    slots, revision = selection.snapshot()
    assert slots == {"companion": "old.pack", "completion": None}
    selection.disable()
    slots["completion"] = "fixture.finish"
    assert selection.compare_and_write_slots(slots, expected_revision=revision) is None
    assert selection.read_slots() == {"companion": None, "completion": None}


def test_snapshot_holds_writer_lock(tmp_path, monkeypatch):
    selection = SelectionStore(tmp_path / "state")
    selection.write("old.pack")
    read = selection._read_slots
    attempts = []

    def interleave(fd):
        with pytest.raises((OSError, ValueError)):
            SelectionStore(selection.directory).disable()
        attempts.append(True)
        return read(fd)

    monkeypatch.setattr(selection, "_read_slots", interleave)
    slots, revision = selection.snapshot()
    assert attempts and slots["companion"] == "old.pack"
    assert revision == selection.revision()


def services_for(tmp_path, fixture_pack, monkeypatch):
    _, pack = fixture_pack
    completion = replace(
        pack,
        manifest=replace(pack.manifest, pack_id="fixture.finish", tags=("completion",)),
    )
    selection = SelectionStore(tmp_path / "state")
    spool = EventSpool(tmp_path / "events")
    catalog = SimpleNamespace(
        entries={
            "fixture.pack": SimpleNamespace(
                pack_id="fixture.pack", resource_name="fixture", access="free"
            )
        },
        resource_for=lambda *a, **k: "fixture",
    )
    repository = SimpleNamespace(load=lambda _: pack)
    services = commands.EmoteServices(repository, catalog, selection, spool)
    monkeypatch.setattr(
        commands,
        "_load",
        lambda svc, name, **kw: completion if name == "fixture.finish" else pack,
    )
    return services


def test_local_completion_equip_cannot_resurrect_off(
    tmp_path, fixture_pack, monkeypatch
):
    services = services_for(tmp_path, fixture_pack, monkeypatch)
    services.selection.write("fixture.pack")
    load = commands._load

    def off_during_load(*args, **kwargs):
        services.selection.disable()
        return load(*args, **kwargs)

    monkeypatch.setattr(commands, "_load", off_during_load)
    result = CliRunner().invoke(
        commands.emote,
        ["equip", "fixture.finish", "--slot", "completion"],
        obj=services,
    )
    assert result.exit_code == 1 and "changed" in result.output
    assert services.selection.read_slots() == {"companion": None, "completion": None}


class TwoSlotAPI:
    backend_scope = "https://example.test"

    def __init__(self, root):
        self.user = str(uuid4())
        self.rows, self.bundles, self.calls = [], {}, []
        self.equipped = {"companion": "item-companion", "completion": "item-completion"}
        self.on_pack = lambda item: None
        for slot, pack_id in (
            ("companion", "fixture.pack"),
            ("completion", "fixture.finish"),
        ):
            item = "item-" + slot
            manifest = json.loads((root / "manifest.json").read_text())
            manifest.update(pack_id=pack_id, tags=[slot])
            self.rows.append(
                dict(
                    item_id=item,
                    pack_id=pack_id,
                    slot=slot,
                    effective_status="active",
                    activatable=True,
                    available_version="1.0.0",
                )
            )
            self.bundles[item] = dict(
                schema_version=1,
                item_id=item,
                pack_id=pack_id,
                version="1.0.0",
                manifest=manifest,
                sheet_base64=base64.b64encode(
                    (root / "sheet.png").read_bytes()
                ).decode(),
            )

    def identity(self):
        return "https://example.test", self.user

    async def owned(self):
        self.calls.append("owned")
        return dict(
            account_id=self.user,
            entitlements=copy.deepcopy(self.rows),
            equipped=dict(self.equipped),
        )

    async def pack(self, item):
        self.calls.append(("pack", item))
        self.on_pack(item)
        return copy.deepcopy(self.bundles[item])

    async def equip(self, item, slot="companion"):
        self.calls.append(("equip", item, slot))
        self.equipped[slot] = item
        return dict(item_id=item, slot=slot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slot,pack_id", [("companion", "fixture.pack"), ("completion", "fixture.finish")]
)
async def test_fresh_equip_restores_both_downloaded_slots(
    tmp_path, fixture_pack, slot, pack_id
):
    api = TwoSlotAPI(fixture_pack[0])
    selection = SelectionStore(tmp_path / "state")
    library = RemoteLibrary(api, tmp_path / "cache", selection)
    try:
        report = await library.equip(pack_id, slot=slot)
        assert selection.read_slots() == {
            "companion": "fixture.pack",
            "completion": "fixture.finish",
        }
        assert report["available"] == ["fixture.finish", "fixture.pack"]
        assert api.calls[-1] == "owned"
        assert (
            sum(isinstance(call, tuple) and call[0] == "pack" for call in api.calls)
            == 2
        )
    finally:
        library.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["revoke", "off", "cancel", "version", "status", "account"]
)
async def test_counterpart_download_rechecks_authority(tmp_path, fixture_pack, failure):
    api = TwoSlotAPI(fixture_pack[0])
    selection = SelectionStore(tmp_path / "state")
    selection.write_slots({"companion": "fixture.pack", "completion": "fixture.finish"})
    library = RemoteLibrary(api, tmp_path / "cache", selection)

    def mutate(item):
        if item != "item-completion":
            return
        if failure == "revoke":
            api.rows[1]["effective_status"] = "revoked"
        elif failure == "version":
            api.rows[1]["available_version"] = "2.0.0"
        elif failure == "off":
            selection.disable()
        elif failure == "status":

            async def unavailable():
                raise OSError("synthetic ownership status failure")

            api.owned = unavailable
        elif failure == "account":
            api.user = str(uuid4())
        else:
            raise asyncio.CancelledError()

    api.on_pack = mutate
    try:
        if failure == "revoke":
            await library.equip("fixture.pack")
            assert selection.read_slots()["completion"] is None
            assert not library.authorize("fixture.finish")
        else:
            with pytest.raises(
                asyncio.CancelledError if failure == "cancel" else RemoteError
            ):
                await library.equip("fixture.pack")
            assert not library.authorize("fixture.finish")
            if failure == "off":
                assert selection.read_slots() == {"companion": None, "completion": None}
    finally:
        library.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slot,other", [("companion", "completion"), ("completion", "companion")]
)
async def test_remote_unequip_preserves_other_downloaded_slot(
    tmp_path, fixture_pack, slot, other
):
    api = TwoSlotAPI(fixture_pack[0])
    selection = SelectionStore(tmp_path / "state")
    library = RemoteLibrary(api, tmp_path / "cache", selection)
    try:
        await library.equip(None, slot=slot)
        slots = selection.read_slots()
        assert slots[slot] is None
        assert slots[other] == (
            "fixture.pack" if other == "companion" else "fixture.finish"
        )
    finally:
        library.close()


def test_startup_boundary_discards_only_target_and_keeps_watermark(tmp_path):
    spool = EventSpool(tmp_path / "events")
    for e in (
        event(Phase.START, 4),
        event(Phase.COMPLETE, 4, 1),
        event(Phase.START, 7, session="other"),
    ):
        assert spool.publish(e)
    assert commands._watch_boundary(spool, "test", "session") == 4
    assert spool.drain(source="test", session_id="session") == []
    assert len(spool.drain(source="test", session_id="other")) == 1
    with _locked(spool.directory):
        with pytest.raises((OSError, ValueError)):
            commands._watch_boundary(spool, "test", "session")


def configure_watch(monkeypatch, services, after_ready=lambda: None):
    states = []
    controllers = []

    def controller(*args, **kwargs):
        instance = EmoteController(*args, **kwargs)
        controllers.append(instance)
        return instance

    class Live:
        def __init__(self, *a, **kw):
            states.append(controllers[-1].current_state)

        def __enter__(self):
            after_ready()
            return self

        def __exit__(self, *a):
            return False

        def update(self, *a, **kw):
            states.append(controllers[-1].current_state)

    monkeypatch.setattr(commands, "EmoteController", controller)
    monkeypatch.setattr(commands, "Live", Live)
    monkeypatch.setattr(
        commands, "_console", lambda: SimpleNamespace(is_terminal=True, size=(80, 24))
    )
    monkeypatch.setattr(commands, "_animated", lambda _: True)
    monkeypatch.setattr(commands, "_render", lambda *args: "frame")

    def stop(_):
        raise KeyboardInterrupt()

    monkeypatch.setattr(commands.time, "sleep", stop)
    return states


def invoke_watch(services, monkeypatch, *extra):
    with CliRunner().isolation():
        monkeypatch.setattr(commands.sys, "stdin", SimpleNamespace(isatty=lambda: True))
        try:
            commands.emote.main(
                ["watch", "--source", "test", "--session", "session", *extra],
                obj=services,
                standalone_mode=False,
            )
        except Exception as exc:
            return exc
    return None


def test_watch_drops_prestartup_and_delayed_old_success_accepts_next_turn(
    tmp_path, fixture_pack, monkeypatch
):
    services = services_for(tmp_path, fixture_pack, monkeypatch)
    for e in (event(Phase.START, 4), event(Phase.COMPLETE, 4, 1)):
        assert services.spool.publish(e)

    def next_turn():
        for e in (
            event(Phase.START, 4, 2),
            event(Phase.COMPLETE, 4, 3),
            event(Phase.START, 5),
            event(Phase.COMPLETE, 5, 1),
        ):
            assert services.spool.publish(e)

    states = configure_watch(monkeypatch, services, next_turn)
    services.remote_factory = lambda _: pytest.fail("explicit preview must be offline")
    assert (
        invoke_watch(
            services,
            monkeypatch,
            "--pack",
            "fixture.pack",
            "--completion-pack",
            "fixture.finish",
        )
        is None
    )
    assert states == [State.IDLE, State.COMPLETE]


def test_watch_queued_success_alone_never_replays(tmp_path, fixture_pack, monkeypatch):
    services = services_for(tmp_path, fixture_pack, monkeypatch)
    for e in (event(Phase.START, 9), event(Phase.COMPLETE, 9, 1)):
        assert services.spool.publish(e)

    def delayed_old():
        for e in (event(Phase.START, 9, 2), event(Phase.COMPLETE, 9, 3)):
            assert services.spool.publish(e)

    states = configure_watch(monkeypatch, services, delayed_old)
    assert (
        invoke_watch(
            services,
            monkeypatch,
            "--pack",
            "fixture.pack",
            "--completion-pack",
            "fixture.finish",
        )
        is None
    )
    assert states == [State.IDLE, State.IDLE]


@pytest.mark.parametrize("phase", [Phase.CANCEL, Phase.ERROR, Phase.PAUSE])
def test_watch_old_replay_and_non_success_stay_neutral(
    tmp_path, fixture_pack, monkeypatch, phase
):
    services = services_for(tmp_path, fixture_pack, monkeypatch)
    services.spool.publish(event(Phase.START, 4))
    services.spool.publish(event(Phase.COMPLETE, 4, 1))

    def arrivals():
        for e in (
            event(Phase.START, 4, 2),
            event(Phase.COMPLETE, 4, 3),
            event(Phase.START, 5),
            event(phase, 5, 1),
        ):
            assert services.spool.publish(e)
        if phase != Phase.PAUSE:
            assert services.spool.publish(event(Phase.COMPLETE, 5, 2))

    states = configure_watch(monkeypatch, services, arrivals)
    assert (
        invoke_watch(
            services,
            monkeypatch,
            "--pack",
            "fixture.pack",
            "--completion-pack",
            "fixture.finish",
        )
        is None
    )
    assert State.COMPLETE not in states


def test_watch_off_after_snapshot_before_load_never_renders(
    tmp_path, fixture_pack, monkeypatch
):
    services = services_for(tmp_path, fixture_pack, monkeypatch)
    services.selection.write_slots({"companion": None, "completion": "fixture.finish"})
    snapshot = services.selection.snapshot

    def interleave():
        result = snapshot()
        services.selection.disable()
        return result

    monkeypatch.setattr(services.selection, "snapshot", interleave)
    states = configure_watch(monkeypatch, services)
    error = invoke_watch(services, monkeypatch)
    assert error is not None and "changed" in str(error)
    assert states == []


@pytest.mark.parametrize("invalid", [False, True])
def test_watch_boundary_failure_never_renders_or_replays(
    tmp_path, fixture_pack, monkeypatch, invalid
):
    services = services_for(tmp_path, fixture_pack, monkeypatch)
    states = configure_watch(monkeypatch, services)
    if invalid:
        with _locked(services.spool.directory) as fd:
            atomic_write(fd, "a" * 32 + ".json", b"not-json")
        error = invoke_watch(services, monkeypatch, "--pack", "fixture.pack")
    else:
        with _locked(services.spool.directory):
            error = invoke_watch(services, monkeypatch, "--pack", "fixture.pack")
    assert error is not None
    assert states == []
