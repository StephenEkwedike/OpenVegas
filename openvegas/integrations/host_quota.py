"""Read host-owned usage metadata only; never call a provider or retain prompts."""
from __future__ import annotations

import asyncio
import json
import math
import os
import shlex
import stat
import sys
import time
from pathlib import Path


def windows(raw, *, field, now=None):
    now = time.time() if now is None else now
    result = []
    if not isinstance(raw, dict):
        return result
    for name in ('primary', 'secondary', 'five_hour', 'seven_day', 'spend_limit'):
        window = raw.get(name)
        if not isinstance(window, dict):
            continue
        used, reset = window.get(field), window.get('resets_at')
        if (type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100
                or type(reset) not in (int, float) or not math.isfinite(reset) or reset <= now):
            continue
        result.append({'remaining': 100 - used, 'reset': reset})
    return result


class HostQuota:
    def __init__(self, host, env):
        self.host, self.env = host, env
        self.path = None
        self.stamp = None
        self.values = []

    def transcript(self, name, identifier):
        if self.host != 'codex' or not isinstance(name, str) or not identifier:
            return
        path = Path(name)
        root = Path(self.env.get('CODEX_HOME', str(Path(self.env['HOME']) / '.codex'))) / 'sessions'
        # Only the registered host's session file, not arbitrary project JSON.
        if (path.is_absolute() and path.name.endswith(identifier + '.jsonl')
                and path.resolve().is_relative_to(root.resolve())
                and not any(p.is_symlink() for p in (path, *path.parents))):
            self.path = path

    def observe(self, raw):
        if self.host == 'claude':
            self.values = windows(raw, field='used_percentage')

    def read(self):
        if self.path is None:
            return
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    return
                stamp = (info.st_ino, info.st_mtime_ns, info.st_size)
                if stamp == self.stamp:
                    return
                self.stamp = stamp
                offset = max(0, info.st_size - 524288)
                stream.seek(offset)
                rows = stream.read(524288).splitlines()
                if offset:
                    rows = rows[1:]
                for row in reversed(rows):
                    try:
                        value = json.loads(row)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(value, dict) or value.get('type') != 'event_msg':
                        continue
                    payload = value.get('payload')
                    if isinstance(payload, dict) and payload.get('type') == 'token_count' and 'rate_limits' in payload:
                        self.values = windows(payload['rate_limits'], field='used_percent')
                        break
        except OSError:
            pass

    def remaining(self):
        self.read()
        now = time.time()
        live = [v['remaining'] for v in self.values if v['reset'] > now]
        return min(live) if live else None


def claude_settings(argv, env):
    """Add an ephemeral status observer only when no user status line is set.

    Never overwrite an explicit settings source or an existing custom script.
    The supervised child alone receives this setting; no config file is edited.
    """
    if any(a.split('=')[0] in ('--settings', '--setting-sources') for a in argv):
        return argv
    root = Path(env.get('CLAUDE_CONFIG_DIR', str(Path(env['HOME']) / '.claude')))
    candidates = [root / 'settings.json', Path('/Library/Application Support/ClaudeCode/managed-settings.json'),
                  Path('/etc/claude-code/managed-settings.json')]
    for parent in (Path.cwd(), *Path.cwd().parents):
        candidates.extend([parent / '.claude/settings.json', parent / '.claude/settings.local.json'])
    try:
        for path in candidates:
            if path.is_file():
                if path.stat().st_size > 1048576 or json.loads(path.read_text()).get('statusLine'):
                    return argv
    except (OSError, ValueError, AttributeError):
        return argv
    from .installer import helper
    command = shlex.join([*helper('claude'), '--quota-statusline'])
    return [*argv, '--settings', json.dumps({'statusLine': {'type': 'command', 'command': command}})]


def statusline_main():
    from .use import request
    from .credentials import IntegrationError
    try:
        data = json.loads(sys.stdin.buffer.read(131073))
        raw = data.get('rate_limits')
        # Forward only known numeric metadata, not transcript text or account IDs.
        clean = {name: {key: value.get(key) for key in ('used_percentage', 'resets_at')}
                 for name, value in (raw.items() if isinstance(raw, dict) else [])
                 if name in ('five_hour', 'seven_day', 'spend_limit') and isinstance(value, dict)}
        asyncio.run(request('claude', 'quota', session_id=data.get('session_id'), rate_limits=clean))
    except (IntegrationError, ValueError, OSError, AttributeError, TypeError):
        pass
    # The owned relay/HUD renders the warning. No duplicate status line text.
    print('')
