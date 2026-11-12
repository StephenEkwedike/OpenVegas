"""Private per-launch observer plugin and bounded nonce-bound file IPC.

No global settings/profile writes. The caller keeps this bridge alive until all
owned native generations stop, then closes it. Drafts exist only in this private
directory/in memory, never evidence logs. No credentials are stored in its files.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
from pathlib import Path

from .claude_generation_switch import ComposerDraft, Observation, PendingPrompt
from .claude_payer_boundary import RuntimeSnapshot
from .credentials import IntegrationError

MAX_FRAME = 4 * 1024 * 1024


def _require(condition):
    if not condition:
        raise IntegrationError('Private Claude observer could not verify this generation.')


class ClaudeGenerationObserver:
    """Callable observer; append .argv and merge .env into ONLY the managed child.

    `directory` must not exist, or omit it to create a private temporary root.
    This bundles the exact installer billing-hook Node command for SessionStart
    and UserPromptSubmit, plus a quota-independent /openvegas use v|host handler.
    """

    def __init__(self, directory=None, *, node=None):
        from .use import MISSING

        node = node or shutil.which('node')
        _require(isinstance(node, str) and Path(node).is_absolute() and Path(node).is_file())
        assets = Path(__file__).with_name('assets')
        source = (assets / 'billing-hook.js').read_text().replace(
            '__OPENVEGAS_MISSING__', json.dumps(MISSING))
        billing = [node, '-e', source, 'claude', '--billing-hook']
        if directory is None:
            self.root = Path(tempfile.mkdtemp(prefix='ov-claude-generation-'))
        else:
            self.root = Path(directory).absolute()
            self.root.mkdir(mode=0o700, parents=False, exist_ok=False)
        os.chmod(self.root, 0o700)
        self.plugin_dir = self.root / 'plugin'
        for path in (self.plugin_dir / '.claude-plugin', self.plugin_dir / 'hooks'):
            path.mkdir(parents=True, mode=0o700)
        self._request, self._response = self.root / 'request.json', self.root / 'response.json'
        self._write(self._request, {})
        self._write(self._response, {})
        self._write(self.plugin_dir / '.claude-plugin/plugin.json', {
            'name': 'openvegas-generation', 'version': '0.0.1',
            'description': 'Private OpenVegas native generation controls and observer'})
        hook_command = shlex.join(billing)
        self._write(self.plugin_dir / 'hooks/hooks.json', {
            'hooks': {event: [{'hooks': [{'type': 'command', 'command': hook_command, 'timeout': 5}]}]
                      for event in ('SessionStart', 'UserPromptSubmit')},
            'modules': ['./observer.js']})
        module = (assets / 'claude-generation-observer.js').read_text().replace(
            '__OPENVEGAS_BILLING_ARGV__', json.dumps(billing))
        (self.plugin_dir / 'hooks/observer.js').write_text(module)
        os.chmod(self.plugin_dir / 'hooks/observer.js', 0o600)
        self.argv = ('--plugin-dir', str(self.plugin_dir))
        self.env = {'OV_BOUNDARY_CONTROL': str(self._request),
                    'OV_BOUNDARY_RESPONSE': str(self._response)}
        self._lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _write(path, value):
        raw = json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()
        _require(len(raw) <= MAX_FRAME)
        fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _read(self):
        fd = os.open(self._response, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                     and not info.st_mode & 0o077 and info.st_size <= MAX_FRAME)
            data = stream.read(MAX_FRAME + 1)
            _require(len(data) <= MAX_FRAME)
        return json.loads(data)

    async def __call__(self, *, process, generation, nonce, draft=None, action='capture', pending=()):
        _require(not self._closed and all(isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}', value)
                                         for value in (generation, nonce))
                 and (draft is None or type(draft) is ComposerDraft)
                 and action in ('capture', 'release') and type(pending) is tuple
                 and len(pending) <= 128 and all(type(item) is PendingPrompt for item in pending))
        async with self._lock:
            request = {'generation': generation, 'nonce': nonce, 'action': action,
                       'pending': [{'identifier': item.identifier, 'text': item.text} for item in pending]}
            if draft is not None:
                request['draft'] = {'text': draft.text, 'cursor': draft.cursor}
            self._write(self._request, request)
            async with asyncio.timeout(8):
                while True:
                    _require(process.returncode is None)
                    try:
                        response = self._read()
                    except json.JSONDecodeError:
                        # A native write can be observed mid-write; stale/partial
                        # data is never an acknowledgement of this request.
                        response = {}
                    if (isinstance(response, dict) and response.get('generation') == generation
                            and response.get('nonce') == nonce):
                        _require(response.get('controls_ready') is True
                                 and type(response.get('composer_ready')) is bool
                                 and not response.get('untransferable'))
                        try:
                            snapshot = RuntimeSnapshot(**response['snapshot'])
                            actual = ComposerDraft(**response['draft'])
                            held = tuple(PendingPrompt(**item) for item in response.get('pending', []))
                        except (KeyError, TypeError, ValueError):
                            _require(False)
                        if draft is not None:
                            _require(response['composer_ready'] and actual == draft)
                        return Observation(generation, nonce, snapshot, actual,
                                           composer_ready=response['composer_ready'], pending=held)
                    await asyncio.sleep(.01)

    def close(self):
        if not self._closed:
            shutil.rmtree(self.root)
            self._closed = True
