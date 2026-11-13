"""Explicit disposable native acceptance transport, never a provider fallback.

Normal launches do not read a fixture file or change their destination. Fixture
launches require isolated profiles, numeric loopback and synthetic credentials.
The real adapter, request ledger and native authentication checks still run.
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .credentials import IntegrationError

ENV = 'OPENVEGAS_NATIVE_FIXTURE'
SYNTHETIC_KEY = 'sk-openvegas-native-fixture'


def require(value):
    if not value:
        raise IntegrationError('Offline native fixture isolation failed; nothing was sent.')


def private(path, *, directory=False):
    require(path.is_absolute() and '..' not in path.parts)
    require(not any(p.is_symlink() for p in (path, *path.parents)))
    info = path.stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o077)
    require(stat.S_ISDIR(info.st_mode) if directory else
            stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 8192)


def read_json(path):
    private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(8193)
    require(len(raw) <= 8192)

    def pairs(items):
        value = dict(items)
        require(len(value) == len(items))
        return value

    return json.loads(raw, object_pairs_hook=pairs)


def isolated_path(path, root):
    require(path.is_absolute() and '..' not in path.parts and path.is_relative_to(root))
    require(not any(p.is_symlink() for p in (path, *path.parents)))
    if path.exists():
        private(path, directory=True)


@dataclass(frozen=True, repr=False)
class OfflineFixture:
    endpoint: str
    expires_at: float
    host_base: str
    wallet_base: str
    root: Path
    profiles: tuple[Path, ...]

    def destination(self, plan, mode):
        require(time.time() < self.expires_at)
        for profile in self.profiles:
            isolated_path(profile, self.root)
        headers = dict(plan.headers)
        require(not any(k in headers for k in ('cookie', 'chatgpt-account-id', 'proxy-authorization')))
        if mode == 'host':
            base = self.host_base.replace('https://', 'wss://', 1) if plan.transport == 'websocket' else self.host_base
            require(plan.endpoint.startswith(base + '/'))
            auth = {k: v for k, v in headers.items() if k in ('authorization', 'x-api-key')}
            require(auth in ({'authorization': 'Bearer ' + SYNTHETIC_KEY}, {'x-api-key': SYNTHETIC_KEY}))
            parsed = urlsplit(plan.endpoint)
            base = self.endpoint.replace('http://', 'ws://', 1) if plan.transport == 'websocket' else self.endpoint
            return base + '/_host' + parsed.path + ('?' + parsed.query if parsed.query else '')
        base = self.wallet_base.replace('http://', 'ws://', 1) if plan.transport == 'websocket' else self.wallet_base
        require(mode == 'v' and plan.endpoint.startswith(base + '/'))
        require(re.fullmatch(r'Bearer ov_native_fixture_[A-Za-z0-9_-]{16,96}',
                             headers.get('authorization', '')) is not None)
        return plan.endpoint


def configured(adapter=None):
    name = os.environ.get(ENV)
    if name is None:
        return None
    try:
        require(os.name == 'posix' and bool(name))
        config = read_json(Path(name))
        require(type(config) is dict and set(config) == {'version', 'root', 'endpoint', 'expires_at'})
        require(type(config['version']) is int and config['version'] == 1)
        root = Path(config['root'])
        private(root, directory=True)
        require(re.fullmatch(r'fixture-[a-f0-9]{32}', root.name) is not None)
        require(Path(name).parent == root)
        expected = {'HOME': root, 'CODEX_HOME': root / '.codex',
                    'CLAUDE_CONFIG_DIR': root / '.claude',
                    'OPENVEGAS_INTEGRATION_PROFILE': root / '.openvegas'}
        require(all(os.environ.get(k) == str(v) for k, v in expected.items()))
        profiles = list(expected.values())
        for profile in profiles:
            isolated_path(profile, root)
        for key in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'):
            require(not os.environ.get(key) or os.environ[key] == SYNTHETIC_KEY)
        require(not os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'))
        require(Path.cwd().is_relative_to(root))
        for key in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'USERPROFILE', 'TMPDIR'):
            if os.environ.get(key):
                path = Path(os.environ[key])
                isolated_path(path, root)
                profiles.append(path)
        require(os.environ.get('PYTHON_DOTENV_DISABLED') == '1'
                and os.environ.get('OPENVEGAS_LOAD_PROJECT_ENV') == '0')
        expiry = config['expires_at']
        require(type(expiry) in (int, float) and time.time() < expiry <= time.time() + 900)
        endpoint = config['endpoint']
        parsed = urlsplit(endpoint)
        require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
                and parsed.port is not None and 1 <= parsed.port <= 65535
                and endpoint == f'http://127.0.0.1:{parsed.port}')
        require(adapter is None or adapter.wallet_base == endpoint + '/integrations/native')
        require(os.environ.get('OPENVEGAS_BACKEND_URL') == endpoint)
        return OfflineFixture(endpoint, expiry, adapter.host_base if adapter else '',
                              endpoint + '/integrations/native', root, tuple(profiles))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise IntegrationError('Offline native fixture isolation failed; nothing was sent.') from None
