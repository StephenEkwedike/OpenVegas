"""Generate private local plugin distributions; host CLIs own their configuration."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tomllib
from pathlib import Path

from .credentials import IntegrationError, backend_url, profile
from .controls import ALIASES, action_catalog, help_text, skill_controls

HOSTS = ("claude", "codex")
ASSETS = Path(__file__).parent / "assets"


def host_environment(env=None):
    if env is not None or not os.getenv("OPENVEGAS_HOST_HOME"):
        return env
    # Reuse the native host's profile without forwarding local backend/provider
    # credentials, test billing settings, or the isolated OpenVegas HOME.
    result = {key: os.environ[key] for key in (
        "PATH", "TERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "COLORTERM",
        "TERMINFO", "TERMINFO_DIRS", "TMUX", "TMUX_PANE", "STY", "ZELLIJ",
        "ZELLIJ_SESSION_NAME", "ZELLIJ_VERSION", "KITTY_WINDOW_ID", "WEZTERM_PANE",
        "WEZTERM_EXECUTABLE", "WEZTERM_VERSION", "GHOSTTY_RESOURCES_DIR",
        "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "WINDIR",
        "OPENVEGAS_TERMINAL_SOCKET", "OPENVEGAS_TERMINAL_TOKEN",
    ) if key in os.environ}
    result["HOME"] = result["USERPROFILE"] = os.environ["OPENVEGAS_HOST_HOME"]
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME"):
        if os.getenv("OPENVEGAS_HOST_" + name):
            result[name] = os.environ["OPENVEGAS_HOST_" + name]
    return result


def detect(host):
    executable = shutil.which(host)
    if host not in HOSTS or not executable:
        return None
    try:
        result = subprocess.run([executable, "--version"], capture_output=True,
                                text=True, timeout=10, stdin=subprocess.DEVNULL,
                                env=host_environment(), check=False)
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout)
        if result.returncode or not match:
            return None
        version = tuple(int(n) for n in match.groups())
        return {"host": host, "executable": executable, "version": ".".join(match.groups()),
                "mod": host == "claude" and version >= (2, 1, 289)}
    except (OSError, subprocess.TimeoutExpired):
        return None


def helper(host):
    # Pin the installed source/runtime so a project cwd cannot shadow OpenVegas.
    if getattr(sys, "frozen", False):
        command = [sys.executable, "integration", "serve"]
    else:
        root = str(Path(__file__).resolve().parents[2])
        # The native host keeps its real HOME, but this helper must retain the
        # CLI account selected during setup (including the isolated dev profile).
        home = str(Path.home())
        command = [sys.executable, "-I", "-c", "import os, sys; os.environ['HOME'] = " + repr(home) +
                   "; os.environ['USERPROFILE'] = " + repr(home) + "; sys.path.insert(0, " + repr(root) +
                   "); from openvegas.integrations.mcp_server import main; main()"]
    return command + ["--host", host, "--backend", backend_url(), "--profile", str(profile())]


def host_command(host, *, env=None):
    """Same installed runtime, but retain the shell's native HOME and auth."""
    if getattr(sys, 'frozen', False):
        command = [sys.executable, 'integration', 'serve']
    else:
        root = str(Path(__file__).resolve().parents[2])
        command = [sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, ' + repr(root) +
                   '); from openvegas.integrations.mcp_server import main; main()']
    account_env = env or {}
    backend = account_env.get('OPENVEGAS_BACKEND_URL') or backend_url()
    account_profile = account_env.get('OPENVEGAS_INTEGRATION_PROFILE') or str(profile())
    return command + ['--host', host, '--backend', backend, '--profile', account_profile, '--supervise']


def companion_art():
    # Reuse the approved public pack, its parser and its terminal cell renderer.
    from openvegas.emotes.render import _cells
    from openvegas.emotes.resources import PackRepository
    pack = PackRepository().load("beat-maker")
    frames = []
    for index in pack.manifest.animations["waiting"].frames:
        numbers = []
        for glyph, fg, bg in _cells(pack.frame(index)):
            if glyph != "\n":
                numbers.extend([ord(glyph), int(fg[1:], 16) if fg else 0x1000000,
                                int(bg[1:], 16) if bg else 0x1000000])
        frames.append(base64.b64encode(struct.pack("<" + "I" * len(numbers), *numbers)).decode())
    return {"columns": pack.manifest.width, "rows": (pack.manifest.height + 1) // 2,
            "frames": frames, "pack_id": pack.manifest.pack_id, "access": "public_preview"}


