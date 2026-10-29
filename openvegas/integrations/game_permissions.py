"""Opt-in permission for the human game menu only, never all host tools."""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import tempfile
import tomllib
from pathlib import Path

from openvegas.emotes.spool import _locked, _read, atomic_write

from .credentials import IntegrationError, profile
from .installer import (
    _verified_previous,
    codex_policy_bytes,
    detect,
    host_environment,
    identity,
    install,
    registration,
)

CLAUDE_PLAY = "mcp__plugin_openvegas_openvegas__play"


def _safe(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise IntegrationError("Symlinked host settings are preserved; game permission unchanged.")
    if path.exists() and (not path.is_file() or path.stat().st_size > 2_000_000):
        raise IntegrationError("Unsupported host settings; game permission unchanged.")


def _replace(path, before, after):
    _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".openvegas-game-entry-backup")
    _safe(backup)
    if not backup.exists():
        with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(before)
    fd, temporary = tempfile.mkstemp(prefix=".openvegas-game-entry-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        _safe(path)
        if (path.read_bytes() if path.exists() else b"") != before:
            raise IntegrationError("Host settings changed during setup; retry after the editor finishes.")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure(host, *, apply=False, restore=False, env=None):
    """Only approved setup calls this; existing explicit restrictions win."""
    info = detect(host)
    if not info:
        return {"host": host, "status": "unsupported", "message": "Host not found; permissions unchanged."}
    environment = host_environment(env) or os.environ
    home = Path(environment.get("HOME", str(Path.home())))
    marketplace, installed_directory = identity(host)
    plugin = "openvegas@" + marketplace
    actual = registration(info, marketplace, env=environment)
    if not restore and not all(actual[k] for k in ("registered", "installed", "enabled")):
        return {"host": host, "status": "preserved", "message": "Install/enable the owned plugin first."}
    if not restore:
        try:
            with _locked(installed_directory) as installed:
                previous = json.loads(_read(installed, "installed.json", 16384))
                _verified_previous(previous, installed_directory, marketplace, actual)
        except (FileNotFoundError, ValueError):
            raise IntegrationError("No valid owned plugin receipt; permissions unchanged.") from None
    if not restore and host == "codex" and tuple(map(int, info["version"].split("."))) < (0, 164, 0):
        return {"host": host, "status": "unsupported", "message": "This Codex version has not been verified for per-plugin game permissions."}
    path = (Path(environment.get("CLAUDE_CONFIG_DIR", str(home / ".claude"))) / "settings.json"
            if host == "claude" else Path(environment.get("CODEX_HOME", str(home / ".codex"))) / "config.toml")
    path = path.expanduser().absolute()
    _safe(path)
    key = hashlib.sha256((host + str(path)).encode()).hexdigest() + ".json"
    with _locked(profile() / "integrations" / "game-permissions") as directory:
        try:
            receipt = json.loads(_read(directory, key, 16384))
        except FileNotFoundError:
            receipt = {}
        before = path.read_bytes() if path.exists() else b""
        try:
            data = (json.loads(before or b"{}") if host == "claude" else tomllib.loads(before.decode()))
            if not isinstance(data, dict):
                raise TypeError()
            proposed = copy.deepcopy(data)
            if host == "claude":
                permissions = proposed.setdefault("permissions", {})
                if not isinstance(permissions, dict) or any(
                    not isinstance(permissions.get(k, []), list) or
                    not all(isinstance(r, str) for r in permissions.get(k, []))
                    for k in ("allow", "ask", "deny")
                ):
                    raise ValueError()
                rules = permissions.setdefault("allow", [])
                if restore:
                    if receipt.get("rule") != CLAUDE_PLAY or CLAUDE_PLAY not in rules:
                        return {"host": host, "status": "unchanged"}
                    rules.remove(CLAUDE_PLAY)
                else:
                    restrictions = permissions.get("ask", []) + permissions.get("deny", [])
                    if any(fnmatch.fnmatchcase(CLAUDE_PLAY, rule) or
                           CLAUDE_PLAY.startswith(rule + "__") for rule in restrictions):
                        return {"host": host, "status": "preserved", "message": "Existing ask/deny rule preserved."}
                    if CLAUDE_PLAY in rules:
                        return {"host": host, "status": "unchanged"}
                    rules.append(CLAUDE_PLAY)
                after = (json.dumps(proposed, indent=2, ensure_ascii=False) + "\n").encode()
                owned = {"rule": CLAUDE_PLAY}
            else:
                block = ("\n# OpenVegas: human game-entry tool only.\n[plugins." + json.dumps(plugin) +
                         '.mcp_servers.openvegas.tools.play]\napproval_mode = "approve"\n')
                text = before.decode()
                if restore:
                    config = proposed.get('plugins', {}).get(plugin, {})
                    server = config.get('mcp_servers', {}).get('openvegas', {})
                    tool_settings = server.get('tools', {})
                    if receipt.get('block') != block or tool_settings.get('play') != {'approval_mode': 'approve'}:
                        return {"host": host, "status": "preserved", "message": "No unchanged owned permission to restore."}
                    del tool_settings['play']
                    if not tool_settings:
                        server.pop('tools')
                    if not server:
                        config['mcp_servers'].pop('openvegas')
                    if not config['mcp_servers']:
                        config.pop('mcp_servers')
                    after = codex_policy_bytes(before, plugin, config)
                else:
                    config = proposed.setdefault("plugins", {}).setdefault(plugin, {})
                    server = config.setdefault("mcp_servers", {}).setdefault("openvegas", {})
                    tools = server.setdefault("tools", {})
                    if tools.get("play", {}).get("approval_mode") == "approve":
                        if apply:
                            install('codex', upgrade=True)
                        return {"host": host, "status": "unchanged"}
                    if (config.get("enabled") is False or server.get("enabled") is False or "play" in tools or
                            "play" in server.get("disabled_tools", []) or
                            ("enabled_tools" in server and "play" not in server["enabled_tools"]) or
                            server.get("default_tools_approval_mode") in {"prompt", "deny"}):
                        return {"host": host, "status": "preserved", "message": "Existing game-tool policy preserved."}
                    tools["play"] = {"approval_mode": "approve"}
                    after = (text + block).encode()
                    if tomllib.loads(after.decode()) != proposed:
                        raise ValueError()
                owned = {"block": block}
        except (ValueError, TypeError, AttributeError):
            raise IntegrationError("Host settings could not be safely updated; preserved unchanged.") from None
        if not apply:
            return {"host": host, "status": "planned", "tool": "play", "restore": restore}
        # Receipt contains only the exact owned rule, never the host's settings.
        # Write it first so a crash after the settings update remains reversible.
        if not restore:
            atomic_write(directory, key, json.dumps(owned).encode())
        try:
            _replace(path, before, after)
        except (IntegrationError, OSError):
            # A failed compare-and-swap must not claim a later user-authored rule.
            if not restore and (path.read_bytes() if path.is_file() else b"") != after:
                atomic_write(directory, key, json.dumps(receipt).encode())
            raise
        if restore:
            atomic_write(directory, key, b"{}")
        if host == 'codex' and actual.get('installed'):
            install('codex', upgrade=True)
        return {"host": host, "status": "restored" if restore else "updated", "tool": "play",
                "message": "Only game-entry approval changed. Host trust and managed restrictions still apply. Restart the host to load it."}
