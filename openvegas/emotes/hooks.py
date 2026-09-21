"""Opt-in, metadata-only native hooks. No success adapter is certified.

Register ``emote.add_command(hooks)`` in the coordinator. Installed handlers use
hook_dispatch, not normal CLI startup (no auth, dotenv, network, or UI startup).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import click

from .events import IDENTITY, Event, Phase
from .runner import reserve_generation
from .spool import EventSpool, _locked, _names, _private, _read, atomic_write

MAX_INPUT = 64 * 1024
MAX_SETTINGS = 1024 * 1024
MAX_STATE = 128 * 1024
MAX_SESSIONS = 64
MAX_TURNS = 512
MIN_VERSION = (2, 1, 196)
MAX_VERSION = (2, 1, 270)
GEMINI_VERSION = (0, 59, 0)
GEMINI_EVENTS = ("BeforeAgent", "AfterAgent", "Notification", "SessionEnd")
HOOK_EVENTS = (
    "UserPromptSubmit", "PreToolUse", "PermissionRequest", "PostToolUseFailure",
    "Stop", "StopFailure", "SessionEnd",
)
RECEIPT = "installation.json"
NOTICE = (
    "Claude activity-only pilot; no per-answer success celebration. Quiet after permission, "
    "failure or Stop; unobserved interrupts use the companion's 20s lease. "
    "Gemini is observation-only (no animation); Codex native notify is unsupported. "
    "Use emote run for whole-process outcomes, not per-answer success."
)
GEMINI_NOTICE = (
    "Gemini observation-only pilot: opaque session discovery only, no waiting or success animation. "
    "Native hooks lack stable turn identity and final acceptance/cancel ordering. "
    "AfterAgent can trigger retries; timestamp and transcript text are not substitutes."
)


class HookError(ValueError):
    pass


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(data: bytes, limit: int) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise HookError("Duplicate JSON key")
            result[key] = value
        return result

    if len(data) > limit:
        raise HookError("Input exceeds size limit")
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(HookError("Invalid JSON")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HookError("Invalid JSON object") from exc
    if not isinstance(result, dict):
        raise HookError("Expected JSON object")
    return result


def _bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2) + "\n").encode("ascii")


def read_input(fd: int = 0, *, timeout: float = 0.25) -> bytes:
    """Read a pipe/file until EOF, with both byte and wall-time bounds."""
    deadline = time.monotonic() + min(max(timeout, 0.0), 1.0)
    result = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise HookError("Hook input timed out")
        chunk = os.read(fd, min(8192, MAX_INPUT + 1 - len(result)))
        if not chunk:
            return bytes(result)
        result.extend(chunk)
        if len(result) > MAX_INPUT:
            raise HookError("Hook input too large")


def claude_version() -> tuple[int, int, int] | None:
    """Read-only version probe. Never start a model session or install an update."""
    executable = shutil.which("claude")
    if not executable or not Path(executable).is_absolute():
        return None
    try:
        result = subprocess.run([executable, "--version"], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=3, check=False)
        match = re.fullmatch(rb"(\d+)\.(\d+)\.(\d+) \(Claude Code\)\s*", result.stdout)
        if result.returncode == 0 and match and len(result.stdout) < 128:
            return tuple(int(part) for part in match.groups())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def gemini_version() -> tuple[int, int, int] | None:
    """Inspect the installed npm manifest without launching Gemini or loading .env.

    Unknown wrappers/binaries are deliberately unsupported, rather than executing
    provider startup just to discover a version. No package manager is invoked.
    """
    executable = shutil.which("gemini")
    if not executable or not Path(executable).is_absolute():
        return None
    try:
        entry = Path(executable).resolve(strict=True)
        for parent in list(entry.parents)[:4]:
            manifest = parent / "package.json"
            data = _read_settings(manifest)
            if data is None:
                continue
            value = _json(data, MAX_INPUT)
            if value.get("name") != "@google/gemini-cli":
                continue
            match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value.get("version", ""))
            return tuple(int(n) for n in match.groups()) if match else None
    except (OSError, ValueError, TypeError):
        pass
    return None


def _provider(receipt: dict) -> str:
    return receipt.get("provider", "claude")


def _events(provider: str) -> tuple[str, ...]:
    return GEMINI_EVENTS if provider == "gemini" else HOOK_EVENTS


def _notice(provider: str) -> str:
    return GEMINI_NOTICE if provider == "gemini" else NOTICE


def _supported(version) -> bool:
    return isinstance(version, tuple) and len(version) == 3 and all(
        type(n) is int for n in version
    ) and MIN_VERSION <= version <= MAX_VERSION


def _gemini_supported(version) -> bool:
    return (isinstance(version, tuple) and len(version) == 3
            and all(type(n) is int for n in version) and version == GEMINI_VERSION)


def _settings_path(path: Path) -> Path:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise HookError("Native hook installation requires the reviewed POSIX backend")
    # Reject symlinked settings and immediate parent; normalize standard OS aliases
    # such as macOS /tmp only after checking the user-selected endpoint.
    path = Path(os.path.abspath(path.expanduser()))
    if path.name not in {"settings.json", "settings.local.json"}:
        raise HookError("Choose a settings.json or settings.local.json file")
    if path.is_symlink() or path.parent.is_symlink():
        raise HookError("Symlinked settings are not supported")
    return path.parent.resolve() / path.name


def _read_settings(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > MAX_SETTINGS):
            raise HookError("Unsafe settings file")
        result = os.read(fd, MAX_SETTINGS + 1)
        if len(result) > MAX_SETTINGS:
            raise HookError("Settings file exceeds size limit")
        return result
    finally:
        os.close(fd)


def _root(path: Path) -> Path:
    return path.parent / (".openvegas-hooks-" + _digest(path.name.encode())[:12])


@contextmanager
def _readonly(root: Path):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _private(os.fstat(fd), directory=True)
        yield fd
    finally:
        os.close(fd)


def _receipt(root: Path) -> dict | None:
    if not root.exists() and not root.is_symlink():
        return None
    with _readonly(root) as fd:
        try:
            result = _validated_receipt(_json(_read(fd, RECEIPT, MAX_STATE), MAX_STATE))
            if _root(Path(result["settings"])) != root:
                raise HookError("Installation receipt belongs to another settings file")
            return result
        except FileNotFoundError:
            return None


def _validated_receipt(value: dict) -> dict:
    required = {"schema", "id", "enabled", "settings", "entries", "before", "after", "backup"}
    if value.get("schema") == 2:
        required.add("provider")
    provider = _provider(value)
    if (set(value) != required or type(value["schema"]) is not int or value["schema"] not in (1, 2)
            or provider not in ("claude", "gemini")
            or type(value["enabled"]) is not bool
            or not isinstance(value["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["id"])
            or not isinstance(value["settings"], str) or not Path(value["settings"]).is_absolute()
            or not isinstance(value["entries"], dict) or set(value["entries"]) != set(_events(provider))
            or value["backup"] != value["id"] + ".backup"
            or any(v is not None and (not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v))
                   for v in (value["before"], value["after"]))):
        raise HookError("Invalid installation receipt")
    for group in value["entries"].values():
        if (not isinstance(group, dict) or set(group) != {"hooks"}
                or not isinstance(group["hooks"], list) or len(group["hooks"]) != 1):
            raise HookError("Invalid owned hook entry")
        handler = group["hooks"][0]
        if not _valid_invocation(handler, value):
            raise HookError("Invalid owned hook invocation")
    return value


def _valid_invocation(handler: dict, receipt: dict) -> bool:
    provider = _provider(receipt)
    keys = {"type", "command", "timeout"} | ({"args"} if provider == "claude" else {"name"})
    if (not isinstance(handler, dict) or set(handler) != keys or handler["type"] != "command"
            or type(handler["timeout"]) is not int
            or handler["timeout"] != (2000 if provider == "gemini" else 2)
            or not isinstance(handler["command"], str)):
        return False
    if provider == "gemini":
        if handler["name"] != "openvegas-" + receipt["id"]:
            return False
        try:
            invocation = shlex.split(handler["command"])
        except ValueError:
            return False
        if shlex.join(invocation) != handler["command"]:
            return False
    else:
        if not isinstance(handler["args"], list):
            return False
        invocation = [handler["command"], *handler["args"]]
    if not invocation or not all(isinstance(v, str) and "\0" not in v for v in invocation):
        return False
    if not Path(invocation[0]).is_absolute():
        return False
    tail = ["handle", "--installation", str(_root(Path(receipt["settings"]))),
            "--owner", receipt["id"]]
    prefixes = [["-I", "-m", "openvegas.emotes.hooks"]] if receipt["schema"] == 1 else [
        ["-I", "-m", "openvegas.emotes.hook_dispatch"], ["--openvegas-emote-hook"]]
    return any(invocation[1:] == prefix + tail for prefix in prefixes)


def _config(data: bytes | None, provider: str = "claude") -> dict:
    value = {} if data is None else _json(data, MAX_SETTINGS)
    hooks_value = value.get("hooks", {})
    if not isinstance(hooks_value, dict) or any(
        not isinstance(groups, list) or any(not isinstance(group, dict)
            or not isinstance(group.get("hooks"), list)
            or any(not isinstance(handler, dict) for handler in group["hooks"])
            for group in groups) for key, groups in hooks_value.items()
            if not (provider == "gemini" and key in {"enabled", "disabled", "notifications"})
    ):
        raise HookError("Unsupported hooks settings shape; no changes made")
    if provider == "gemini":
        for policy in (hooks_value, value.get("hooksConfig", {})):
            if not isinstance(policy, dict):
                raise HookError("Unsupported Gemini hook policy")
            for key in ("enabled", "notifications"):
                if key in policy and type(policy[key]) is not bool:
                    raise HookError("Unsupported Gemini hook policy")
            disabled = policy.get("disabled", [])
            if not isinstance(disabled, list) or any(not isinstance(v, str) for v in disabled):
                raise HookError("Unsupported Gemini disabled hook list")
    return value


def _invocation() -> list[str]:
    if not Path(sys.executable).is_absolute() or "\0" in sys.executable:
        raise HookError("This build cannot invoke an absolute hook executable")
    if getattr(sys, "frozen", False):
        return [sys.executable, "--openvegas-emote-hook"]
    return [sys.executable, "-I", "-m", "openvegas.emotes.hook_dispatch"]


def _entries(root: Path, installation_id: str, provider: str = "claude") -> dict:
    # Preserve the venv executable symlink: resolving it would select base Python.
    invocation = _invocation() + ["handle", "--installation", str(root), "--owner", installation_id]
    if provider == "gemini":
        # Gemini expands these placeholders before its shell parses quotes.
        # Reject literal path collisions rather than letting stdin alter argv.
        if any(re.search(r"\$(?:GEMINI_(?:PROJECT_DIR|CWD|PLANS_DIR|SESSION_ID)|CLAUDE_PROJECT_DIR)", arg)
               for arg in invocation):
            raise HookError("Gemini hook paths cannot contain native context placeholders")
        handler = {"type": "command", "command": shlex.join(invocation), "timeout": 2000,
                   "name": "openvegas-" + installation_id}
    else:
        handler = {"type": "command", "command": invocation[0], "args": invocation[1:], "timeout": 2}
    return {event: {"hooks": [copy.deepcopy(handler)]} for event in _events(provider)}


def _probe_handler() -> bool:
    try:
        result = subprocess.run([*_invocation(), "probe"],
                                capture_output=True, stdin=subprocess.DEVNULL, timeout=3, check=False)
        return (result.returncode == 0 and result.stdout == b"openvegas-hook-dispatch-v2\n"
                and result.stderr == b"")
    except (OSError, subprocess.TimeoutExpired):
        return False


def _remove_owned(config: dict, receipt: dict, *, require_all: bool) -> dict:
    """Remove exact handlers, including if another handler joined their group."""
    result = copy.deepcopy(config)
    mapping = result.get("hooks", {})
    for event, expected in receipt["entries"].items():
        wanted = expected["hooks"][0]
        found = 0
        kept_groups = []
        for group in mapping.get(event, []):
            kept_handlers = []
            for handler in group["hooks"]:
                if handler == wanted and {k: v for k, v in group.items() if k != "hooks"} == {}:
                    found += 1
                    continue
                if receipt["id"] in json.dumps(handler) or handler == wanted:
                    raise HookError("An owned hook was edited; refusing to overwrite it")
                kept_handlers.append(handler)
            if kept_handlers:
                kept_groups.append({**group, "hooks": kept_handlers})
            elif not group["hooks"]:
                kept_groups.append(group)
        if found > 1 or (require_all and found != 1):
            raise HookError("Owned hook is missing or duplicated; review settings before retrying")
        if kept_groups:
            mapping[event] = kept_groups
        else:
            mapping.pop(event, None)
    if "hooks" in result and not mapping:
        result.pop("hooks")
    return result


def _replace_settings(path: Path, before: bytes | None, after: bytes | None) -> None:
    """Atomic replace plus change detection; do not edit concurrently with setup."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_info = path.parent.lstat()
    if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid()
            or parent_info.st_mode & 0o022):
        raise HookError("Unsafe settings directory")
    if _read_settings(path) != before:
        raise HookError("Settings changed during operation; retry after closing other editors")
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if after is None:
            if before is not None:
                os.unlink(path.name, dir_fd=fd)
        else:
            if len(after) > MAX_SETTINGS:
                raise HookError("Merged settings exceed size limit")
            atomic_write(fd, path.name, after)
    finally:
        os.close(fd)


