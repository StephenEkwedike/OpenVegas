"""Opt-in VS Code game view. Reuses native_play; only its human input surface changes."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import time

from .credentials import IntegrationError, profile

EDITOR_SCHEMES = {'vscode', 'vscode-insiders', 'cursor', 'windsurf'}


def editor_launcher(env, *, candidates=None):
    # macOS users can have an old `code` alias pointing to a different editor.
    # Prefer the requested VS Code installation when it exists.
    if candidates is None:
        candidates = [Path(env.get('HOME', str(Path.home()))) / 'Applications/Visual Studio Code.app/Contents/Resources/app/bin/code',
                      Path('/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code')]
    return next((str(p) for p in candidates if p.is_file() and os.access(p, os.X_OK)),
                shutil.which('code', path=env.get('PATH')))


def editor_scheme(launcher):
    executable = Path(launcher).resolve()
    app = executable.parent.parent
    for product in (app / 'product.json', app / 'resources/app/product.json'):
        if product.is_file():
            scheme = json.loads(product.read_text()).get('urlProtocol')
            if scheme in EDITOR_SCHEMES:
                return scheme
            raise IntegrationError('This editor URL protocol is not supported by the local preview.')
    return 'vscode'


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise IntegrationError('Game view directory must not be redirected.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise IntegrationError('Game view directory must be private (0700).')
    return path


def settings():
    path = profile() / 'game-view.json'
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise IntegrationError('Unsafe game view settings; use integration game-view to reset.')
    try:
        value = json.loads(path.read_text())
    except ValueError:
        raise IntegrationError('Invalid game view settings; reinstall or disable the game view.') from None
    if not isinstance(value, dict):
        raise IntegrationError('Invalid game view settings; reinstall or disable the game view.')
    if value.get('enabled') is not True:
        return None
    if any(not isinstance(value.get(key), str) or not Path(value[key]).is_absolute()
           for key in ('requests_dir', 'launcher', 'host_home')):
        raise IntegrationError('Invalid game view paths; reinstall or disable the game view.')
    if value.get('uri_scheme', 'vscode') not in EDITOR_SCHEMES:
        raise IntegrationError('Invalid game view protocol; reinstall or disable the game view.')
    return value


class GameView:
    def __init__(self, config):
        self.directory = private_directory(config['requests_dir'])
        self.launcher = config['launcher']
        self.scheme = config.get('uri_scheme', 'vscode')
        self.host_home = config.get('host_home', str(Path.home()))
        self.id = secrets.token_hex(16)
        # macOS Unix sockets have a 104-byte path limit. Keep the socket in a
        # private temp directory, not a deeply nested project checkout.
        import tempfile
        self.socket_dir = Path(tempfile.mkdtemp(prefix='ov-game-'))
        self.socket = self.socket_dir / 'view.sock'
        self.request = self.directory / (self.id+'.json')
        self.token = secrets.token_hex(32)
        self.writer = None
        self.answer = None
        self.question = None
        self.closed = False
        self.clients = set()
        self.server = None
        self.proc = None

    async def connect(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        owned = False
        try:
            first = json.loads(await asyncio.wait_for(reader.readline(), 3))
            if not isinstance(first, dict) or not hmac.compare_digest(str(first.get('token','')), self.token) or self.writer is not None:
                return
            owned = True
            self.writer = writer
            if not self.ready.done():
                self.ready.set_result(True)
            while line := await reader.readline():
                value = json.loads(line)
                if not isinstance(value, dict):
                    break
                if value.get('type') == 'quit':
                    self.closed = True
                    if self.answer and not self.answer.done():
                        self.answer.set_result(None)
                    break
                if (value.get('type') == 'answer' and value.get('id') == self.question
                        and self.answer and not self.answer.done()):
                    self.answer.set_result(value.get('data'))
        except (ValueError, OSError, asyncio.TimeoutError):
            pass
        finally:
            if owned:
                self.closed = True
                if self.answer and not self.answer.done():
                    self.answer.set_result(None)
            writer.close()
            self.clients.discard(task)

    async def __aenter__(self):
        self.ready = asyncio.get_running_loop().create_future()
        try:
            self.server = await asyncio.start_unix_server(self.connect, path=self.socket, limit=65536)
            os.chmod(self.socket, 0o600)
            data = {'socket':str(self.socket), 'token':self.token, 'cwd':str(Path.cwd().resolve()),
                    'created':time.time(), 'id':self.id}
            fd = os.open(self.request, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd,'w') as stream:
                json.dump(data,stream)
            # The dev MCP helper has an isolated account HOME. The editor must
            # use its real installation HOME, without inheriting provider keys.
            launch_env = {k: os.environ[k] for k in ('PATH', 'TMPDIR', 'LANG', 'LC_ALL',
                'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS') if k in os.environ}
            launch_env.update(HOME=self.host_home, USERPROFILE=self.host_home)
            self.proc = await asyncio.create_subprocess_exec(self.launcher, '--reuse-window', '--open-url',
                self.scheme+'://openvegas.game-table/play?request='+self.id,
                env=launch_env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            if await asyncio.wait_for(self.proc.wait(), 8):
                raise OSError('VS Code launcher failed')
            await asyncio.wait_for(self.ready, 12)
        except BaseException:
            await self.__aexit__(None,None,None)
            raise
        return self

    async def ask(self, message, schema):
        if self.closed:
            return None
        from pydantic import ValidationError
        self.question = secrets.token_hex(16)
        self.answer = asyncio.get_running_loop().create_future()
        self.writer.write((json.dumps({'type':'question', 'id':self.question, 'message':message,
            'schema':schema.model_json_schema()})+'\n').encode())
        await self.writer.drain()
        data = await self.answer
        if data is None:
            return None
        try:
            return schema.model_validate(data, strict=True)
        except ValidationError:
            # Invalid/stale UI messages cannot act as consent or a game move.
            raise IntegrationError('Invalid game-view response. Round preserved; reopen play.') from None

    async def status(self, message, *, terminal=False):
        if self.closed or self.writer is None:
            return
        self.writer.write((json.dumps({'type': 'closed' if terminal else 'status',
                                      'message': message}) + '\n').encode())
        await self.writer.drain()

    async def __aexit__(self, *_):
        self.closed = True
        if self.writer:
            self.writer.close()
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        tasks = list(self.clients)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.request.unlink(missing_ok=True)
        self.socket.unlink(missing_ok=True)
        self.socket_dir.rmdir()


async def play(ctx, broker, config, *, requested_game=None):
    from .native_play import notify, play_in_host
    try:
        async with GameView(config) as view:
            try:
                result = await play_in_host(ctx, broker, ask_override=view.ask, progress_override=view.status,
                                            requested_game=requested_game)
            except IntegrationError as exc:
                async def failed(message):
                    await view.status(message, terminal=True)
                await notify(failed, str(exc))
                raise
            if result.get('available') is False:
                async def failed(message):
                    await view.status(message, terminal=True)
                await notify(failed, result['message'])
            return result
    except (OSError, asyncio.TimeoutError):
        return {'available':False, 'message':'The VS Code game tab did not connect. No automatic wager or retry. '
                'Reload your VS Code window and try again. Use ov integration game-view --disable '
                'to restore native forms; an existing round is preserved.'}
