from __future__ import annotations

import base64
import copy
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from openvegas.emotes.controller import EmoteController, State
from openvegas.emotes.events import Event, Phase
from openvegas.emotes.manifest import Animation, PackError
from openvegas.emotes.remote import RemoteError, RemoteLibrary
from openvegas.emotes.selection import SelectionStore
from openvegas.emotes.spool import _locked, atomic_write


def event(phase, sequence, *, turn="turn", generation=0):
    return Event(
        "test",
        "session",
        turn,
        str(sequence),
        phase,
        generation,
        sequence,
        "success" if phase == Phase.COMPLETE else None,
    )


@pytest.fixture
def completion(pack):
    animations = dict(pack.manifest.animations)
    animations["complete"] = Animation((2, 1, 2), 1800, False)
    return replace(
        pack,
        manifest=replace(
            pack.manifest,
            pack_id="fixture.finish",
            animations=animations,
            tags=("completion",),
        ),
    )


def test_distinct_completion_runs_once_for_its_own_duration(pack, completion, clock):
    controller = EmoteController(
        pack,
        completion_pack=completion,
        source="test",
        session_id="session",
        clock=clock,
    )
    controller.handle(event(Phase.START, 0))
    assert controller.current_frame().tobytes() == pack.frame(0).tobytes()
    controller.handle(event(Phase.COMPLETE, 1))
    assert controller.current_frame().tobytes() == completion.frame(2).tobytes()
    clock.advance(4.9)
    assert controller.current_state == State.COMPLETE
    controller.handle(event(Phase.COMPLETE, 2))
    clock.advance(0.6)
    assert controller.current_state == State.IDLE
    assert controller.current_frame().tobytes() == pack.frame(0).tobytes()
    assert not controller.handle(event(Phase.COMPLETE, 2))


@pytest.mark.parametrize(
    "phase,state",
    [
        (Phase.CANCEL, State.CANCELLED),
        (Phase.ERROR, State.ERROR),
        (Phase.PAUSE, State.PAUSED),
    ],
)
def test_non_success_never_uses_completion_pack(pack, completion, clock, phase, state):
    controller = EmoteController(
        pack,
        completion_pack=completion,
        source="test",
        session_id="session",
        clock=clock,
    )
    controller.handle(event(Phase.START, 0))
    controller.handle(event(phase, 1))
    assert controller.current_state == state
    assert controller.current_frame().tobytes() == pack.frame(0).tobytes()


def test_new_turn_and_pack_change_retire_celebration(pack, completion, clock):
    controller = EmoteController(
        pack,
        completion_pack=completion,
        source="test",
        session_id="session",
        clock=clock,
    )
    controller.handle(event(Phase.START, 0))
    controller.handle(event(Phase.COMPLETE, 1))
    controller.handle(event(Phase.START, 2, turn="next", generation=1))
    assert controller.current_state == State.ACTIVE
    controller.replace_completion_pack(None)
    controller.handle(event(Phase.COMPLETE, 3, turn="next", generation=1))
    assert controller.current_state == State.IDLE


def test_selection_migrates_v1_without_losing_companion(tmp_path):
    selection = SelectionStore(tmp_path / "state")
    with _locked(selection.directory) as fd:
        atomic_write(fd, "selection.json", b'{"schema_version":1,"pack_id":"old.pack"}')
    assert selection.read_slots() == {"companion": "old.pack", "completion": None}
    slots = selection.read_slots()
    slots["completion"] = "finish.pack"
    selection.write_slots(slots)
    selection.write("new.pack")
    assert selection.read_slots() == {
        "companion": "new.pack",
        "completion": "finish.pack",
    }
    selection.disable()
    assert selection.read_slots() == {"companion": None, "completion": None}


@pytest.mark.parametrize(
    "slots",
    [
        {},
        {"companion": None},
        {"companion": None, "completion": "../bad"},
        {"companion": None, "completion": True},
    ],
)
def test_invalid_two_slot_preference_rejected(tmp_path, slots):
    with pytest.raises(PackError):
        SelectionStore(tmp_path / "state").write_slots(slots)


