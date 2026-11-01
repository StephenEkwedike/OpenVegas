"""Local preference only. Revalidate access every time a selected pack is loaded."""

from __future__ import annotations

import json
from pathlib import Path

from .manifest import PackError, parse_json, safe_token
from .resources import Catalog, PackRepository
from .spool import _locked, _read, atomic_write, default_state_dir, private_directory, state_stat


class SelectionStore:
    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory) if directory is not None else default_state_dir()

    @staticmethod
    def _revision(fd) -> tuple[int, int] | None:
        try:
            info = state_stat(fd, "selection.json")
            return info.st_ino, info.st_mtime_ns
        except FileNotFoundError:
            return None

    def revision(self) -> tuple[int, int] | None:
        """Watch this token to observe equip/off, including repeated off writes."""
        with private_directory(self.directory) as fd:
            return self._revision(fd)

    def _read_slots(self, fd) -> dict[str, str | None]:
        try:
            raw = parse_json(_read(fd, "selection.json", 1024), 1024)
            if type(raw.get("schema_version")) is not int:
                raise PackError("Invalid selection schema")
            if raw["schema_version"] == 1 and set(raw) == {"schema_version", "pack_id"}:
                slots = {"companion": raw["pack_id"], "completion": None}
            elif raw["schema_version"] == 2 and set(raw) == {"schema_version", "slots"}:
                slots = raw["slots"]
            else:
                raise PackError("Invalid selection schema")
            return self._validate_slots(slots)
        except FileNotFoundError:
            return {"companion": None, "completion": None}

    @staticmethod
    def _validate_slots(slots):
        if not isinstance(slots, dict) or set(slots) != {"companion", "completion"}:
            raise PackError("Invalid selection slots")
        return {slot: None if value is None else safe_token(value) for slot, value in slots.items()}

    @staticmethod
    def _encode(slots):
        return json.dumps({"schema_version": 2, "slots": slots}).encode("ascii")

    def read_slots(self) -> dict[str, str | None]:
        with private_directory(self.directory) as fd:
            return self._read_slots(fd)

    def snapshot(self) -> tuple[dict[str, str | None], tuple[int, int] | None]:
        """Read both slots and their CAS token under the writers' lock."""
        with _locked(self.directory) as fd:
            return self._read_slots(fd), self._revision(fd)

    def read(self) -> str | None:
        return self.read_slots()["companion"]

    def write(self, pack_id: str | None) -> None:
        if pack_id is not None:
            safe_token(pack_id)
        with _locked(self.directory) as fd:
            slots = self._read_slots(fd)
            slots["companion"] = pack_id
            atomic_write(fd, "selection.json", self._encode(slots))

    def write_slots(self, slots) -> None:
        slots = self._validate_slots(slots)
        with _locked(self.directory) as fd:
            atomic_write(fd, "selection.json", self._encode(slots))

    def disable(self) -> None:
        self.write_slots({"companion": None, "completion": None})

    def compare_and_write_slots(self, slots, *, expected_revision):
        slots = self._validate_slots(slots)
        with _locked(self.directory) as fd:
            if self._revision(fd) != expected_revision:
                return None
            atomic_write(fd, "selection.json", self._encode(slots))
            return self._revision(fd)

    def compare_and_write(self, pack_id, *, expected_revision):
        """Atomically refuse stale network results across cooperating CLI processes."""
        if pack_id is not None:
            safe_token(pack_id)
        with _locked(self.directory) as fd:
            if self._revision(fd) != expected_revision:
                return None
            slots = self._read_slots(fd)
            slots["companion"] = pack_id
            atomic_write(fd, "selection.json", self._encode(slots))
            return self._revision(fd)

    def equip(self, pack_id: str, *, catalog: Catalog, repository: PackRepository) -> None:
        resource = catalog.resource_for(pack_id)
        pack = repository.load(resource)
        if pack.manifest.pack_id != pack_id:
            raise PackError("Catalog and pack identity mismatch")
        self.write(pack_id)

    def load_selected(self, *, catalog: Catalog, repository: PackRepository):
        selected = self.read()
        if selected is None:
            return None
        pack = repository.load(catalog.resource_for(selected))
        if pack.manifest.pack_id != selected:
            raise PackError("Catalog and pack identity mismatch")
        return pack
