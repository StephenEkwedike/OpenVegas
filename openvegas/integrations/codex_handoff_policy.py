"""Pinned Codex evidence, scoped to one owned session; never export RPC contents."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from websockets.asyncio.client import unix_connect

from .credentials import IntegrationError


class _Unverified(IntegrationError):
    pass


def _require(value, reason):
    if not value:
        raise _Unverified("Codex handoff not verified: " + reason + ".")


def _digest(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, repr=False)
class IdleEvidence:
    thread_id: str
    cwd: str
    model: str


@dataclass(frozen=True, repr=False)
class IntentObservation:
    thread_id: str
    cwd: str
    model: str
    status: str


@dataclass(frozen=True, repr=False)
class PolicySnapshot(IdleEvidence):
    policy_digest: str
    _static: tuple
    _runtime: str
    _seal: object = field(compare=False)
    reasoning_effort: str | None = None


_SEAL = object()
_ROUTE_FIELDS = {
    "openai_base_url",
    "cli_auth_credentials_store",
    "model",
    "review_model",
    "model_catalog_json",
}
_SELECTION_FIELDS = {"model", "model_reasoning_effort"}


def require_snapshot(value):
    _require(
        type(value) is PolicySnapshot and value._seal is _SEAL, "missing exclusive source evidence"
    )


def assert_same_policy(original, candidate):
    require_snapshot(original)
    require_snapshot(candidate)
    _require(
        (original.thread_id, original.cwd, original.policy_digest)
        == (candidate.thread_id, candidate.cwd, candidate.policy_digest),
        "identity or effective policy changed",
    )


def assert_same_selection(original, candidate):
    require_snapshot(original)
    require_snapshot(candidate)
    _require(
        (original.model, original.reasoning_effort)
        == (candidate.model, candidate.reasoning_effort),
        "active model or reasoning effort changed during billing handoff",
    )


@asynccontextmanager
async def _reader(endpoint):
    from .codex_billing_route import _LIVE_SOURCE_PROOFS, _OWNED_SERVERS, _POLICY_CONTEXT

    # The caller owns the runtime; never attach to a shared daemon or a TCP address.
    _require(
        isinstance(endpoint, str) and endpoint.startswith("unix:///"), "private endpoint required"
    )
    proc = _OWNED_SERVERS.get(endpoint)
    _require(proc is not None and proc.returncode is None, "server not owned by this supervisor")
    path = Path(endpoint[7:])
    try:
        info, parent = path.lstat(), path.parent.lstat()
        _require(
            stat.S_ISDIR(parent.st_mode)
            and parent.st_uid == os.getuid()
            and not parent.st_mode & 0o077,
            "endpoint directory ownership or mode",
        )
        if stat.S_ISLNK(info.st_mode):
            # Pinned macOS Codex shortens its private listen path this way.
            backing = path.resolve(strict=True)
            _require(
                info.st_uid == os.getuid()
                and re.fullmatch("[0-9a-f]{64}", backing.name)
                and backing.parent == Path("/tmp", f"codex-daemon-{os.getuid()}").resolve()
                and backing.parent.stat().st_uid == os.getuid()
                and not backing.parent.stat().st_mode & 0o077,
                "unverified socket alias",
            )
            info = backing.lstat()
        _require(
            stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
            "endpoint ownership or mode",
        )
        async with (
            asyncio.timeout(20),
            unix_connect(str(path), open_timeout=3, max_size=4 * 1024 * 1024) as ws,
        ):
            seq = 0

            async def call(method, params):
                nonlocal seq
                seq += 1
                await ws.send(json.dumps({"id": seq, "method": method, "params": params}))
                while True:
                    msg = json.loads(await ws.recv())
                    _require(isinstance(msg, dict), "invalid server message")
                    if "method" in msg:
                        _require("id" not in msg, "outstanding server request")
                        # A transient busy turn is not erased by a later idle read.
                        _require(
                            msg["method"]
                            not in {
                                "turn/started",
                                "item/started",
                                "thread/queue/changed",
                                "thread/settings/updated",
                            },
                            "runtime changed during inspection",
                        )
                        continue
                    _require(
                        msg.get("id") == seq
                        and "error" not in msg
                        and isinstance(msg.get("result"), dict),
                        method + " unavailable",
                    )
                    return msg["result"]

            handshake = await call(
                "initialize",
                {
                    "clientInfo": {"name": "openvegas-policy", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            _require(
                isinstance(handshake.get("userAgent"), str)
                and re.match(r"^[A-Za-z0-9_.-]+/0\.166\.0(?: |$)", handshake["userAgent"]),
                "unverified server version",
            )
            await ws.send('{"method":"initialized","params":{}}')
            call.runtime = endpoint
            call.live = _LIVE_SOURCE_PROOFS.setdefault(endpoint, {})
            call.scope = _POLICY_CONTEXT[endpoint]
            yield call
    except (_Unverified, asyncio.CancelledError):
        raise
    except Exception:  # noqa: BLE001 - never expose path, config, account or transcript-bearing RPC errors.
        raise _Unverified(
            "Codex policy inspection failed; no prompt or automatic fallback."
        ) from None


def _identity(thread, sid, *, for_intent=False):
    _require(
        thread["id"] == sid
        and thread["modelProvider"] == "openai"
        and thread["cliVersion"] == "0.166.0"
        and thread["ephemeral"] is False
        and thread["parentThreadId"] is None
        and (
            thread["status"] == {"type": "idle"}
            or (for_intent and thread["status"] == {"type": "active", "activeFlags": []})
        )
        and (
            thread["canAcceptDirectInput"] is True
            or (
                for_intent
                and thread["canAcceptDirectInput"] is False
                and thread["status"]["type"] == "active"
            )
        ),
        "thread not the expected available local root",
    )
    cwd, model = thread["cwd"], thread["model"]
    _require(
        isinstance(cwd, str) and Path(cwd).is_absolute() and isinstance(model, str) and model,
        "missing runtime identity",
    )
    envs = thread["environments"]
    _require(
        isinstance(envs, list)
        and len(envs) == 1
        and envs[0]["environmentId"] == "local"
        and envs[0]["cwd"] == cwd
        and envs[0]["runtimeWorkspaceRoots"] == [cwd],
        "nonlocal or extra roots",
    )
    if for_intent:
        return IntentObservation(sid, cwd, model, thread["status"]["type"])
    return IdleEvidence(sid, cwd, model)


async def _inspect(call, sid, *, for_intent=False):
    _require(isinstance(sid, str) and str(UUID(sid)) == sid, "invalid session ID")
    params = {"threadId": sid}

    async def read():
        return _identity(
            (await call("thread/read", {**params, "includeTurns": False}))["thread"],
            sid,
            for_intent=for_intent,
        )

    first = await read()
    loaded = await call("thread/loaded/list", {"limit": 2})
    ids = loaded["data"]
    # The hook may arrive while Codex is retiring an unsubscribed runtime.
    # A preview can start a read-only availability lookup, never a switch.
    # Don't inspect the other chat or infer ownership from its identity.
    provisional_only = (for_intent and isinstance(ids, list) and len(ids) == 2
                        and ids.count(sid) == 1 and all(isinstance(i, str) and i for i in ids))
    _require(
        (ids == [sid] or provisional_only) and loaded["nextCursor"] is None, "additional or unloaded runtime"
    )
    await _empty_activity(call, sid)
    _require(first == await read(), "thread changed during inventory")
    return first


async def _idle(call, sid):
    return await _inspect(call, sid)


async def observe_intent(endpoint, thread_id):
    """Non-authorizing preview while the command hook itself may still be active."""
    async with _reader(endpoint) as call:
        # Permanent capability loss should be explained even if this hook is
        # active or another runtime would also prevent an idle observation.
        await _configuration(call, call.scope["cwd"], {"cwds": [call.scope["cwd"]]})
        intent = await _inspect(call, thread_id, for_intent=True)
        _require(
            os.path.realpath(intent.cwd) == call.scope["cwd"], "original working directory changed"
        )
        return intent


async def check_source_config(endpoint, cwd):
    """Eligibility preview, not activity inspection, consent or a switch ticket."""
    async with _reader(endpoint) as call:
        _require(
            isinstance(cwd, str) and os.path.realpath(cwd) == call.scope["cwd"],
            "original cwd changed",
        )
        await _configuration(call, cwd, {"cwds": [cwd]})


def _all_sources():
    # The native default omits subagent and other non-interactive histories.
    return ['cli', 'vscode', 'exec', 'appServer', 'subAgent', 'subAgentReview',
            'subAgentCompact', 'subAgentThreadSpawn', 'subAgentOther', 'unknown']


async def _empty_activity(call, sid):
    params = {"threadId": sid}
    for method, query in [
        ("thread/backgroundTerminals/list", params),
        ("thread/queue/list", params),
        ("thread/attachment/list", params),
        ("thread/list", {"ancestorThreadId": sid, "modelProviders": [], "sourceKinds": _all_sources()}),
        ("thread/list", {"attachedToThreadId": sid, "modelProviders": [], "sourceKinds": _all_sources()}),
    ]:
        page = await call(method, {**query, "limit": 1})
        _require(
            page["data"] == [] and page["nextCursor"] is None, "nonempty or incomplete " + method
        )
    _require((await call("thread/goal/get", params))["goal"] is None, "thread has a goal")


async def _configuration(call, cwd, hook_params):
    cfg = await call("config/read", {"cwd": cwd, "includeLayers": True})
    effective = cfg["config"]
    _require(
        effective.get("web_search") == "disabled",
        "the wallet route disables hosted web search, but this original Codex configuration does not "
        'explicitly report web_search="disabled". Stay on host billing; no setting was changed',
    )
    _require(
        effective["model_provider"] in (None, "openai")
        and not any(
            effective[k]
            for k in (
                "permissions",
                "profile",
                "profiles",
                "forced_login_method",
                "forced_chatgpt_workspace_id",
            )
        ),
        "custom policy or account binding",
    )
    _require(
        effective["default_permissions"] in (None, ":workspace", ":read-only"),
        "unknown default permissions",
    )
    _require(
        effective.get("approval_policy") in (None, "on-request")
        and effective.get("approvals_reviewer") in (None, "user")
        and effective["sandbox_mode"] in (None, "read-only", "workspace-write"),
        "only the native user reviewer, on-request approvals and a restricted local sandbox are supported",
    )
    _require(
        (await call("configRequirements/read", {}))["requirements"] is None, "managed requirements"
    )
    layers = []
    _require(isinstance(cfg["layers"], list) and cfg["layers"], "missing config provenance")
    for layer in cfg["layers"]:
        kind = layer["name"]["type"]
        _require(
            kind in {"user", "project", "packagedDefaults", "system", "sessionFlags"},
            "managed config layer",
        )
        if kind == "sessionFlags":
            rest = {k: v for k, v in layer["config"].items()
                    if k not in _ROUTE_FIELDS | _SELECTION_FIELDS}
            if rest.get("model_provider") == "openai":
                rest.pop("model_provider")
            if rest.get("web_search") == effective["web_search"] == "disabled":
                rest.pop("web_search")
            if rest:
                layers.append((layer["name"], rest, layer.get("disabledReason")))
        else:
            # Native /model saves selections in user/project layers. Normalize
            # only those layers; other provenance revisions remain fenced.
            selection_layer = (kind in {"user", "project"}
                               and bool(_SELECTION_FIELDS & layer["config"].keys()))
            layers.append({
                **{k: v for k, v in layer.items()
                   if k != "config" and not (k == "version" and selection_layer)},
                "config": {k: v for k, v in layer["config"].items()
                           if k not in _SELECTION_FIELDS},
            })
    hooks = await call("hooks/list", hook_params)
    _require(
        len(hooks["data"]) == 1
        and hooks["data"][0]["cwd"] == cwd
        and hooks["data"][0]["errors"] == []
        and hooks["data"][0]["warnings"] == []
        and isinstance(hooks["data"][0]["hooks"], list)
        and all(
            isinstance(h.get("enabled"), bool)
            and h.get("trustStatus") in {"trusted", "untrusted", "managed", "modified"}
            and isinstance(h.get("currentHash"), str)
            and h["currentHash"]
            for h in hooks["data"][0]["hooks"]
        ),
        "hook trust unavailable",
    )
    parts = {
        "config": {
            k: v for k, v in effective.items()
            if k not in _ROUTE_FIELDS | _SELECTION_FIELDS | {"model_provider"}
        },
        "layers": layers,
        "hooks": hooks,
        "launch": call.scope,
    }
    return effective, tuple((name, _digest(value)) for name, value in parts.items())


async def _evidence(call, sid, *, source, model=None, source_fence=None):
    before = await _idle(call, sid) if source else None
    if not source:
        loaded = await call("thread/loaded/list", {"limit": 1})
        _require(
            loaded["data"] == [] and loaded["nextCursor"] is None, "target runtime already in use"
        )
    # The ordinary source UI must have exited, or its private transport must
    # still hold an authenticated detach proof. Never resume a second writer.
    if source_fence is not None:
        source_fence()
    result = await call("thread/resume", {"threadId": sid, "excludeTurns": True})
    return await _resume_snapshot(call, sid, result, before=before, model=model)


async def _resume_snapshot(call, sid, result, *, before, model):
    actual = _identity(result["thread"], sid)
    _require("reasoningEffort" in result, "active reasoning effort not reported")
    effort = result["reasoningEffort"]
    _require(effort is None or (isinstance(effort, str)
                               and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", effort)),
             "invalid active reasoning effort")
    _require(
        (before is None or before == actual)
        and result["modelProvider"] == "openai"
        and result["cwd"] == actual.cwd
        and result["model"] == actual.model
        and (not model or model == actual.model),
        "resumed a different runtime",
    )
    sandbox, profile = result["sandbox"], result["activePermissionProfile"]
    _require(
        sandbox["type"] in {"readOnly", "workspaceWrite"}
        and sandbox.get("networkAccess") is False
        and result["approvalPolicy"] == "on-request"
        and result["approvalsReviewer"] == "user",
        "unverified sandbox, network or approval mode",
    )
    _require(
        profile is None
        or (profile["id"] in {":read-only", ":workspace"} and not profile.get("extends")),
        "custom permission profile",
    )
    _require(result["runtimeWorkspaceRoots"] == [actual.cwd], "additional runtime roots")
    effective, static = await _configuration(call, actual.cwd, {"cwds": [actual.cwd]})
    _, scoped = await _configuration(call, actual.cwd, {"threadId": sid})
    if profile is None:
        _require(
            effective["sandbox_mode"]
            == {"readOnly": "read-only", "workspaceWrite": "workspace-write"}[sandbox["type"]],
            "no authoritative profile or explicit matching legacy sandbox",
        )
    policy = {
        "settings": {
            k: result[k]
            for k in (
                "cwd",
                "sandbox",
                "activePermissionProfile",
                "approvalPolicy",
                "approvalsReviewer",
                "runtimeWorkspaceRoots",
                "disabledPluginIds",
                "instructionSources",
            )
        },
        "static": static,
        "scoped": scoped,
        "environments": result["thread"]["environments"],
        "sessionId": result["thread"]["sessionId"],
        "forkedFromId": result["thread"]["forkedFromId"],
    }
    _require(await _idle(call, sid) == actual, "activity changed during policy inspection")
    return PolicySnapshot(
        sid, actual.cwd, actual.model, _digest(policy), static, call.runtime, _SEAL, effort
    )


async def capture_source(endpoint, thread_id, *, after_exit=False):
    async with _reader(endpoint) as call:
        if not after_exit:
            live = await _idle(call, thread_id)
            _, static = await _configuration(call, live.cwd, {"cwds": [live.cwd]})
            _, scoped = await _configuration(call, live.cwd, {"threadId": thread_id})
            call.live[thread_id] = live, static, scoped
            return live
        return await _after_source_disconnected(call, thread_id)


async def _after_source_disconnected(call, thread_id, *, fence=None):
    prior = call.live.pop(thread_id, None)
    _require(prior is not None, 'no live idle observation from this source runtime')
    live, static, scoped = prior
    _require(await _idle(call, thread_id) == live, 'source identity or model changed since live proof')
    _require((await _configuration(call, live.cwd, {'threadId': thread_id}))[1] == scoped,
             'source hook or policy changed since live proof')
    snapshot = await _evidence(call, thread_id, source=True, source_fence=fence)
    _require(snapshot._static == static, 'source configuration changed since live proof')
    if fence is not None:
        fence()
    return snapshot


async def capture_detached_source(endpoint, thread_id, *, transport, proof, generation, peer_pid):
    from .codex_live_transport import LiveTransport

    _require(isinstance(transport, LiveTransport), 'not an owned native transport')

    def fence():
        transport.assert_detached(proof, endpoint, generation, peer_pid)

    fence()
    async with _reader(endpoint) as call:
        fence()
        return await _after_source_disconnected(call, thread_id, fence=fence)


async def verify_target(endpoint, snapshot, *, model=None):
    require_snapshot(snapshot)
    from .codex_billing_route import _OWNED_SERVERS

    _require(snapshot._runtime not in _OWNED_SERVERS, "source runtime not retired")
    async with _reader(endpoint) as call:
        _, static = await _configuration(call, snapshot.cwd, {"cwds": [snapshot.cwd]})
        different = [name for name, digest in static if dict(snapshot._static).get(name) != digest]
        _require(
            static == snapshot._static,
            "target policy differs before resume: " + ", ".join(different),
        )
        target = await _evidence(call, snapshot.thread_id, source=False, model=model)
        assert_same_policy(snapshot, target)
        assert_same_selection(snapshot, target)
        return target


async def verify_attached_target(endpoint, snapshot, *, model, transport, generation, peer_pid):
    """Inspect the existing TUI's fenced resume, without creating another writer."""
    from .codex_billing_route import _OWNED_SERVERS
    from .codex_live_transport import LiveTransport

    require_snapshot(snapshot)
    _require(snapshot._runtime != endpoint and snapshot._runtime not in _OWNED_SERVERS,
             "source runtime not retired")
    _require(isinstance(transport, LiveTransport) and model == snapshot.model,
             "target transport or selected model changed")
    sid = snapshot.thread_id

    def observed():
        return transport.attached_resume(endpoint, sid, generation, peer_pid)

    observed()
    async with _reader(endpoint) as call:
        before = await _idle(call, sid)
        target = await _resume_snapshot(call, sid, observed(), before=before, model=model)
        assert_same_policy(snapshot, target)
        assert_same_selection(snapshot, target)
        observed()
        return target