def setup(path: Path, *, apply: bool = False, activity_only: bool = False,
          provider: str = "claude", observation_only: bool = False) -> dict:
    """Plan by default. Explicit opt-in and reviewed host version required to write."""
    if provider not in ("claude", "gemini"):
        raise HookError("Lifecycle adapter unsupported; use emote run for whole-process outcomes")
    path = _settings_path(path)
    if provider == "gemini" and (path.name != "settings.json" or path.parent.name != ".gemini"):
        raise HookError("Gemini requires an explicit .gemini/settings.json file")
    before = _read_settings(path)
    config = _config(before, provider)
    root = _root(path)
    previous = _receipt(root)
    if previous and previous["enabled"]:
        if _provider(previous) != provider:
            raise HookError("Settings already belong to a different provider")
        _remove_owned(config, previous, require_all=True)
        return {"changed": False, "applied": False, "settings": str(path), "notice": _notice(provider)}
    version = gemini_version() if provider == "gemini" else claude_version()
    allowed = (_gemini_supported(version) if provider == "gemini" else _supported(version)) and os.name == "posix"
    if config.get("disableAllHooks") is True or config.get("allowManagedHooksOnly") is True:
        raise HookError("Hooks are disabled or managed-only; OpenVegas will not change that policy")
    if provider == "gemini" and any(config.get(k, {}).get("enabled") is False
                                    for k in ("hooks", "hooksConfig")):
        raise HookError("Gemini hooks are disabled; OpenVegas will not change that policy")
    owner = uuid4().hex
    entries = _entries(root, owner, provider)
    opt_in = "--observation-only" if provider == "gemini" else "--activity-only"
    result = {"changed": True, "applied": False, "settings": str(path), "notice": _notice(provider),
              "provider": provider, "mode": "observation-only" if provider == "gemini" else "activity-only",
              "host_version": version, "schema_reviewed": allowed, "hook_patch": entries,
              "requires": opt_in + " --apply; no live lifecycle certification"}
    if not apply:
        return result
    accepted = observation_only if provider == "gemini" else activity_only
    if not accepted or not allowed:
        gate = "Gemini CLI 0.59.0" if provider == "gemini" else "Claude Code 2.1.196-2.1.270"
        raise HookError(f"Apply requires {opt_in} and {gate} on POSIX; "
                        "other versions use emote run (whole-process only)")
    if not _probe_handler():
        raise HookError("The isolated hook module is not available in this interpreter; no settings changed")
    merged = copy.deepcopy(config)
    for event, group in entries.items():
        merged.setdefault("hooks", {}).setdefault(event, []).append(group)
    after = _bytes(merged)
    if len(after) > MAX_SETTINGS:
        raise HookError("Merged settings exceed size limit")
    receipt = {"schema": 2, "provider": provider, "id": owner, "enabled": True, "settings": str(path), "entries": entries,
               "before": None if before is None else _digest(before), "after": _digest(after),
               "backup": owner + ".backup"}
    with _locked(root) as fd:
        # Recheck ownership/config under our lock. A prepared receipt also permits
        # safe uninstall if the process dies between receipt and settings writes.
        try:
            current = _validated_receipt(_json(_read(fd, RECEIPT, MAX_STATE), MAX_STATE))
            if current["enabled"]:
                raise HookError("Another installation is active")
        except FileNotFoundError:
            pass
        if len(_names(fd)) >= MAX_SESSIONS + 64:
            raise HookError("Installation history is full; review backups before installing")
        atomic_write(fd, receipt["backup"], before or b"")
        atomic_write(fd, RECEIPT, _bytes(receipt))
        _replace_settings(path, before, after)
    result.update(applied=True, installation=str(root))
    return result