def test_completion_change_invalidates_atomic_companion_write(tmp_path):
    selection = SelectionStore(tmp_path / "state")
    selection.write("original.pack")
    revision = selection.revision()
    selection.write_slots({"companion": "original.pack", "completion": "new.finish"})
    assert selection.compare_and_write("stale.pack", expected_revision=revision) is None
    assert selection.read_slots()["completion"] == "new.finish"


class API:
    backend_scope = "https://example.test"

    def __init__(self, pack_dir):
        self.user = str(uuid4())
        self.equipped = {"companion": "test-companion", "completion": "test-completion"}
        self.rows, self.bundles = [], {}
        self.on_pack = lambda: None
        for slot, pack_id in (
            ("companion", "fixture.pack"),
            ("completion", "fixture.finish"),
        ):
            item_id = "test-" + slot
            manifest = json.loads((pack_dir / "manifest.json").read_text())
            manifest.update(pack_id=pack_id, tags=[slot])
            self.rows.append(
                {
                    "item_id": item_id,
                    "pack_id": pack_id,
                    "slot": slot,
                    "effective_status": "active",
                    "activatable": True,
                    "available_version": "1.0.0",
                }
            )
            self.bundles[item_id] = {
                "schema_version": 1,
                "item_id": item_id,
                "pack_id": pack_id,
                "version": "1.0.0",
                "manifest": manifest,
                "sheet_base64": base64.b64encode(
                    (pack_dir / "sheet.png").read_bytes()
                ).decode(),
            }

    def identity(self):
        return self.backend_scope, self.user

    async def owned(self):
        return copy.deepcopy(
            {"account_id": self.user, "entitlements": self.rows, "equipped": self.equipped}
        )

    async def pack(self, item_id):
        self.on_pack()
        return copy.deepcopy(self.bundles[item_id])

    async def equip(self, item_id, *, slot):
        self.equipped[slot] = item_id
        return {"item_id": item_id, "slot": slot}


@pytest.fixture
def library(tmp_path, pack_dir, clock):
    api = API(pack_dir)
    selection = SelectionStore(tmp_path / "state")
    return (
        RemoteLibrary(api, tmp_path / "cache", selection, clock=clock),
        api,
        selection,
        clock,
    )


@pytest.mark.asyncio
async def test_restore_and_completion_equip_preserve_both_slots(library):
    remote, api, selection, _ = library
    api.equipped["completion"] = None
    await remote.sync()
    result = await remote.equip("fixture.finish", slot="completion")
    assert result["selected"] == "fixture.pack"
    assert result["selected_slots"] == {
        "companion": "fixture.pack",
        "completion": "fixture.finish",
    }
    assert selection.read_slots() == result["selected_slots"]
    await remote.equip(None, slot="completion")
    assert selection.read_slots() == {"companion": "fixture.pack", "completion": None}


@pytest.mark.asyncio
async def test_completion_revocation_and_lease_failure_stop_access(library):
    remote, api, selection, clock = library
    await remote.sync()
    api.rows[1]["effective_status"] = "revoked"
    await remote.refresh()
    assert selection.read_slots() == {"companion": "fixture.pack", "completion": None}
    assert not remote.authorize("fixture.finish")
    clock.advance(31)
    assert not remote.authorize("fixture.pack")
    assert selection.read_slots() == {"companion": None, "completion": None}


@pytest.mark.asyncio
async def test_late_download_does_not_undo_global_off(library):
    remote, api, selection, _ = library
    selection.write_slots({"companion": "old.pack", "completion": "old.finish"})
    api.on_pack = selection.disable
    with pytest.raises(RemoteError, match="selection changed"):
        await remote.sync()
    assert selection.read_slots() == {"companion": None, "completion": None}
    assert not remote.authorize("fixture.finish")


@pytest.mark.asyncio
async def test_companion_cannot_be_equipped_in_completion_slot(library):
    remote, _, selection, _ = library
    with pytest.raises(RemoteError, match="ownership"):
        await remote.equip("fixture.pack", slot="completion")
    assert selection.read_slots() == {"companion": None, "completion": None}