def _retirement_process_alone(pid):
    """Affirm the entire group we would signal is the owned server and nothing else."""
    import subprocess
    import sys

    if sys.platform == 'darwin':
        from .native_process import darwin_server_alone
        return darwin_server_alone(pid)

    if type(pid) is not int or pid <= 1 or pid == os.getpgrp():
        return False
    try:
        output = subprocess.check_output(
            ["/bin/ps", "-axo", "pid=,ppid=,pgid="], stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=2,
        )
        rows = [tuple(map(int, line.split())) for line in output.splitlines()]
        return (rows.count((pid, os.getpid(), pid)) == 1
                and all(p == pid or (parent != pid and group != pid) for p, parent, group in rows))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


async def _retired_root(endpoint, sid, proc):
    """Independent post-unload evidence, never a live policy or switch ticket."""
    from .codex_billing_route import _OWNED_SERVERS

    async with _reader(endpoint) as call:
        params = {"threadId": sid}

        async def quiet():
            loaded = await call("thread/loaded/list", {"limit": 1})
            _require(loaded["data"] == [] and loaded["nextCursor"] is None, "runtime reloaded")
            diag = await call("server/diagnostics", {})
            gauges = {g["name"]: g["value"] for g in diag["gauges"]}
            _require(
                diag["process"]["id"] == proc.pid
                and len(gauges) == len(diag["gauges"])
                and all(type(v) is int for v in gauges.values())
                and gauges.get("core.threads.live") == 0
                and gauges.get("app.requests.in_flight") == 1  # this diagnostic RPC
                and gauges.get("app.requests.queued") == 0
                and all(v == 0 for k, v in gauges.items() if k != "app.requests.in_flight"),
                "owned server has pending or unknown work",
            )

        await quiet()
        thread = (await call("thread/read", {**params, "includeTurns": False}))["thread"]
        _require(thread["id"] == sid and thread["parentThreadId"] is None
                 and thread["status"] == {"type": "notLoaded"}, "expected unloaded root")
        # The default source filter includes only interactive threads. Include
        # all pinned source kinds when looking for stored children or side chats.
        sources = _all_sources()
        for method, query in [
            ("thread/queue/list", params), ("thread/attachment/list", params),
            ("thread/list", {"ancestorThreadId": sid, "modelProviders": [], "sourceKinds": sources}),
            ("thread/list", {"attachedToThreadId": sid, "modelProviders": [], "sourceKinds": sources}),
        ]:
            page = await call(method, {**query, "limit": 1})
            _require(page["data"] == [] and page["nextCursor"] is None, "pending " + method)
        _require((await call("thread/goal/get", params))["goal"] is None, "thread has a goal")
        _require(await asyncio.to_thread(_retirement_process_alone, proc.pid),
                 "owned process group or server still has children")
        await quiet()
        _require(_OWNED_SERVERS.get(endpoint) is proc and proc.returncode is None, "owner changed")