def uninstall(path: Path, *, apply: bool = False) -> dict:
    path = _settings_path(path)
    root = _root(path)
    receipt = _receipt(root)
    if not receipt or not receipt["enabled"]:
        return {"changed": False, "applied": False, "settings": str(path)}
    before = _read_settings(path)
    config = _config(before, _provider(receipt))
    merged = _remove_owned(config, receipt, require_all=False)
    result = {"changed": True, "applied": False, "settings": str(path),
              "operation": "remove only exact owned hooks; preserve other settings and hooks"}
    if not apply:
        return result
    with _locked(root) as fd:
        current = _validated_receipt(_json(_read(fd, RECEIPT, MAX_STATE), MAX_STATE))
        if current != receipt:
            raise HookError("Installation changed; retry uninstall")
        original = _read(fd, receipt["backup"], MAX_SETTINGS)
        if (receipt["before"] is not None and _digest(original) != receipt["before"]
                or receipt["before"] is None and original != b""):
            raise HookError("Original backup failed integrity check")
        if before is not None and _digest(before) == receipt["after"]:
            after = original if receipt["before"] is not None else None
        else:
            after = None if before is None else _bytes(merged)
        # Keep a recovery copy of intervening settings edits; never blindly restore
        # an old full-file backup over changes made after installation.
        atomic_write(fd, receipt["id"] + ".uninstall-backup", before or b"")
        _replace_settings(path, before, after)
        atomic_write(fd, RECEIPT, _bytes({**receipt, "enabled": False}))
    result["applied"] = True
    return result


