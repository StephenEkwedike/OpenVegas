"""Opt-in VS Code macOS terminal profile, not a wrapper for either coding host."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .animation_demo import prepare_sixel_terminfo
from .codex_pets import _directory, _publish

PROFILE = "OpenVegas Graphics"
GAME_PROFILE = "OpenVegas Games"
OWNER = "openvegas.native-terminal.v1"
DEFAULT_KEY = "terminal.integrated.defaultProfile.osx"


def profile_spec(root, *, game_handoff=True, account=None):
    # VS Code injects TERM_PROGRAM after profile.env. env unsets it before zsh
    # starts. The relay owns display switching, not the host's agent or billing.
    shell = ["/bin/zsh", "-l"]
    if game_handoff:
        if getattr(sys, "frozen", False):
            shell = [sys.executable, "--_terminal-relay", "--", *shell]
        else:
            source = str(Path(__file__).resolve().parents[2])
            shell = [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, " + repr(source) +
                     "); from openvegas.integrations.terminal_relay import main; main()", "--", *shell]
    spec = {
        "path": "/usr/bin/env",
        "args": ["-u", "TERM_PROGRAM", "-u", "TERM_PROGRAM_VERSION",
                 "TERM=openvegas-sixel", "TERMINFO=" + str(root), *shell],
        "overrideName": True,
    }
    if account:
        # Match the account used to install the plugin, not whichever global
        # CLI login happens to be current when a terminal is relaunched.
        spec['env'] = {'OPENVEGAS_BACKEND_URL': account[0],
                       'OPENVEGAS_INTEGRATION_PROFILE': str(account[1])}
    return spec


def _write_settings(settings, original, proposed):
    # Back up privately beside the user's file, never in repo evidence.
    backup = settings.with_name("settings.json.openvegas-graphics-backup")
    if not backup.exists():
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(original)
    fd, temporary = tempfile.mkstemp(prefix=".ov-settings-", dir=settings.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(proposed, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        if (settings.read_bytes() if settings.exists() else b"{}\n") != original:
            raise ValueError("Settings changed during setup; retry after the other editor finishes")
        os.replace(temporary, settings)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure(home: Path, env: dict, *, apply=False, make_default=False, restore_default=False, game_handoff=False):
    """Add one optional profile; preserve the default, other profiles and backups."""
    settings = home / "Library/Application Support/Code/User/settings.json"
    root = home / "Library/Application Support/OpenVegas/native-terminal"
    saved_default = root / "default-profile.json"
    profile = GAME_PROFILE if game_handoff else PROFILE
    if make_default and restore_default:
        raise ValueError("Choose make-default or restore-default, not both")
    # Never traverse a redirected settings/profile directory.
    for path in (settings, root, saved_default):
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("Symlinked configuration is not modified")
    if not settings.parent.is_dir():
        raise ValueError("VS Code user settings directory is missing")
    original = settings.read_bytes() if settings.exists() else b"{}\n"
    try:
        data = json.loads(original)
    except ValueError:
        raise ValueError("Settings use JSON comments or invalid JSON; preserve them and configure the profile manually") from None
    if not isinstance(data, dict):
        raise ValueError("Settings must be an object")
    profiles = data.get("terminal.integrated.profiles.osx", {})
    if not isinstance(profiles, dict):
        raise ValueError("Terminal profiles must be an object")
    from .credentials import backend_url, profile as account_profile
    desired = profile_spec(root, account=(backend_url(), account_profile()))
    legacy = profile_spec(root, game_handoff=False)
    unbound = profile_spec(root)
    owned_legacy = (profiles.get(profile) in (legacy, unbound) and
                    (root / "owner.json").is_file() and
                    (root / "owner.json").read_text() == json.dumps({"owner": OWNER}) + "\n")
    if profile in profiles and profiles[profile] != desired and not owned_legacy:
        raise ValueError("Existing OpenVegas Graphics profile was customized; not overwritten")
    if root.exists() and (not root.is_dir() or
                          (root / "owner.json").read_text() != json.dumps({"owner": OWNER}) + "\n"):
        raise ValueError("Existing terminal profile files are not owned by OpenVegas")
    proposed = {**data, "terminal.integrated.enableImages": True,
                "terminal.integrated.gpuAcceleration": "on",
                "terminal.integrated.profiles.osx": {**profiles, profile: desired}}
    previous = None
    if restore_default:
        if not saved_default.exists():
            raise ValueError("No previous default saved by OpenVegas; settings unchanged")
        previous = json.loads(saved_default.read_text())
        if (not isinstance(previous, dict) or previous.get("owner") != OWNER or
                not isinstance(previous.get("present"), bool) or
                previous.get("value") is not None and not isinstance(previous["value"], str)):
            raise ValueError("Invalid saved default; settings unchanged")
        if data.get(DEFAULT_KEY) != profile:
            raise ValueError("The default was changed after OpenVegas setup; preserving your newer choice")
        proposed = dict(data)
        if previous["present"]:
            proposed[DEFAULT_KEY] = previous["value"]
        else:
            proposed.pop(DEFAULT_KEY, None)
    elif make_default:
        proposed[DEFAULT_KEY] = profile
    if not apply:
        return {"settings": str(settings), "profile": profile, "applied": False,
                "is_default": data.get(DEFAULT_KEY) == profile}
    root.parent.mkdir(parents=True, exist_ok=True)
    if not root.exists():
        with _directory(root.parent) as parent:
            with tempfile.TemporaryDirectory(prefix=".terminal-", dir=root.parent) as tmp:
                prepare_sixel_terminfo(tmp, env)
                (Path(tmp) / "owner.json").write_text(json.dumps({"owner": OWNER}) + "\n")
                _publish(parent, Path(tmp).name, root.name)
    result = subprocess.run(["infocmp", "openvegas-sixel"],
                            env={**env, "TERMINFO": str(root)}, capture_output=True, timeout=5)
    if result.returncode:
        raise ValueError("Persistent terminfo could not be verified; settings unchanged")
    saved_new_default = False
    if make_default and data.get(DEFAULT_KEY) != profile:
        if saved_default.exists():
            raise ValueError("A prior default is already saved; restore it before changing the default again")
        fd = os.open(saved_default, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"owner": OWNER, "present": DEFAULT_KEY in data,
                       "value": data.get(DEFAULT_KEY)}, stream)
        saved_new_default = True
    try:
        if proposed != data:
            _write_settings(settings, original, proposed)
    except BaseException:
        if saved_new_default:
            saved_default.unlink(missing_ok=True)
        raise
    if restore_default:
        saved_default.unlink()
    return {"settings": str(settings), "profile": profile, "applied": True,
            "is_default": proposed.get(DEFAULT_KEY) == profile}
