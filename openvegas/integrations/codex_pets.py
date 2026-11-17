"""Codex pet atlas builder and session-scoped Shop equipment.

Codex keeps decoded frames locally; only freshly verified equipped art is handed
to the managed native session. Standalone preview exports remain public only.
Private artwork studies require a separate explicit local-review opt-in.
Installation is atomic per pet and does not select a pet or edit config.
Explicit upgrades accept only byte-verified prior exports, with retained backups.
Building is portable; secure installation supports macOS/Linux, not Windows.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import uuid
from contextlib import ExitStack, asynccontextmanager, contextmanager
from pathlib import Path

from PIL import Image, ImageOps

from openvegas.emotes.resources import PackRepository
from ._q50_export_history import Q50_PRIOR_EXPORTS
from .credentials import IntegrationError

PREVIEW_PAIRS = (
    ("beat-maker", "skyline-dunk"),
    ("pixel-courier", "bicycle-finish"),
    ("visor-explorer", "three-point-glow"),
)
# Export-only artwork studies are not part of the installed preview collection.
REVIEW_PAIRS = (("naruto-shadow-clone", "naruto-shadow-clone"),)
LOCAL_REVIEW_PAIRS = (("q50-jewelry", "q50-jewelry"), ("q50-jewelry-motion", "q50-jewelry-motion"))
ATLAS_SIZE = (1536, 1872)
CELL_SIZE = (192, 208)
# Codex owns the outer anchor. Keep the visible art above its footer region,
# not at the bottom of an arbitrary source canvas. Shared by every exported pack.
CONTENT_BOX = (8, 8, 184, 152)
NAMESPACE = "openvegas-preview-"
_OWNER_FILE = ".openvegas-preview.json"
_DANCE_TRACKS = (
    "running", "waiting", "running-right", "running-left", "waving", "jumping",
    "move_right", "move_left", "wave", "bounce",
)


class CodexPetError(ValueError):
    """Unsupported preview pair or unsafe/conflicting installation."""


def _pack_layout(pack, indices, content_box=CONTENT_BOX):
    """One union crop/scale for an entire pack preserves authored motion/jumps."""
    bounds, canvas = None, None
    for index in sorted(indices):
        with pack.frame(index) as frame:
            if canvas is not None and frame.size != canvas:
                raise CodexPetError("Animation frame dimensions differ within a pack")
            canvas = frame.size
            box = frame.getchannel("A").getbbox()
            if box:
                bounds = box if bounds is None else (
                    min(bounds[0], box[0]), min(bounds[1], box[1]),
                    max(bounds[2], box[2]), max(bounds[3], box[3]))
    if bounds is None:
        raise CodexPetError("Animation contains no visible artwork")
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    left, top, right, bottom = content_box
    scale = min((right - left) / width, (bottom - top) / height)
    # Integer enlargement keeps pixel edges exact; oversized art is uniformly fit.
    if scale >= 1:
        scale = int(scale)
    size = (max(1, int(width * scale)), max(1, int(height * scale)))
    offset = (left + (right - left - size[0]) // 2,
              top + (bottom - top - size[1]) // 2)
    return bounds, size, offset


def build_pet(companion_slug: str | None, completion_slug: str | None, *, demo=False,
              equipped=False, _legacy_layout=False, _legacy_motion_timing=False, _legacy_size=False) -> tuple[dict, bytes]:
    """Return a native manifest and deterministic PNG from allowlisted packaged art."""
    pair = (companion_slug, completion_slug)
    if equipped:
        if (demo or not any(pair) or any(slug is not None and slug not in
                {p[index] for p in PREVIEW_PAIRS} for index, slug in enumerate(pair))):
            raise CodexPetError("Unsupported public equipment artwork")
    elif pair not in PREVIEW_PAIRS + REVIEW_PAIRS + LOCAL_REVIEW_PAIRS or (demo and pair not in PREVIEW_PAIRS + LOCAL_REVIEW_PAIRS):
        raise CodexPetError("Only reviewed public preview pairs are supported")
    repository = PackRepository()
    companion = repository.load(companion_slug) if companion_slug else None
    completion = repository.load(completion_slug) if completion_slug else None
    return _atlas(pair, companion, completion, demo=demo, _legacy_layout=_legacy_layout,
                  _legacy_motion_timing=_legacy_motion_timing, _legacy_size=_legacy_size)


def _atlas(pair, companion, completion, *, owned=False, demo=False, _legacy_layout=False,
           _legacy_motion_timing=False, _legacy_size=False):
    companion_slug, completion_slug = pair
    motion = companion_slug == "q50-jewelry-motion"
    # Codex 0.163 requires a fixed 1536x1872 sheet but accepts a custom grid.
    old_size = _legacy_size or _legacy_layout or _legacy_motion_timing
    columns, rows = (12, 12) if motion else ((8, 9) if old_size else (6, 9))
    cell_size = (ATLAS_SIZE[0] // columns, ATLAS_SIZE[1] // rows)
    content_box = (5, 6, 123, 114) if motion else CONTENT_BOX
    atlas = Image.new("RGBA", ATLAS_SIZE)
    mappings = []
    slot = 0
    for pack in (companion, completion):
        if pack is None:
            mappings.append({})
            continue
        if motion and mappings:
            mappings.append(mappings[0])
            continue
        indices = {pack.manifest.reduced_motion_frame}
        indices.update(i for clip in pack.manifest.animations.values() for i in clip.frames)
        layout = None if _legacy_layout else _pack_layout(pack, indices, content_box)
        if layout is not None and not old_size:
            bounds, size, _ = layout
            size = tuple(round(value * 1.4) for value in size)
            if size[0] > cell_size[0] or size[1] > cell_size[1]:
                raise CodexPetError("Enlarged animation exceeds its native cell")
            # Keep one transform across the complete animation, never per-pose fitting.
            layout = bounds, size, ((cell_size[0] - size[0]) // 2, 0)
        mapping = {}
        for index in sorted(indices):
            if slot >= rows * columns:
                raise CodexPetError("Preview exceeds its bounded native atlas")
            with pack.frame(index) as source:
                if layout is None:
                    # Retained solely to verify/migrate exact old public exports.
                    scale = min(CELL_SIZE[0] // source.width, CELL_SIZE[1] // source.height)
                    size = (source.width * scale, source.height * scale) if scale else CELL_SIZE
                    frame = ImageOps.contain(source, size, Image.Resampling.NEAREST)
                    offset = ((CELL_SIZE[0] - frame.width) // 2, CELL_SIZE[1] - frame.height)
                else:
                    bounds, size, offset = layout
                    with source.crop(bounds) as cropped:
                        frame = cropped.resize(size, Image.Resampling.LANCZOS if motion else Image.Resampling.NEAREST)
                with frame:
                    atlas.paste(frame, ((slot % columns) * cell_size[0] + offset[0],
                                       (slot // columns) * cell_size[1] + offset[1]))
            mapping[index] = slot
            slot += 1
        mappings.append(mapping)

    def track(pack, mapping, name, *, loop):
        clip = pack.manifest.animations[name]
        fps = 1000 / clip.frame_ms
        if not 0 < fps <= 60:
            raise CodexPetError("Public preview FPS exceeds the native contract")
        return {"frames": [mapping[i] for i in clip.frames], "fps": fps,
                "loop": loop, "fallback": "idle"}

    idle = mappings[0][companion.manifest.animations["idle"].frames[0]] if companion else slot
    if idle >= rows * columns:
        raise CodexPetError("No transparent idle frame available")
    still = {"frames": [idle], "fps": 1, "loop": False, "fallback": "idle"}
    manifest = {
        "id": NAMESPACE + (companion_slug or "none") + "-" + (completion_slug or "none"),
        "displayName": "OpenVegas " + " + ".join(p.manifest.display_name for p in (companion, completion) if p) + " (Preview)",
        "description": "OpenVegas public previews only; not premium owned cosmetics.",
        "spritesheetPath": "spritesheet.png",
        "frame": {"width": cell_size[0], "height": cell_size[1], "columns": columns, "rows": rows},
        "animations": {
            "idle": {"frames": [idle], "fps": 1, "loop": True, "fallback": "idle"},
            # Override every CLI 0.163 default: inherited indices target another atlas.
            **{name: track(companion, mappings[0], "waiting", loop=True) if companion else still
               for name in _DANCE_TRACKS},
            "failed": {"frames": [idle], "fps": 1, "loop": True, "fallback": "idle"},
            "sad": {"frames": [idle], "fps": 1, "loop": True, "fallback": "idle"},
            "review": track(completion, mappings[1], "complete", loop=False) if completion else still,
        },
    }
    if owned:
        manifest["id"] = "openvegas-equipped-" + (companion_slug or "none") + "-" + (completion_slug or "none")
        manifest["displayName"] = "OpenVegas " + " + ".join(p.manifest.display_name for p in (companion, completion) if p)
        manifest["description"] = "Equipped Shop artwork for this session."
        if companion:
            manifest["animations"]["idle"] = track(companion, mappings[0], "idle", loop=True)
    if demo:
        # Separate, explicitly labeled visual tests. Normal pets still follow
        # genuine host lifecycle, and private entitlements never enter this cache.
        manifest["id"] = "openvegas-demo-" + companion_slug
        label = str(PREVIEW_PAIRS.index(pair) + 1) if pair in PREVIEW_PAIRS else "Q50"
        manifest["displayName"] = f"OV Demo {label}: {companion.manifest.display_name}"
        manifest["description"] = "Public artwork animation test; idle loops are not task completion or rewards."
        demo_fps = 20 if motion and not _legacy_motion_timing else 25
        def samples(name, repeats=1):
            clip = manifest["animations"][name]
            count = round(len(clip["frames"]) / clip["fps"] * demo_fps * repeats)
            return [clip["frames"][int(tick * clip["fps"] / demo_fps) % len(clip["frames"])]
                    for tick in range(count)]
        manifest["animations"]["idle"] = {
            "frames": samples("waiting", 3) + samples("review") + [idle] * demo_fps,
            "fps": demo_fps, "loop": True, "fallback": "idle",
        }
    if pair in LOCAL_REVIEW_PAIRS:
        if not demo:
            manifest["displayName"] = "OV Q50: Jewelry Flex (Local Review)"
        manifest["description"] = "Private six-pose artwork review; not for sale, not an endorsement or owned cosmetic. Demo loops are not task status."
    if motion:
        manifest["displayName"] = "OV Demo Q50 Motion" if demo else "OV Q50 Motion (Status)"
        manifest["description"] = "Continuous Q50 motion, local artwork review; not for sale or an owned cosmetic. Demo loops are not task status."
    output = io.BytesIO()
    atlas.save(output, format="PNG")
    atlas.close()
    return manifest, output.getvalue()


def _equipped_pair(owned):
    if not isinstance(owned, dict) or not isinstance(owned.get("account_id"), str) or not owned["account_id"]:
        return None, None
    selected, entitlements = owned.get("equipped"), owned.get("entitlements")
    if not isinstance(selected, dict) or not isinstance(entitlements, list) or len(entitlements) > 256:
        return None, None
    pair = []
    for index, slot in enumerate(("companion", "completion")):
        item = selected.get(slot)
        slug = next((p[index] for p in PREVIEW_PAIRS if item == "openvegas." + p[index]), None)
        rows = [e for e in entitlements if isinstance(e, dict) and e.get("item_id") == item]
        valid = (slug and len(rows) == 1 and rows[0].get("slot") == slot
                 and rows[0].get("pack_id") == item and rows[0].get("effective_status") == "active"
                 and rows[0].get("activatable") is True and rows[0].get("equipped") is True)
        pair.append(slug if valid else None)
    return tuple(pair)


@asynccontextmanager
async def equipped_pet_args(broker, argv=()):
    """Set only the managed TUI's pet for this process, never its saved preference."""
    explicit_pet = any("tui.pet=" in arg.replace(" ", "") for arg in argv)
    # An empty override list revives the native saved preview (for example Q50).
    # Codex 0.166/0.169 use this sentinel to hide it without changing config.toml.
    args = [] if explicit_pet else ["-c", 'tui.pet="disabled"']
    with ExitStack() as cleanup:
        try:
            if not explicit_pet:
                from .cosmetics import _HostAssets, _HostLibrary
                from openvegas.emotes.resources import Catalog
                from openvegas.emotes.selection import SelectionStore
                async with asyncio.timeout(9):
                    owned = await broker.cosmetics_owned("codex")
                    pair = _equipped_pair(owned)
                    if any(pair):
                        directory = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="ov-pet-"))).resolve()
                        api = _HostAssets(broker, "codex", (broker.backend, owned["account_id"]))
                        with tempfile.TemporaryDirectory(dir=directory) as scratch:
                            library = _HostLibrary(api, Path(scratch) / "cache", SelectionStore(Path(scratch) / "selection"))
                            try:
                                selection = await library.sync()
                                if pair != _equipped_pair(selection["owned"]):
                                    raise CodexPetError("Equipment changed during startup")
                                catalog = library.catalog(Catalog())
                                repo = library.repository(PackRepository())
                                packs = []
                                for slot, slug in zip(("companion", "completion"), pair):
                                    key = selection["selected_slots"][slot]
                                    if slug is None and key is None:
                                        packs.append(None)
                                    elif slug and key == "openvegas." + slug and library.authorize(key):
                                        pack = repo.load(catalog.resource_for(key))
                                        if slot not in pack.manifest.tags or pack.manifest.pack_id != key:
                                            raise CodexPetError("Unexpected Shop pack")
                                        packs.append(pack)
                                    else:
                                        raise CodexPetError("Equipped pack not authorized")
                                manifest, png = _atlas(pair, *packs, owned=True)
                                if not all(library.authorize(p.manifest.pack_id) for p in packs if p):
                                    raise CodexPetError("Equipment authorization expired")
                            finally:
                                library.close()
                        (directory / "spritesheet.png").write_bytes(png)
                        (directory / "pet.json").write_bytes(_json(manifest))
                        args = ["-c", "tui.pet=" + json.dumps(directory.as_posix(), ensure_ascii=False)]
        except (OSError, IntegrationError, ValueError, TimeoutError):
            print("OpenVegas: equipped character unavailable. No saved preview substituted; "
                  "start a new session to retry. Chat is still available.", file=sys.stderr)
        yield args


