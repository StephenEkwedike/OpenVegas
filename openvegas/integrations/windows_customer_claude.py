"""Private per-launch Claude plugin. A native command, not a model tool, changes payer."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path

import click

from .billing_host import session_id
from .credentials import IntegrationError
from .inference_router_adapter import VERSIONS
from .inference_router_windows import WindowsTCPPeerAuth
from .windows_customer import Selection, require, same_workspace

PLUGIN = r"""
const base = __BASE__, key = __KEY__;
async function send($, data) {
  const reply = await $.http.fetch(base + '/ov-local-control', {method:'POST',
    headers:{'Content-Type':'application/json','X-OV-Command':key}, body:JSON.stringify(data)});
  if (!reply.ok) throw Error();
  return JSON.parse(reply.text);
}
async function metadata($) {
  const policy = await $.settings.read({source:'policy'});
  const env = (await $.settings.read({key:'env'})) ?? {};
  if (!policy || Object.keys(policy).length || Object.keys(env).some(k =>
      /^(ANTHROPIC_.*BASE_URL|ANTHROPIC_UNIX_SOCKET|ANTHROPIC_CUSTOM_HEADERS|CLAUDE_CODE_USE_.*|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY)$/i.test(k)))
    throw Error();
  if ((await $.session.version()).version !== '2.1.322' || await $.env.get('ANTHROPIC_UNIX_SOCKET')
      || await $.env.get('ANTHROPIC_BASE_URL') !== base)
    throw Error();
  return {identifier:await $.session.id(), model:await $.session.model(), cwd:await $.session.cwd(), base_url:base};
}
async function verifyReady($) {
  if ((await send($, {event:'ready', value:await metadata($)})).message !== 'ready') throw Error();
}
function localResult($, message) {
  // Returning {text} retains the command, arguments and stdout in the next model request.
  try { $.ui.log(message); } catch { /* No model-facing fallback if the display is unavailable. */ }
  return {};
}
export function register(on) {
  on('session.start', async ($, e, next) => {
    const result = await next(e);
    await $.command.register({name:'ov-billing',description:'Local OpenVegas payer and balance',
      argumentHint:'v|host|status', immediate:true});
    // Register before returning, but don't hold native startup on a local HTTP reply.
    $.clock.after(0, async () => {
      try { await verifyReady($); }
      catch { $.ui.log('OpenVegas billing controls registered; readiness unconfirmed.'); return; }
      $.ui.log('OpenVegas billing controls ready.');
    });
    return result;
  });
  on('command.run', {command:'ov-billing'}, async ($, e) => {
    const action = (e.args ?? '').trim().toLowerCase();
    if (e.origin.kind !== 'composer') return {};
    if (!['v','host','status'].includes(action))
      return localResult($, 'Use /ov-billing v, host, or status from the native composer.');
    try {
      // Only a person's explicit status check repeats readiness, never a payer mutation.
      if (action === 'status') await verifyReady($);
      else await metadata($);
      let reply = await send($, {event:'start', action});
      for (let i=0; i<3 && reply.challenge; i++)
        reply = await send($, {event:'observe', challenge:reply.challenge, value:await metadata($)});
      // Only show billing outcomes, never an unexpected response body or error.
      const msg = reply.message;
      const known = typeof msg === 'string' && msg.length <= 100 && !/[\r\n]/.test(msg) &&
        (/^Payer: (host|v)(?:\. No prompt sent\.|; (?:\$V [\d,]+\.\d{2}|balance unavailable)\.)$/.test(msg) ||
        ['Switch refused; payer unchanged. No prompt sent.', 'Billing control busy; no new switch started.',
         'Billing outcome unconfirmed; inference held. No automatic retry.',
         'Billing change pending or unconfirmed; check status.'].includes(msg));
      return localResult($, known ? msg : 'Local control unconfirmed. Check status.');
    } catch { return localResult($, 'Local control unconfirmed. Check status; no automatic retry.'); }
  });
}
"""


def unsafe_key(key):
    k = key.upper()
    return (
        k
        in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "ANTHROPIC_CUSTOM_HEADERS",
            "ANTHROPIC_UNIX_SOCKET",
            "CLAUDE_CODE_SIMPLE",
            "CLAUDE_CODE_SETTINGS_PATH",
            "OPENVEGAS_LOCAL_ROUTER_AUTH",
        )
        or k.startswith(("CLAUDE_CODE_USE_", "CLAUDE_CODE_MANAGED_", "CLAUDE_CODE_POLICY_"))
        or k.startswith("ANTHROPIC_")
        and k.endswith("BASE_URL")
    )


def check_settings(env, cwd):
    """Reject profile/project/enterprise routing overrides; never read account/auth files."""
    import winreg

    from openvegas.emotes._windows_resources import read_windows

    from .windows_session_lease import known_folder

    require(not any(unsafe_key(k) for k in env))
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(
                    hive, r"SOFTWARE\Policies\ClaudeCode", 0, winreg.KEY_READ | view
                ):
                    require(False)
            except FileNotFoundError:
                pass
    home = Path(env["USERPROFILE"])
    require(home.is_absolute() and (not env.get("HOME") or Path(env["HOME"]) == home))
    profile = Path(env.get("CLAUDE_CONFIG_DIR", home / ".claude"))
    require(profile.is_absolute())
    sources = {profile, *(p / ".claude" for p in (Path(cwd), *Path(cwd).parents))}
    # Do not let a project-controlled ProgramFiles/ProgramData hide policy.
    system_roots = [
        known_folder(key)
        for key in (
            "905E63B6-C1BF-494E-B29C-65B732D3D21A",  # Program Files
            "7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E",  # Program Files (x86)
            "62AB5D82-FDC1-4DC3-A9DD-070D1D495D97",  # ProgramData
        )
    ]
    for base in {profile, *(p / "ClaudeCode" for p in system_roots)}:
        for name in ("managed-settings.json", "managed-settings.d"):
            try:
                (base / name).lstat()
            except FileNotFoundError:
                continue
            require(False)
    for source, name in [
        (profile, "remote-settings.json"),
        *((p, n) for p in sources for n in ("settings.json", "settings.local.json")),
    ]:
        try:
            (source / name).lstat()
        except FileNotFoundError:
            continue
        value = json.loads(read_windows(source, [name], 1024 * 1024))
        require(isinstance(value, dict) and (name != "remote-settings.json" or not value))
        overrides = value.get("env", {})
        require(isinstance(overrides, dict) and not any(unsafe_key(k) for k in overrides))


@contextmanager
def plugin(base, key):
    from openvegas.emotes._windows_state import private_windows_directory

    from .windows_session_lease import _local_appdata

    path = _local_appdata() / "OpenVegas-Claude" / secrets.token_hex(16)
    with ExitStack() as stack:
        for p, files in (
            (path, {}),
            (
                path / ".claude-plugin",
                {
                    "plugin.json": '{"name":"ov-win-billing","version":"1.0.0","description":"Local Windows billing controls"}'
                },
            ),
            (
                path / "hooks",
                {
                    "hooks.json": '{"hooks":{},"modules":["./control.js"]}',
                    "control.js": PLUGIN.replace("__BASE__", json.dumps(base)).replace(
                        "__KEY__", json.dumps(key)
                    ),
                },
            ),
        ):
            d = stack.enter_context(private_windows_directory(p))
            require(not d.names(1))
            for name, value in files.items():
                d.atomic_write(name, value.encode("ascii"))
                stack.callback(d.unlink, name)
        yield str(path)
    # Only empty, known directories. Never recurse through native-created entries.
    for p in (path / "hooks", path / ".claude-plugin", path):
        try:
            p.rmdir()
        except OSError:
            pass


class Ingress:
    """Guard inference AND controls with the retained process; controls also require the plugin key."""

    def __init__(self, owner, peer):
        self.owner, self.peer = owner, peer
        self.inner = owner.session.router
        self.key = secrets.token_urlsafe(32)
        self.process = self.ticket = self.answer = self.task = None
        self.ready = self.closed = False
        self.identifier = None
        self.changed = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    authority = property(lambda s: s.inner.authority, lambda s, v: setattr(s.inner, "authority", v))
    client = property(lambda s: s.inner.client, lambda s, v: setattr(s.inner, "client", v))

    def live(self):
        require(
            not self.closed
            and self.ready
            and self.process is not None
            and self.process.poll() is None
        )

    async def observe(self):
        self.live()
        self.answer = asyncio.get_running_loop().create_future()
        self.ticket = secrets.token_hex(16)
        self.changed.set()
        try:
            return await self.answer
        finally:
            self.ticket = self.answer = None

    async def exchange(self, msg):
        event = msg.get("event")
        if event == "ready":
            self.ready = False
            value = Selection(**msg["value"])
            require(
                not self.closed
                and self.process is not None
                and self.process.poll() is None
                and same_workspace(value.cwd, self.owner.cwd)
                and value.base_url == self.owner.session._url
                and session_id(value.identifier)
                and self.identifier in (None, value.identifier)
            )
            self.identifier = value.identifier
            self.ready = True
            return {"message": "ready"}
        self.live()
        if event == "start":
            # If a command reply was lost, it is safe to inspect its outcome,
            # not to repeat it. Any unknown mutation remains held by RouterSession.
            if self.task is not None and self.task.done():
                if not self.task.cancelled():
                    self.task.exception()
                self.task = None
            require(self.task is None and msg.get("action") in ("v", "host", "status"))
            self.changed.clear()
            self.task = asyncio.create_task(
                self.owner.command(msg["action"], observe=self.observe, guard=self.live)
            )
            self.task.add_done_callback(lambda _: self.changed.set())
        elif event == "observe":
            require(
                self.ticket is not None
                and msg.get("challenge") == self.ticket
                and not self.answer.done()
            )
            value = Selection(**msg["value"])
            require(self.identifier == value.identifier)
            self.changed.clear()
            self.answer.set_result(value)
            self.ticket = None
        else:
            require(False)
        task = self.task
        async with asyncio.timeout(5):
            await self.changed.wait()
        if task.done():
            if self.task is task:
                self.task = None
            return {"message": task.result()}
        require(self.ticket is not None)
        return {"challenge": self.ticket}

    async def __call__(self, scope, receive, send):
        if not await self.peer(scope):
            return await self.inner._error(scope, send, 403, "Native peer refused.")
        if scope.get("path") != "/ov-local-control":
            if not self.ready:
                return await self.inner._error(scope, send, 403, "Native settings not verified.")
            return await self.inner(scope, receive, send)
        try:
            require(
                scope["type"] == "http"
                and scope["method"] == "POST"
                and not scope.get("query_string")
                and [v for k, v in scope["headers"] if k == b"host"]
                == [self.authority.encode("ascii")]
            )
            proof = [v for k, v in scope["headers"] if k == b"x-ov-command"]
            require(len(proof) == 1 and hmac.compare_digest(proof[0], self.key.encode("ascii")))
            data = bytearray()
            async with asyncio.timeout(2):
                while True:
                    frame = await receive()
                    require(
                        frame["type"] == "http.request" and len(data) + len(frame["body"]) <= 4096
                    )
                    data.extend(frame["body"])
                    if not frame.get("more_body"):
                        break
            result = await self.exchange(json.loads(data))
        except (IntegrationError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
            return await self.inner._error(scope, send, 403, "Local command unconfirmed.")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps(result).encode()})

    async def close(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


async def run(owner, exe, env):
    session = owner.session
    peer = WindowsTCPPeerAuth()
    ingress = Ingress(owner, peer)
    session.router = ingress
    try:
        async with session.launch(
            version=VERSIONS["claude"], env=env, args=[], effective_policy_verified=True
        ) as (child_env, args):
            with plugin(session._url, ingress.key) as directory:
                click.echo(
                    "Windows managed Claude. In the composer use /ov-billing v, host, or status.",
                    err=True,
                )
                child = ingress.process = subprocess.Popen(  # noqa: ASYNC220 - Pin launch handle before yielding.
                    [exe, *args, "--plugin-dir", directory],
                    env=child_env,
                    cwd=owner.cwd,
                    shell=False,
                )
                try:
                    peer.bind(child)
                    while child.poll() is None:
                        await asyncio.sleep(0.05)
                    return child.returncode
                finally:
                    try:
                        await ingress.close()
                    finally:
                        if child.poll() is None:
                            child.kill()
                            await asyncio.to_thread(child.wait)
    finally:
        peer.close()