def mod_source(command, site):
    graphics = "import { GAME_ART } from './game-art-module.js';\n" + (ASSETS / "game-graphics.js").read_text(encoding="utf-8")
    return graphics + (ASSETS / "register.js").read_text(encoding="utf-8").replace(
        "__OPENVEGAS_HELPER__", json.dumps(command)).replace(
        "__OPENVEGAS_SITE__", json.dumps(site)).replace(
        "__OPENVEGAS_CONTROLS__", json.dumps({"actions": action_catalog(), "aliases": ALIASES,
                                             "help": help_text()}))


def codex_game_entry_opt_in(marketplace):
    """Reflect explicit host policy; never ship an unconditional permission grant."""
    environment = host_environment() or os.environ
    home = Path(environment.get('HOME', str(Path.home())))
    path = Path(environment.get('CODEX_HOME', str(home / '.codex'))) / 'config.toml'
    try:
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            return False
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        plugin = data.get('plugins', {}).get('openvegas@' + marketplace, {})
        server = plugin.get('mcp_servers', {}).get('openvegas', {})
        return (plugin.get('enabled', True) is True and server.get('enabled', True) is True
                and server.get('default_tools_approval_mode') not in {'prompt', 'writes', 'deny'}
                and 'play' not in server.get('disabled_tools', [])
                and ('enabled_tools' not in server or 'play' in server['enabled_tools'])
                and server.get('tools', {}).get('play', {}).get('approval_mode') == 'approve')
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def codex_policy_snapshot(plugin, env):
    environment = host_environment(env) or os.environ
    home = Path(environment.get('HOME', str(Path.home())))
    path = Path(environment.get('CODEX_HOME', str(home / '.codex'))) / 'config.toml'
    from .game_permissions import _safe
    _safe(path)
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return path, data.get('plugins', {}).get(plugin)


def codex_policy_bytes(before, plugin, policy):
    """Re-encode only the owned plugin namespace, preserving all other TOML."""
    data = tomllib.loads(before.decode())

    def value(v):
        if isinstance(v, dict):
            return '{' + ', '.join(json.dumps(k) + ' = ' + value(x) for k, x in v.items()) + '}'
        if isinstance(v, list):
            return '[' + ', '.join(value(x) for x in v) + ']'
        return json.dumps(v, allow_nan=False)

    text = before.decode()
    headers = list(re.finditer(r'^\[.*$', text, re.MULTILINE))
    spans = []
    for i, match in enumerate(headers):
        try:
            probe = tomllib.loads(match.group() + '\n__ov_marker = true\n')
        except ValueError:
            continue
        if list(probe) == ['plugins'] and list(probe['plugins']) == [plugin]:
            end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
            spans.append((match.start(), end))
    if not spans:
        raise IntegrationError('Cannot safely locate the owned Codex policy; settings preserved.')
    for start, end in reversed(spans):
        comments = ''.join(line for line in text[start:end].splitlines(keepends=True)
                           if line.lstrip().startswith('#'))
        text = text[:start] + comments + text[end:]
    if policy:
        text += '\n[plugins.' + json.dumps(plugin) + ']\n' + ''.join(json.dumps(k) + ' = ' + value(v) + '\n' for k, v in policy.items())
        data['plugins'][plugin] = policy
    else:
        del data['plugins'][plugin]
    after = text.encode()
    if tomllib.loads(after.decode()) != data:
        raise IntegrationError('Codex policy round-trip failed; settings preserved.')
    return after


def restore_codex_policy(snapshot, plugin):
    """Codex remove/add deletes this plugin's policy; restore only that namespace."""
    path, policy = snapshot
    if not policy:
        return
    from .game_permissions import _replace
    before = path.read_bytes()
    current = tomllib.loads(before.decode()).get('plugins', {}).get(plugin)
    if current == policy:
        return
    if current != {'enabled': True}:
        raise IntegrationError('Codex plugin policy changed during update; new settings preserved.')
    _replace(path, before, codex_policy_bytes(before, plugin, policy))