def _json(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _files(manifest: dict, png: bytes, *, legacy=False) -> dict[str, bytes]:
    files = {"pet.json": _json(manifest), "spritesheet.png": png}
    files[_OWNER_FILE] = _json({
        "owner": "openvegas.codex-public-previews", "version": 1 if legacy else 2,
        "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    })
    return files


@contextmanager
def _directory(path: Path):
    """Retain no-follow descriptors for every ancestor, not just the final path."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise CodexPetError("Secure pet installation requires macOS or Linux")
    if ".." in path.parts:
        raise CodexPetError("Parent traversal is not allowed")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _existing_matches(parent: int, name: str, files: dict[str, bytes]) -> bool:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        return False
    try:
        if set(os.listdir(fd)) != set(files):
            raise CodexPetError("Existing pet is foreign or changed; nothing was overwritten")
        for filename, expected in files.items():
            child = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(child, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_size != len(expected)
                        or stream.read(len(expected) + 1) != expected):
                    raise CodexPetError("Existing pet is foreign or changed; nothing was overwritten")
        return True
    finally:
        os.close(fd)


def _publish(parent: int, staging: str, name: str, *, target_parent=None) -> None:
    """Atomic rename without replacing even an empty racing foreign directory."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename, flag = getattr(libc, "renameatx_np", None), 4  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename, flag = getattr(libc, "renameat2", None), 1  # RENAME_NOREPLACE
    else:
        rename, flag = None, 0
    if rename is None:
        raise CodexPetError("Atomic no-replace pet installation is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                       ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(staging), parent if target_parent is None else target_parent,
              os.fsencode(name), flag):
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise CodexPetError("Pet directory appeared during installation; not overwritten")
        raise OSError(code, os.strerror(code))


def _prior_q50_export(parent: int, name: str) -> dict[str, bytes]:
    """Read only an exact trusted historical export, not a self-signed receipt."""
    candidates = [entry['files'] for entry in Q50_PRIOR_EXPORTS if entry['id'] == name]
    if not candidates:
        raise CodexPetError("No verified prior Q50 export")
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        if set(os.listdir(fd)) != set(candidates[0]):
            raise CodexPetError("Prior Q50 export contains foreign files")
        files = {}
        for filename in candidates[0]:
            limit = max(entry[filename]['bytes'] for entry in candidates)
            child = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(child, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                    raise CodexPetError("Prior Q50 export is modified")
                files[filename] = stream.read(limit + 1)
        for expected in candidates:
            if all(len(files[key]) == value['bytes'] and
                   hashlib.sha256(files[key]).hexdigest() == value['sha256']
                   for key, value in expected.items()):
                return files
        raise CodexPetError("Prior Q50 export is modified or unknown")
    finally:
        os.close(fd)


def _install(parent: int, name: str, files: dict[str, bytes]) -> None:
    staging = "." + NAMESPACE + uuid.uuid4().hex
    os.mkdir(staging, 0o700, dir_fd=parent)
    fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    published = False
    written = []
    try:
        for filename, data in files.items():
            child = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=fd)
            written.append(filename)
            with os.fdopen(child, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        os.fsync(fd)
        _publish(parent, staging, name)
        published = True
        os.fsync(parent)
    finally:
        if not published:
            for filename in written:
                os.unlink(filename, dir_fd=fd)
            os.rmdir(staging, dir_fd=parent)
        os.close(fd)


def install_previews(home: Path, *, demos=False, upgrade=False, review_pack=None) -> list[Path]:
    """Install public pairs or one explicit review into an existing Codex home.

    Identical installs are no-ops. Modified/foreign targets are always refused.
    upgrade migrates byte-verified prior exports with backups outside the pet picker.
    Each directory publishes atomically; an interrupted
    batch can be rerun. This does not change Codex configuration or select a pet.
    """
    if review_pack is not None and (review_pack, review_pack) not in LOCAL_REVIEW_PAIRS:
        raise CodexPetError("Unsupported local artwork review")
    # Keep six-pose installs untouched/recoverable; new motion has its own native ID.
    resource = "q50-jewelry-motion" if review_pack == "q50-jewelry" else review_pack
    pairs = PREVIEW_PAIRS if resource is None else ((resource, resource),)
    home = Path(home).absolute()
    bundles = [build_pet(*pair, demo=demos) for pair in pairs]
    expected = [(manifest["id"], _files(manifest, png)) for manifest, png in bundles]
    try:
        with _directory(home) as home_fd:
            try:
                os.mkdir("pets", 0o700, dir_fd=home_fd)
            except FileExistsError:
                pass
            pets_fd = os.open("pets", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=home_fd)
            try:
                missing, upgrades = [], []
                for (name, files), pair in zip(expected, pairs):
                    try:
                        if not _existing_matches(pets_fd, name, files):
                            missing.append((name, files))
                    except CodexPetError:
                        if not upgrade:
                            raise
                        previous_size = _files(*build_pet(*pair, demo=demos, _legacy_size=True))
                        try:
                            matches_previous = _existing_matches(pets_fd, name, previous_size)
                        except CodexPetError:
                            matches_previous = False
                        if matches_previous:
                            upgrades.append((name, files, previous_size))
                            continue
                        if pair == ("q50-jewelry-motion", "q50-jewelry-motion"):
                            try:
                                legacy = _prior_q50_export(pets_fd, name)
                            except CodexPetError:
                                if not demos:
                                    raise
                                legacy = _files(*build_pet(*pair, demo=True, _legacy_motion_timing=True))
                        else:
                            legacy = _files(*build_pet(*pair, demo=demos, _legacy_layout=True), legacy=True)
                        if not _existing_matches(pets_fd, name, legacy):
                            raise CodexPetError("Prior pet disappeared during upgrade")
                        upgrades.append((name, files, legacy))
                # Validate the entire batch before changing any existing pet.
                if upgrades:
                    try:
                        os.mkdir(".openvegas-pet-backups", 0o700, dir_fd=home_fd)
                    except FileExistsError:
                        pass
                    backup_fd = os.open(".openvegas-pet-backups", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                        dir_fd=home_fd)
                    try:
                        for name, files, legacy in upgrades:
                            backup = name + "-" + uuid.uuid4().hex
                            _publish(pets_fd, name, backup, target_parent=backup_fd)
                            try:
                                if not _existing_matches(backup_fd, backup, legacy):
                                    raise CodexPetError("Prior pet changed during upgrade")
                                _install(pets_fd, name, files)
                            except BaseException:
                                # No-replace rollback preserves a racing foreign target.
                                try:
                                    _publish(backup_fd, backup, name, target_parent=pets_fd)
                                except (OSError, CodexPetError):
                                    pass  # Original remains recoverable in the backup directory.
                                raise
                    finally:
                        os.close(backup_fd)
                for name, files in missing:
                    _install(pets_fd, name, files)
            finally:
                os.close(pets_fd)
    except OSError as exc:
        raise CodexPetError("Unsafe or unavailable pet installation path") from exc
    return [home / "pets" / name for name, _ in expected]