def _metadata(data: bytes) -> tuple[str, str | None, str, str | None] | None:
    value = _json(data, MAX_INPUT)
    name, session = value.get("hook_event_name"), value.get("session_id")
    if name not in HOOK_EVENTS or not isinstance(session, str) or not IDENTITY.fullmatch(session):
        return None
    if "agent_id" in value:  # Main session only, never borrowed subagent completion.
        return None
    prompt = value.get("prompt_id")
    if prompt is not None:
        if not isinstance(prompt, str) or str(UUID(prompt)) != prompt:
            return None
    elif name != "SessionEnd":
        return None
    tool = value.get("tool_use_id") if name == "PreToolUse" else None
    if name == "PreToolUse" and (not isinstance(tool, str) or not IDENTITY.fullmatch(tool)):
        return None
    return session, prompt, name, tool


def _validate_state(value: dict, session: str) -> dict:
    if (set(value) != {"schema", "session_id", "active", "turns"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or value["session_id"] != session or not isinstance(value["turns"], dict)
            or len(value["turns"]) > MAX_TURNS):
        raise HookError("Invalid hook state")
    for prompt, turn in value["turns"].items():
        if str(UUID(prompt)) != prompt or not isinstance(turn, dict):
            raise HookError("Invalid turn state")
        if set(turn) != {"generation", "sequence", "quiet", "tools"}:
            raise HookError("Invalid turn state")
        if any(type(turn[k]) is not int or not 0 <= turn[k] < 2**53 - 2
               for k in ("generation", "sequence")) or type(turn["quiet"]) is not bool:
            raise HookError("Invalid ordering state")
        if (not isinstance(turn["tools"], list) or len(turn["tools"]) > 128
                or any(not isinstance(t, str) or not re.fullmatch(r"[0-9a-f]{64}", t)
                       for t in turn["tools"])):
            raise HookError("Invalid deduplication state")
    if value["active"] is not None and value["active"] not in value["turns"]:
        raise HookError("Unknown active turn")
    return value


def handle_input(data: bytes, *, installation: Path, owner: str,
                 spool: EventSpool | None = None) -> bool:
    """Silent, fail-closed activity subset. Never publish COMPLETE or RESUME.

    UserPromptSubmit is the only generation boundary. Native prompt UUIDs and
    persistent tombstones prevent old events finishing or restarting newer work.
    The counter sequences our conservative transitions, not inferred host order.
    """
    try:
        # Receipt chooses the schema, never a provider field in untrusted stdin.
        receipt = _receipt(installation)
        if not receipt or not receipt["enabled"] or receipt["id"] != owner:
            return False
        if _provider(receipt) == "gemini":
            return _observe_gemini(data, installation=installation, receipt=receipt)
        metadata = _metadata(data)
        if metadata is None or not re.fullmatch(r"[0-9a-f]{32}", owner):
            return False
        native_session, prompt, name, tool = metadata
        spool = spool if spool is not None else EventSpool()
        session = "claude-" + _digest((owner + ":" + native_session).encode())[:32]
        filename = session + ".json"
        # Missing installation must not create any directories on incoming data.
        if not installation.is_dir() or installation.is_symlink():
            return False
        with _locked(installation) as fd:
            receipt = _validated_receipt(_json(_read(fd, RECEIPT, MAX_STATE), MAX_STATE))
            if (not receipt["enabled"] or receipt["id"] != owner
                    or _root(Path(receipt["settings"])) != installation):
                return False
            try:
                state = _validate_state(_json(_read(fd, filename, MAX_STATE), MAX_STATE), session)
            except FileNotFoundError:
                if sum(n.startswith("claude-") for n in _names(fd)) >= MAX_SESSIONS:
                    return False
                state = {"schema": 1, "session_id": session, "active": None, "turns": {}}
            turns = state["turns"]
            pending = []

            def emit(pid: str, phase: Phase):
                turn = turns[pid]
                turn["sequence"] += 1
                pending.append(Event("claude", session, pid, uuid4().hex, phase,
                                     turn["generation"], turn["sequence"]))

            if name == "UserPromptSubmit":
                if prompt in turns or len(turns) >= MAX_TURNS:
                    return False
                generation = reserve_generation(spool, source="claude", session_id=session)
                if generation is None:
                    return False
                if state["active"] is not None:
                    old = state["active"]
                    turns[old]["quiet"] = True
                    emit(old, Phase.CANCEL)
                turns[prompt] = {"generation": generation, "sequence": 0, "quiet": False, "tools": []}
                state["active"] = prompt
                emit(prompt, Phase.START)
            elif name == "SessionEnd":
                active = state["active"]
                if active is None or (prompt is not None and prompt != active):
                    return False
                turns[active]["quiet"] = True
                emit(active, Phase.EXIT)
                state["active"] = None
            elif prompt not in turns:
                # A terminal/permission event can beat a delayed start. Tombstone
                # that UUID without synthesizing an active turn or publishing it.
                if name == "PreToolUse" or len(turns) >= MAX_TURNS:
                    return False
                turns[prompt] = {"generation": 0, "sequence": 0, "quiet": True, "tools": []}
            elif prompt != state["active"]:
                return False
            elif name == "PreToolUse":
                turn = turns[prompt]
                token = _digest(tool.encode())
                if turn["quiet"] or token in turn["tools"] or len(turn["tools"]) >= 128:
                    return False
                turn["tools"].append(token)
                emit(prompt, Phase.BUSY)
            else:
                turn = turns[prompt]
                if turn["quiet"]:
                    return False
                turn["quiet"] = True
                emit(prompt, Phase.ERROR if name == "StopFailure" else Phase.PAUSE)
            encoded = _bytes(state)
            if len(encoded) > MAX_STATE:
                return False
            # Write first. Dropped publications cannot be replayed into a success
            # or resurrect a turn after cancellation, restart, or a full queue.
            atomic_write(fd, filename, encoded)
            sent = [spool.publish(event) for event in pending]
            return bool(sent) and all(sent)
    except Exception:  # noqa: BLE001 - cosmetics never block or print into the host
        return False


def _observation(value: dict, session: str) -> dict:
    if (set(value) != {"schema", "session_id", "observed", "closed"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or value["session_id"] != session or type(value["closed"]) is not bool
            or not isinstance(value["observed"], list)
            or any(not isinstance(v, str) or v not in GEMINI_EVENTS for v in value["observed"])
            or len(set(value["observed"])) != len(value["observed"])):
        raise HookError("Invalid Gemini observation state")
    return value


def _observe_gemini(data: bytes, *, installation: Path, receipt: dict) -> bool:
    """Monotonic set of event *kinds*, not turn ordering, activity, or success.

    No per-turn identifier is provided by the reviewed native API. Recording a
    bounded set is idempotent even for rapid turns, retries and delayed delivery.
    SessionEnd is a permanent tombstone; no later event reopens this session.
    """
    value = _json(data, MAX_INPUT)
    name, native_session = value.get("hook_event_name"), value.get("session_id")
    if (name not in GEMINI_EVENTS or not isinstance(native_session, str)
            or not IDENTITY.fullmatch(native_session) or "agent_id" in value):
        return False
    session = "gemini-" + _digest((receipt["id"] + ":" + native_session).encode())[:32]
    filename = session + ".json"
    with _locked(installation) as fd:
        current = _validated_receipt(_json(_read(fd, RECEIPT, MAX_STATE), MAX_STATE))
        if current != receipt:
            return False
        try:
            state = _observation(_json(_read(fd, filename, MAX_STATE), MAX_STATE), session)
        except FileNotFoundError:
            if sum(n.startswith("gemini-") for n in _names(fd)) >= MAX_SESSIONS:
                return False
            state = {"schema": 1, "session_id": session, "observed": [], "closed": False}
        if state["closed"] or name in state["observed"]:
            return False
        state["observed"] = sorted([*state["observed"], name])
        state["closed"] = name == "SessionEnd"
        atomic_write(fd, filename, _bytes(state))
    return True


@click.group()
def hooks():
    """Plan opt-in external hooks; no success adapter is currently certified."""


def _path_option(function):
    return click.option("--settings", type=click.Path(path_type=Path),
                        default=None,
                        help="Settings file; Gemini setup requires an explicit .gemini/settings.json.")(function)


def _default_path(settings):
    return settings if settings is not None else Path.home() / ".claude" / "settings.json"


@hooks.command("setup")
@click.argument("provider", type=click.Choice(["claude", "codex", "gemini"]))
@_path_option
@click.option("--apply", is_flag=True, help="Write reviewed hooks and a private original backup.")
@click.option("--activity-only", is_flag=True, help="Accept the activity-only limitations; required to apply.")
@click.option("--observation-only", is_flag=True, help="Accept Gemini session discovery with no animation.")
def setup_command(provider, settings, apply, activity_only, observation_only):
    """Show a patch by default. Never overwrites other providers' configuration."""
    if provider == "codex":
        raise click.ClickException("Lifecycle adapter unsupported; use emote run for whole-process outcomes")
    if provider == "gemini" and settings is None:
        raise click.ClickException("Gemini requires explicit --settings /path/to/.gemini/settings.json")
    try:
        result = setup(_default_path(settings), apply=apply, activity_only=activity_only,
                       provider=provider, observation_only=observation_only)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(json.dumps(result, indent=2))


@hooks.command("uninstall")
@_path_option
@click.option("--apply", is_flag=True, help="Remove only exact entries owned by this installation.")
def uninstall_command(settings, apply):
    """Dry-run by default; preserve subsequent user edits and original backups."""
    try:
        result = uninstall(_default_path(settings), apply=apply)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(json.dumps(result, indent=2))


@hooks.command("status")
@_path_option
def status_command(settings):
    """Report configuration, not live-host certification or rendering success."""
    try:
        path = _settings_path(_default_path(settings))
        receipt = _receipt(_root(path))
        configured = bool(receipt and receipt["enabled"])
        provider = _provider(receipt) if receipt else ("gemini" if path.parent.name == ".gemini" else "claude")
        config = _config(_read_settings(path), provider)
        if configured:
            _remove_owned(config, receipt, require_all=True)
        version = gemini_version() if provider == "gemini" else claude_version()
        disabled = (config.get("disableAllHooks") is True or config.get("allowManagedHooksOnly") is True
                    or (provider == "gemini" and any(config.get(k, {}).get("enabled") is False
                                                    for k in ("hooks", "hooksConfig"))))
        disabled_owned = False
        if configured and provider == "gemini":
            handler = next(iter(receipt["entries"].values()))["hooks"][0]
            disabled_owned = any(handler["name"] in config.get(k, {}).get("disabled", [])
                                 or handler["command"] in config.get(k, {}).get("disabled", [])
                                 for k in ("hooks", "hooksConfig"))
        click.echo(json.dumps({"configured": configured, "version": version, "provider": provider,
                               "policy_disabled": disabled or disabled_owned,
                               "mode": "observation-only" if provider == "gemini" else "activity-only",
                               "scope": "selected settings file only; other policy layers not inspected",
                               "schema_reviewed": _gemini_supported(version) if provider == "gemini" else _supported(version),
                               "lifecycle_certified": False, "notice": _notice(provider)}))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None


@hooks.command("sessions")
@_path_option
def sessions_command(settings):
    """List opaque watch IDs only. No transcript discovery or terminal writes."""
    try:
        root = _root(_settings_path(_default_path(settings)))
        sessions = []
        receipt = _receipt(root)
        if receipt and receipt["enabled"]:
            with _readonly(root) as fd:
                for name in _names(fd):
                    if _provider(receipt) == "claude" and re.fullmatch(r"claude-[0-9a-f]{32}\.json", name):
                        state = _validate_state(_json(_read(fd, name, MAX_STATE), MAX_STATE), name[:-5])
                        sessions.append({"session": state["session_id"],
                                         "watch": f"openvegas emote watch --source claude --session {state['session_id']}"})
                    elif _provider(receipt) == "gemini" and re.fullmatch(r"gemini-[0-9a-f]{32}\.json", name):
                        state = _observation(_json(_read(fd, name, MAX_STATE), MAX_STATE), name[:-5])
                        sessions.append({"session": state["session_id"], "mode": "observation-only",
                                         "observed": state["observed"], "closed": state["closed"]})
        notice = GEMINI_NOTICE if receipt and _provider(receipt) == "gemini" else "Run watch in a separate terminal/pane."
        click.echo(json.dumps({"sessions": sessions, "notice": notice}))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None


@hooks.command("handle", hidden=True)
@click.option("--installation", required=True, type=click.Path(path_type=Path))
@click.option("--owner", required=True)
def handle_command(installation, owner):
    try:
        handle_input(read_input(), installation=installation, owner=owner)
    except Exception:  # noqa: BLE001 - stdin errors are cosmetic only
        return


@hooks.command("probe", hidden=True)
def probe_command():
    click.echo("openvegas-hooks-v1")


if __name__ == "__main__":
    # Even malformed generated-handler arguments must not change host control flow
    # or put usage text/tracebacks in a model's context. Interactive setup retains
    # ordinary Click diagnostics.
    if len(sys.argv) > 1 and sys.argv[1] == "handle":
        try:
            hooks(standalone_mode=False)
        except (Exception, KeyboardInterrupt):  # noqa: BLE001 - no hook errors in model context
            sys.exit(0)
    else:
        hooks()