def bundle_files(info, marketplace):
    host = info["host"]
    cmd = helper(host)
    mcp = {"mcpServers": {"openvegas": {"type": "stdio", "command": cmd[0], "args": cmd[1:]}}}
    metadata = {"name": "openvegas", "version": "0.1.1", "author": {"name": "OpenVegas"}, "description":
                "Existing OpenVegas wallet and cosmetics inside your native coding host."}
    files = {"plugins/openvegas/skills/openvegas/SKILL.md": (ASSETS / "SKILL.md").read_text(encoding="utf-8").replace(
        "__OPENVEGAS_CONTROLS_TABLE__", skill_controls())}

    def put(name, value):
        files[name] = json.dumps(value, indent=2) + "\n"

    import shlex
    from .use import MISSING
    node = shutil.which('node')
    hook_command = (shlex.join([node, '-e', (ASSETS / 'billing-hook.js').read_text().replace(
        '__OPENVEGAS_MISSING__', json.dumps(MISSING)), host, '--billing-hook'])
        if node else shlex.join(cmd + ['--billing-hook']))
    billing_hooks = {'hooks': {event: [{'hooks': [{'type': 'command',
        'command': hook_command, 'timeout': 5 if node else 15}]}]
        for event in ('SessionStart', 'UserPromptSubmit')}}
    put('plugins/openvegas/hooks/hooks.json', billing_hooks)

    if host == "claude":
        put(".claude-plugin/marketplace.json", {"name": marketplace, "owner": {"name": "OpenVegas"},
            "plugins": [{"name": "openvegas", "source": "./plugins/openvegas"}]})
        put("plugins/openvegas/.claude-plugin/plugin.json", metadata)
        put("plugins/openvegas/.mcp.json", mcp)
        if info["mod"]:
            # A Mod command owns /openvegas; avoid a skill with the same name.
            files["plugins/openvegas/skills/openvegas-help/SKILL.md"] = files.pop(
                "plugins/openvegas/skills/openvegas/SKILL.md").replace("name: openvegas\n", "name: openvegas-help\n")
            put("plugins/openvegas/hooks/hooks.json", {**billing_hooks, "modules": ["./register.js"]})
            files["plugins/openvegas/hooks/register.js"] = mod_source(cmd, backend_url())
            files["plugins/openvegas/hooks/pet.js"] = (ASSETS / "pet.js").read_text(encoding="utf-8")
            files["plugins/openvegas/hooks/game-table.js"] = (ASSETS / "game-table.js").read_text(encoding="utf-8")
            for asset in ASSETS.glob('game-art-*.js'):
                files['plugins/openvegas/hooks/' + asset.name] = asset.read_text(encoding="utf-8")
            # Private equipped frames are fetched at runtime, never bundled.
    else:
        put(".agents/plugins/marketplace.json", {"name": marketplace,
            "interface": {"displayName": "OpenVegas local integration"}, "plugins": [{
                "name": "openvegas", "source": {"source": "local", "path": "./plugins/openvegas"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity"}]})
        # Codex 0.163 loads portable skills but ignores the portable stdio MCP
        # declaration. Use its supported compatibility layout, without a root
        # manifest that would shadow this one.
        put("plugins/openvegas/.codex-plugin/plugin.json", {
            **metadata, "skills": "./skills/", "mcpServers": "./.mcp.json", "hooks": "./hooks/hooks.json"})
        # A human game break can exceed Codex's default 60-second tool limit.
        # Bound this one server's call time; never change other MCP servers.
        codex_mcp = {"mcpServers": {"openvegas": {"command": cmd[0], "args": cmd[1:],
                                                  "tool_timeout_sec": 900,
                                                  "env_vars": ["OPENVEGAS_TERMINAL_SOCKET", "OPENVEGAS_TERMINAL_TOKEN",
                                                               "OPENVEGAS_NATIVE_TOKEN"]}}}
        if codex_game_entry_opt_in(marketplace):
            # Codex intersects the user setting with the plugin declaration.
            # Only the user's explicit play opt-in may widen this one tool.
            codex_mcp['mcpServers']['openvegas']['tools'] = {'play': {'approval_mode': 'approve'}}
        put("plugins/openvegas/.mcp.json", codex_mcp)
    return files


def identity(host):
    suffix = hashlib.sha256((backend_url() + str(profile())).encode()).hexdigest()[:12]
    return "openvegas-local-" + suffix, profile() / "integrations" / "hosts" / host


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(text)
    path.chmod(0o600)


def _run(cmd, *, env=None):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                                stdin=subprocess.DEVNULL, env=host_environment(env), check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise IntegrationError("Host plugin command failed or timed out; inspect the host's plugin list before retrying.") from None
    if result.returncode:
        # Host stderr may contain local account information; don't echo it through an agent tool.
        raise IntegrationError(f"{Path(cmd[0]).name} plugin command failed (exit {result.returncode}). "
                               "Review its plugin settings; existing unrelated plugins were not edited.")
    return result


def registration(info, marketplace, *, env=None):
    """Ask the host, rather than assuming our receipt still matches its settings."""
    try:
        markets = json.loads(_run([info["executable"], "plugin", "marketplace", "list", "--json"], env=env).stdout)
        plugins = json.loads(_run([info["executable"], "plugin", "list", "--json"], env=env).stdout)
        if info["host"] == "codex":
            markets, plugins = markets["marketplaces"], plugins["installed"]
        if not isinstance(markets, list) or not isinstance(plugins, list):
            raise TypeError
        market = next((v for v in markets if v.get("name") == marketplace), {})
        plugin = next((v for v in plugins if v.get("id", v.get("pluginId")) == "openvegas@" + marketplace), {})
        return {"registered": bool(market), "installed": bool(plugin),
                "enabled": plugin.get("enabled") is True,
                "path": market.get("path", market.get("root")),
                "plugin_path": plugin.get("readFromFolder", (plugin.get("source") or {}).get("path"))}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise IntegrationError("Could not verify the host's plugin registrations; no configuration was changed.") from None


def _matches(actual, root):
    return ((not actual["registered"] or actual["path"] == str(root)) and
            (not actual["installed"] or actual["plugin_path"] == str(root / "plugins/openvegas")))


def _verified_previous(previous, directory, marketplace, actual):
    fingerprint = previous.get("fingerprint", "")
    root = directory / fingerprint[:16]
    if (not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
            or previous.get("marketplace") != marketplace or previous.get("path") != str(root)
            or not _matches(actual, root)
            or not all(actual.get(k) for k in ("registered", "installed", "enabled"))):
        raise IntegrationError("The existing integration no longer matches its receipt. "
                               "Host settings were preserved; inspect them before updating.")
    paths = list(root.rglob("*"))
    if root.is_symlink() or any(p.is_symlink() for p in paths):
        raise IntegrationError("Existing bundle contains symlinks; refusing to update it.")
    files = {str(p.relative_to(root)).replace(os.sep, "/"): p.read_text(encoding="utf-8") for p in paths if p.is_file()}
    if hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest() != fingerprint:
        raise IntegrationError("Existing bundle was modified; refusing to replace it.")
    return root


def _remove_registration(info, marketplace, actual, *, env=None):
    exe, host = info["executable"], info["host"]
    if actual["installed"]:
        _run([exe, "plugin", "uninstall" if host == "claude" else "remove", "openvegas@" + marketplace], env=env)
    if actual["registered"]:
        _run([exe, "plugin", "marketplace", "remove", marketplace], env=env)


def other_installations(host):
    """Find verified OpenVegas installs which would shadow a new account setup."""
    info = detect(host)
    if not info:
        return []
    current, _ = identity(host)
    try:
        markets = json.loads(_run([info['executable'], 'plugin', 'marketplace', 'list', '--json']).stdout)
        if host == 'codex':
            markets = markets['marketplaces']
        result = []
        for market in markets:
            name = market.get('name', '')
            if name == current or not re.fullmatch(r'openvegas-local-[a-f0-9]{12}', name):
                continue
            actual = registration(info, name)
            if not actual['installed'] or not actual['enabled']:
                continue
            root = Path(actual['path'])
            receipt = root.parent / 'installed.json'
            if (not root.is_absolute() or any(p.is_symlink() for p in (receipt, *receipt.parents))
                    or receipt.stat().st_size > 16384
                    or os.name == 'posix' and receipt.stat().st_uid != os.getuid()):
                raise IntegrationError('Existing OpenVegas setup cannot be safely verified; settings preserved.')
            previous = json.loads(receipt.read_text())
            _verified_previous(previous, root.parent, name, actual)
            if previous.get('host') != host:
                raise IntegrationError('Existing OpenVegas host receipt does not match; settings preserved.')
            result.append({'host': host, 'marketplace': name, 'receipt': str(receipt)})
        return result
    except (OSError, ValueError, KeyError, TypeError):
        raise IntegrationError('Could not verify an older OpenVegas setup; settings preserved.') from None


def retire_installation(plan):
    """After replacement succeeds, remove only the verified old registration."""
    from openvegas.emotes.spool import _locked, _read, atomic_write, state_unlink
    directory = Path(plan['receipt']).parent
    info = detect(plan['host'])
    if not info:
        raise IntegrationError('Host disappeared during setup; old registration preserved.')
    with _locked(directory) as fd:
        raw = _read(fd, 'installed.json', 16384)
        previous = json.loads(raw)
        actual = registration(info, plan['marketplace'])
        _verified_previous(previous, directory, plan['marketplace'], actual)
        _remove_registration(info, plan['marketplace'], actual)
        remaining = registration(info, plan['marketplace'])
        if remaining['registered'] or remaining['installed']:
            raise IntegrationError('Old OpenVegas registration is still active; restart only after inspecting plugin status.')
        atomic_write(fd, 'uninstalled.json', raw)
        state_unlink(fd, 'installed.json')


def install(host, *, dry_run=False, upgrade=False, env=None):
    info = detect(host)
    if not info:
        raise IntegrationError(f"{host} was not found or its version could not be verified.")
    marketplace, directory = identity(host)
    files = bundle_files(info, marketplace)
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    root = directory / fingerprint[:16]
    exe = info["executable"]
    plugin = "openvegas@" + marketplace
    commands = [[exe, "plugin", "marketplace", "add", str(root)],
                [exe, "plugin", "install" if host == "claude" else "add", plugin]]
    summary = {**info, "marketplace": marketplace, "path": str(root), "commands": commands,
               "mode": "mod_and_mcp" if info["mod"] else "mcp_text_only",
               "host_home": (host_environment(env) or os.environ).get("HOME", str(Path.home())),
               "billing": "host_managed_unchanged"}
    if dry_run:
        return {**summary, "installed": False, "dry_run": True}
    for ancestor in [directory, *directory.parents]:
        if ancestor.is_symlink():
            raise IntegrationError("Integration install directories must not be symlinks.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Reuse the existing cross-platform locking/atomic-write backend.
    from openvegas.emotes.spool import _locked, _read, atomic_write
    with _locked(directory) as fd:
        try:
            raw = _read(fd, "installed.json", 16384)
        except FileNotFoundError:
            raw = None
        previous = json.loads(raw) if raw else None
        actual = registration(info, marketplace, env=env)
        if previous and previous.get("fingerprint") == fingerprint:
            if _matches(actual, root) and all(actual[k] for k in ("registered", "installed", "enabled")):
                return {**summary, "installed": True, "unchanged": True}
            raise IntegrationError("Host integration was removed, disabled or changed outside OpenVegas. "
                                   "Inspect host settings, or run integration uninstall " + host + " before setting up again.")
        if previous:
            old_root = _verified_previous(previous, directory, marketplace, actual)
            if not upgrade:
                raise IntegrationError("An older OpenVegas plugin is installed. Run openvegas go to review "
                                       "and approve its update; existing files were preserved.")
        if not previous and (actual["registered"] or actual["installed"]):
            raise IntegrationError("This integration name already exists without an OpenVegas install receipt. "
                                   "Existing host settings were preserved; inspect them before setup.")
        for name, text in files.items():
            path = root / name
            if any(parent.is_symlink() for parent in (path, *path.parents)):
                raise IntegrationError("Integration bundle must not contain symlinks.")
            if path.exists():
                if path.is_symlink() or path.read_text(encoding="utf-8") != text:
                    raise IntegrationError("Existing bundle was modified; refusing to overwrite it.")
            else:
                _write(path, text)
        if host == "claude":
            _run([exe, "plugin", "validate", str(root / "plugins/openvegas")], env=env)
        registered = False
        policy = codex_policy_snapshot(plugin, env) if host == 'codex' else None
        try:
            if previous:
                atomic_write(fd, "previous.json", raw)
                _remove_registration(info, marketplace, actual, env=env)
            _run(commands[0], env=env)
            registered = True
            _run(commands[1], env=env)
            if policy:
                restore_codex_policy(policy, plugin)
            actual = registration(info, marketplace, env=env)
            if not _matches(actual, root) or not all(actual[k] for k in ("registered", "installed", "enabled")):
                raise IntegrationError("Host did not confirm the integration installation.")
            atomic_write(fd, "installed.json", json.dumps({**summary, "fingerprint": fingerprint}).encode())
        except (IntegrationError, OSError) as exc:
            if previous:
                try:
                    current = registration(info, marketplace, env=env)
                    if not (_matches(current, root) or _matches(current, old_root)):
                        raise IntegrationError("Host configuration changed during update.")
                    _remove_registration(info, marketplace, current, env=env)
                    _run([exe, "plugin", "marketplace", "add", str(old_root)], env=env)
                    _run(commands[1], env=env)
                    if policy:
                        restore_codex_policy(policy, plugin)
                    restored = registration(info, marketplace, env=env)
                    if not _matches(restored, old_root) or not all(restored.get(k) for k in ("registered", "installed", "enabled")):
                        raise IntegrationError("Host did not confirm restoration.")
                except IntegrationError:
                    raise IntegrationError("OpenVegas update and rollback could not be confirmed. "
                                           "Old bundle and receipts were preserved; inspect integration status before retrying.") from None
                detail = str(exc) if isinstance(exc, IntegrationError) else "Local receipt could not be saved."
                raise IntegrationError("OpenVegas update failed; the previous plugin was restored. " + detail) from None
            # Remove only this setup's namespaced registration; never edit host settings wholesale.
            if registered:
                try:
                    _run([exe, "plugin", "uninstall" if host == "claude" else "remove", plugin], env=env)
                except IntegrationError:
                    pass
                try:
                    _run([exe, "plugin", "marketplace", "remove", marketplace], env=env)
                except IntegrationError:
                    pass
            raise
    return {**summary, "installed": True, "updated": bool(previous),
            "reload": "Restart Claude, then /openvegas wallet" if host == "claude" else "Start a new Codex chat, then $openvegas"}


def uninstall(host, *, env=None):
    marketplace, directory = identity(host)
    from openvegas.emotes.spool import _locked, _read, atomic_write
    if not directory.exists():
        return {"installed": False}
    info = detect(host)
    if not info:
        raise IntegrationError("Host CLI unavailable; integration files preserved for later removal.")
    with _locked(directory) as fd:
        try:
            raw = _read(fd, "installed.json", 16384)
        except FileNotFoundError:
            raw = None
        if not raw:
            return {"installed": False}
        receipt = json.loads(raw)
        if receipt.get("marketplace") != marketplace:
            raise IntegrationError("Install receipt does not match this account profile.")
        exe = info["executable"]
        actual = registration(info, marketplace, env=env)
        if not _matches(actual, Path(receipt["path"])):
            raise IntegrationError("Host registration no longer matches this install; refusing to remove someone else's configuration.")
        if actual["installed"]:
            _run([exe, "plugin", "uninstall" if host == "claude" else "remove", "openvegas@" + marketplace], env=env)
        if actual["registered"]:
            _run([exe, "plugin", "marketplace", "remove", marketplace], env=env)
        # Keep bundle and audit receipt; only remove the active installation marker.
        atomic_write(fd, "uninstalled.json", raw)
        from openvegas.emotes.spool import state_unlink
        state_unlink(fd, "installed.json")
    return {"installed": False, "preserved_bundle": receipt["path"]}
