"""Installed-package resources and injectable server-owned catalog/access policy."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .manifest import (
    MAX_MANIFEST_BYTES,
    MAX_SHEET_BYTES,
    LoadedPack,
    PackError,
    decode_pack,
    load_pack,
    parse_json,
    safe_token,
    validate_manifest,
)


class PackRepository:
    def __init__(self, root=None):
        self.root = (
            root
            if root is not None
            else resources.files("openvegas.emotes").joinpath("assets")
        )

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        names = []
        for child in self.root.iterdir():
            if len(names) >= 256:
                raise PackError("Package catalog exceeds limit")
            if child.is_dir():
                names.append(safe_token(child.name))
        return sorted(names)

    def load(self, name: str) -> LoadedPack:
        root = self.root.joinpath(safe_token(name))
        if isinstance(root, Path):
            return load_pack(root)

        def read(relative, limit):
            try:
                with root.joinpath(*relative.split("/")).open("rb") as stream:
                    data = stream.read(limit + 1)
                if len(data) > limit:
                    raise PackError("Package resource exceeds size limit")
                return data
            except (OSError, KeyError) as exc:
                raise PackError("Missing package resource") from exc

        manifest = validate_manifest(
            parse_json(read("manifest.json", MAX_MANIFEST_BYTES))
        )
        return decode_pack(manifest, read(manifest.sheet, MAX_SHEET_BYTES))


@dataclass(frozen=True)
class CatalogEntry:
    pack_id: str
    resource_name: str
    display_name: str
    access: str = "preview_only"
    # Premium public previews must be a distinct public resource, never the full pack.
    preview_resource: str | None = None

    def __post_init__(self):
        safe_token(self.pack_id)
        safe_token(self.resource_name)
        if self.preview_resource is not None:
            safe_token(self.preview_resource)
        if self.access not in {"preview_only", "free", "premium"}:
            raise ValueError("Invalid catalog access class")
        if (
            not isinstance(self.display_name, str)
            or not 1 <= len(self.display_name) <= 128
            or not self.display_name.isascii()
            or not self.display_name.isprintable()
        ):
            raise ValueError("Unsafe catalog label")


class Catalog:
    """Construct from a trusted server integration, NEVER downloaded pack flags.

    authorize is a server-backed or server-verifiable bounded offline decision;
    absent/raising/false fails closed. This module supplies no premium grant.
    """

    def __init__(
        self,
        entries: Iterable[CatalogEntry] = (),
        *,
        authorize: Callable[[str], bool] | None = None,
    ):
        self.entries = {entry.pack_id: entry for entry in entries}
        self.authorize = authorize

    def get(self, pack_id: str) -> CatalogEntry:
        try:
            return self.entries[pack_id]
        except KeyError as exc:
            raise PackError("Unknown pack; use emote list") from exc

    def resource_for(self, pack_id: str, *, preview: bool = False) -> str:
        entry = self.get(pack_id)
        if preview and entry.access == "premium" and entry.preview_resource:
            return entry.preview_resource
        if entry.access == "preview_only":
            if preview:
                return entry.resource_name
            raise PackError("This pack is preview-only; release verification is still pending")
        if entry.access == "free":
            return entry.resource_name
        try:
            authorized = self.authorize is not None and self.authorize(pack_id) is True
        except Exception:  # noqa: BLE001 - unavailable entitlement checks fail closed
            authorized = False
        if not authorized:
            raise PackError(
                "Verified server entitlement required; no offline local override"
            )
        return entry.resource_name


def preview_catalog(repository: PackRepository) -> Catalog:
    entries = []
    for name in repository.names():
        try:
            pack = repository.load(name)
            entries.append(
                CatalogEntry(pack.manifest.pack_id, name, pack.manifest.display_name)
            )
        except PackError:
            continue
    return Catalog(entries)