async def wait_until_idle(endpoint, thread_id=None, *, on_wait=None):
    """Retirement only; no resume, signals, grant or successor authorization."""
    from .codex_billing_route import _OWNED_SERVERS

    notified = False
    fast_until = asyncio.get_running_loop().time() + 3
    while True:
        proc = _OWNED_SERVERS.get(endpoint)
        _require(
            proc is not None and proc.returncode is None,
            "owned runtime exited before verified retirement",
        )
        try:
            inspect_unloaded = False
            async with _reader(endpoint) as call:
                loaded = await call("thread/loaded/list", {"limit": 2})
                if thread_id is None:
                    _require(
                        loaded["data"] == [] and loaded["nextCursor"] is None,
                        "owned server has loaded runtimes",
                    )
                elif loaded["data"] == [] and loaded["nextCursor"] is None:
                    params = {"threadId": thread_id, "includeTurns": False}
                    thread = (await call("thread/read", params))["thread"]
                    _require(
                        thread["id"] == thread_id and thread["status"] == {"type": "notLoaded"},
                        "unverified unloaded thread",
                    )
                    try:
                        await _empty_activity(call, thread_id)
                    except _Unverified as exc:
                        if str(exc) != "Codex handoff not verified: thread/backgroundTerminals/list unavailable.":
                            raise
                        # This result is NOT evidence. Close this connection,
                        # then independently inspect durable work and the OS group.
                        inspect_unloaded = True
                    again = await call("thread/loaded/list", {"limit": 1})
                    _require(
                        again["data"] == [] and again["nextCursor"] is None,
                        "runtime reloaded during drain",
                    )
                else:
                    await _idle(call, thread_id)
            if inspect_unloaded:
                await _retired_root(endpoint, thread_id, proc)
            return
        except _Unverified:
            if not notified and on_wait is not None:
                on_wait(
                    "The original Codex runtime is busy or unverifiable. No switch or termination was requested."
                )
            notified = True
            # A freshly detached UI and its last helper can unload less than
            # a second apart. Observe that window; a timer is never evidence.
            await asyncio.sleep(.05 if asyncio.get_running_loop().time() < fast_until else 1)
