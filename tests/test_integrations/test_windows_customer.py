"""Windows product wiring with an in-memory bank, not native or paid acceptance."""

import asyncio
import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio
from click.testing import CliRunner

from openvegas.integrations import commands, windows_billing_host
from openvegas.integrations import windows_customer as win
from openvegas.integrations import windows_customer_claude as claude
from openvegas.integrations import windows_customer_codex as codex
from openvegas.integrations.broker import Broker
from openvegas.integrations.credentials import IntegrationError

SID = "a1644bc1-e7fd-47f2-88f0-4c5cbba02c82"
OTHER = "7078ce3b-c832-46b9-bea3-5458107fce14"
URL = "http://127.0.0.1:13212"
DEVICE = "ov_device_synthetic"
TOKEN = "ov_native_synthetic_control_only"
CWD = r"C:\QA\work"


class Bank:
    def __init__(self, host):
        self.host = host
        self.model = "gpt-native" if host == "codex" else "claude-native"
        self.provider = "openai" if host == "codex" else "anthropic"
        self.calls = []
        self.reject = None

    def http(self, req):
        path = req.url.raw_path.decode().removeprefix("/integrations")
        self.calls.append((req.method, path))
        if self.reject == path:
            return httpx.Response(503, json={})
        assert req.headers["authorization"] == "Bearer " + (
            TOKEN if path == "/native/session" else DEVICE
        )
        fields = {
            "host": self.host,
            "model": self.model,
            "provider": self.provider,
            "max_tokens": 512,
        }
        pricing = {
            "model": self.model,
            "provider": self.provider,
            "fingerprint": "a" * 64,
            "v_per_usd": "100",
            "multiplier": "1",
            "supplier_input_usd_per_million": "1",
            "supplier_output_usd_per_million": "2",
        }
        status = {
            "host": self.host,
            "model": self.model,
            "session_id": "fixture",
            "billing_mode": "wallet",
            "allowed_models": [self.model],
        }
        if path == "/account":
            assert req.method == "GET"
            value = {"balance": "73.25"}
        elif path == "/native/options?host=" + self.host:
            assert req.method == "GET"
            value = {
                **fields,
                "wallet_limit_policy": "live_balance",
                "wallet_billing": True,
                "balance_v": "73.25",
                "models": [fields],
            }
        elif path == "/native/quote":
            assert req.method == "POST" and json.loads(req.content) == fields
            value = {**fields, "pricing": pricing}
        elif path == "/native/sessions":
            assert req.method == "POST" and json.loads(req.content) == {
                **fields,
                "confirmed": True,
                "billing_mode": "wallet",
                "pricing_fingerprint": "a" * 64,
            }
            value = {
                **status,
                "token": TOKEN,
                "pricing": pricing,
                "expires_in": 600,
                "catalog_hash": "b" * 64,
            }
        else:
            assert path == "/native/session" and req.method in ("GET", "DELETE")
            value = status if req.method == "GET" else {"closed": True}
        return httpx.Response(200, json=value)


@pytest_asyncio.fixture
async def rig(monkeypatch, request):
    from openvegas.integrations import inference_router_session, inference_router_wallet

    bank = Bank(request.param)
    broker = Broker(backend="https://bank.invalid", transport=httpx.MockTransport(bank.http))
    broker.credentials = SimpleNamespace(load=lambda: DEVICE)
    leases = Mock()
    monkeypatch.setattr(win, "SessionLease", leases)
    monkeypatch.setattr(inference_router_session, "native_tcp_peer_auth", lambda: AsyncMock())
    catalog = AsyncMock(return_value={"models": []})
    monkeypatch.setattr(inference_router_wallet, "_grant_catalog", catalog)
    origin = codex.ORIGINS["chatgpt"] if bank.host == "codex" else "https://api.anthropic.com"
    owner = win.Customer(bank.host, broker, origin, CWD)
    owner.session._url = URL
    pick = win.Selection(SID, bank.model, CWD, URL)
    try:
        yield owner, bank, pick, leases, catalog
    finally:
        await owner.session.controls.settle_callbacks()
        await broker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("rig", ["codex", "claude"], indirect=True)
async def test_authenticated_status_and_one_shot_real_protocol(rig):
    owner, bank, pick, leases, catalog = rig
    observe, guard = AsyncMock(return_value=pick), Mock()

    async def cmd(action):
        return await owner.command(action, observe=observe, guard=guard)

    assert not bank.calls
    assert await cmd("status") == "Payer: host; $V 73.25."
    assert await cmd("v") == "Payer: v. No prompt sent."
    assert await cmd("v") == "Payer: v. No prompt sent."
    assert owner.session._wallet.token == TOKEN
    assert await cmd("status") == "Payer: v; $V 73.25."
    assert await cmd("host") == "Payer: host. No prompt sent."
    assert owner.session._wallet is None and owner.session.state.generation == 2
    assert bank.calls == [
        ("GET", "/account"),
        ("GET", "/native/options?host=" + bank.host),
        ("POST", "/native/quote"),
        ("POST", "/native/sessions"),
        ("GET", "/native/session"),
        ("GET", "/account"),
        ("DELETE", "/native/session"),
    ]
    assert observe.await_count == 4
    leases.assert_called_once_with(bank.host, SID)
    if bank.host == "codex":
        assert catalog.await_args.kwargs["identifiers"] == (bank.model,)
    await owner.close()
    leases.return_value.close.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("rig", ["codex", "claude"], indirect=True)
@pytest.mark.parametrize(
    "failure", ["unknown_issue", "changed_model", "wrong_route", "unknown_close"]
)
async def test_uncertainty_never_admits_a_fallback_or_retries(rig, failure):
    owner, bank, pick, *_ = rig
    obs = AsyncMock(return_value=pick)
    if failure == "unknown_issue":
        bank.reject = "/native/sessions"
    elif failure == "wrong_route":
        obs.return_value = replace(pick, base_url="https://untrusted.invalid")
    elif failure == "changed_model":
        obs.side_effect = [pick, replace(pick, model=pick.model + "-changed")]

    async def cmd(action):
        return await owner.command(action, guard=lambda: None, observe=obs)

    message = await cmd("v")
    if failure == "unknown_close":
        assert "Payer: v" in message
        bank.reject = "/native/session"
        message = await cmd("host")
    if failure == "wrong_route":
        assert not bank.calls and owner.session.state.pending is None
        assert "refused" in message
        return
    assert "unconfirmed" in message and owner.session.state.pending is not None
    assert "unconfirmed" in await cmd("v") and "unconfirmed" in await cmd("host")
    await owner.session.controls.settle_callbacks()
    if failure in ("unknown_issue", "unknown_close"):
        with pytest.raises(IntegrationError, match="unconfirmed"):
            await owner.close()
    else:
        await owner.close()
    assert bank.calls.count(("POST", "/native/sessions")) == 1
    assert bank.calls.count(("DELETE", "/native/session")) == (failure != "unknown_issue")


@pytest.mark.asyncio
@pytest.mark.parametrize("rig", ["claude"], indirect=True)
async def test_claude_native_challenges_match_same_session_once(rig):
    owner, bank, pick, *_ = rig
    ingress = claude.Ingress(owner, AsyncMock(return_value=True))
    ingress.process = SimpleNamespace(poll=lambda: None)
    value = asdict(pick)
    assert await ingress.exchange({"event": "ready", "value": value}) == {"message": "ready"}
    first = await ingress.exchange({"event": "start", "action": "v"})
    assert not bank.calls
    with pytest.raises(IntegrationError):
        await ingress.exchange(
            {
                "event": "observe",
                "challenge": first["challenge"],
                "value": {**value, "identifier": OTHER},
            }
        )
    second = await ingress.exchange({"event": "observe", **first, "value": value})
    assert second != first
    with pytest.raises(IntegrationError):
        await ingress.exchange({"event": "observe", **first, "value": value})
    assert await ingress.exchange({"event": "observe", **second, "value": value}) == {
        "message": "Payer: v. No prompt sent."
    }
    await ingress.close()
    await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("rig", ["claude"], indirect=True)
async def test_lost_challenge_recovers_without_issuing(rig):
    owner, bank, pick, *_ = rig
    ingress = claude.Ingress(owner, AsyncMock(return_value=True))
    ingress.process = SimpleNamespace(poll=lambda: None)
    await ingress.exchange({"event": "ready", "value": asdict(pick)})
    owner.session.controls.timeout = 0.03
    await ingress.exchange({"event": "start", "action": "v"})
    await ingress.task
    assert not bank.calls
    assert await ingress.exchange({"event": "start", "action": "status"}) == {
        "message": "Payer: host; $V 73.25."
    }
    await ingress.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("rig", ["claude"], indirect=True)
@pytest.mark.parametrize("peer, key", [(False, "correct"), (True, "wrong"), (True, "duplicate")])
async def test_claude_requires_pinned_process_and_private_plugin_key(rig, peer, key):
    owner, bank, *_ = rig
    gate = claude.Ingress(owner, AsyncMock(return_value=peer))
    gate.authority = "127.0.0.1:13212"
    values = (
        [gate.key]
        if key == "correct"
        else [gate.key, gate.key]
        if key == "duplicate"
        else ["wrong"]
    )
    scope = {
        "type": "http",
        "path": "/ov-local-control",
        "method": "POST",
        "headers": [
            (b"host", gate.authority.encode()),
            *((b"x-ov-command", v.encode()) for v in values),
        ],
    }
    send, receive = AsyncMock(), AsyncMock()
    await gate(scope, receive, send)
    assert send.await_args_list[0].args[0]["status"] == 403
    receive.assert_not_awaited()
    assert not bank.calls


def test_explicit_installed_dispatch_only(monkeypatch):
    monkeypatch.setattr(commands, "os", SimpleNamespace(name="nt"))
    run = Mock(return_value=17)
    monkeypatch.setattr(windows_billing_host, "launch", run)
    cli = CliRunner()
    for host in ("codex", "claude"):
        argv = ["--attach", "--host", host]
        assert cli.invoke(commands.terminal, [*argv, "--managed-billing"]).exit_code == 17
        run.assert_called_with(host, managed=True)
        assert cli.invoke(commands.terminal, argv).exit_code == 17
        run.assert_called_with(host, fixture_port=None)
    run.reset_mock()
    for args in (
        [],
        ["--attach"],
        ["--host", "codex"],
        ["--attach", "--host", "codex", "--windows-fixture-port", "8000"],
        ["--windows-session", "a", "--payer", "v"],
        ["--attach", "--host", "codex", "--apply"],
    ):
        assert cli.invoke(commands.terminal, [*args, "--managed-billing"]).exit_code == 2
    run.assert_not_called()


@pytest.mark.parametrize(
    "overrides", [{"https_proxy": "https://unused.invalid"}, {"openvegas_native_token": "fixture"}]
)
def test_env_does_not_overlay_existing_routes(overrides):
    with pytest.raises(IntegrationError):
        win.environment(overrides)
    assert win.environment(
        {
            "Path": "a",
            "OpenVegas_Terminal_Token": "private",
            "OV_PRIVATE": "private",
            "OPENAI_API_KEY": "synthetic",
        }
    ) == {"PATH": "a", "OPENAI_API_KEY": "synthetic"}


@pytest.mark.parametrize(
    "method, item, expected",
    [
        ("turn/start", {"type": "text", "text": "openvegas use v"}, "v"),
        ("turn/start", {"type": "text", "text": "openvegas STATUS"}, "status"),
        ("turn/steer", {"type": "text", "text": "openvegas use host"}, "host"),
        ("turn/start", {"type": "text", "text": "openvegas use v\nthen do work"}, None),
        ("turn/start", {"type": "image", "text": "openvegas use v"}, None),
        ("turn/start", {"type": "text", "text": "openvegas use v", "text_elements": [{}]}, None),
        ("item/completed", {"type": "text", "text": "openvegas use v"}, None),
    ],
)
def test_codex_intercepts_only_single_plain_native_control(method, item, expected):
    assert codex.action_for(method, {"input": [item]}) == expected
    assert codex.action_for(method, {"input": [item, item]}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("rig", ["codex"], indirect=True)
@pytest.mark.parametrize("interleave", ["quiet", "title", "root-before", "root-after", "unknown"])
async def test_native_stdio_bridge_forwards_normal_work_but_not_controls(rig, tmp_path, interleave):
    from websockets.protocol import State

    owner, bank, pick, *_ = rig
    cwd = str(tmp_path)
    owner.cwd = cwd
    params = {
        "threadId": SID,
        "model": pick.model,
        "effort": "high",
        "sandboxPolicy": {"type": "readOnly"},
        "approvalPolicy": "on-request",
        "input": [{"type": "text", "text": "openvegas use v"}],
    }
    native_thread = {
        "id": SID,
        "modelProvider": "openai",
        "cliVersion": "0.166.0",
        "ephemeral": False,
        "parentThreadId": None,
        "status": {"type": "idle"},
        "canAcceptDirectInput": True,
        "model": pick.model,
        "cwd": cwd,
        "environments": [{"environmentId": "local", "cwd": cwd, "runtimeWorkspaceRoots": [cwd]}],
    }
    c2s, s2c = asyncio.Queue(), asyncio.Queue()

    class TUI:
        state = State.OPEN

        def __aiter__(self):
            return self

        async def __anext__(self):
            msg = await c2s.get()
            if msg is None:
                raise StopAsyncIteration
            return json.dumps(msg)

        async def send(self, raw):
            await s2c.put(json.loads(raw))

    incoming = asyncio.StreamReader()
    native_calls = []

    class Stdin:
        def write(self, raw):
            msg = json.loads(raw)
            method = msg["method"]
            native_calls.append((method, msg["params"]))
            response = {
                "config/read": {"config": {"openai_base_url": URL}},
                "account/read": {"account": {"type": "chatgpt"}},
                "thread/loaded/list": {"data": [OTHER, SID], "nextCursor": None},
                "thread/start": {
                    "thread": {**native_thread, "id": OTHER, "ephemeral": True}
                    if msg["params"].get("ephemeral")
                    else native_thread
                },
                "thread/read": {"thread": native_thread},
                "turn/start": {"turn": {"id": "native-turn", "status": "inProgress"}},
            }[method]
            if method == "thread/read":
                checked = sum(m == method for m, _ in native_calls)
                sid = {
                    "title": OTHER,
                    "root-before": SID if checked == 1 else None,
                    "root-after": SID if checked == 2 else None,
                    "unknown": "eae0bbff-4e67-4563-842b-fbcee7246374",
                }.get(interleave)
                if sid:
                    notice = {"method": "turn/completed", "params": {"threadId": sid}}
                    incoming.feed_data(json.dumps(notice).encode() + b"\n")
            incoming.feed_data(json.dumps({"result": response, "id": msg["id"]}).encode() + b"\n")

        async def drain(self):
            pass

    pipe = codex.Pipe(SimpleNamespace(returncode=None, stdout=incoming, stdin=Stdin()))
    ws = pipe.client = TUI()
    bridge = asyncio.create_task(codex.bridge(ws, pipe, owner, SimpleNamespace(poll=lambda: None)))

    async def send(seq, values, method="turn/start"):
        await c2s.put({"id": seq, "method": method, "params": values})
        async with asyncio.timeout(2):
            result = await s2c.get()
            while result.get("method") == "turn/completed":
                result = await s2c.get()
        assert result["id"] == seq
        return result

    try:
        assert (await send(10, {"ephemeral": True}, "thread/start"))["result"]["thread"][
            "id"
        ] == OTHER
        assert (await send(11, {}, "thread/start"))["result"]["thread"]["id"] == SID
        message = (await send(1, params))["error"]["message"]
        if interleave in ("root-before", "unknown", "root-after"):
            assert message.endswith("Control check: native-fence.")
            if interleave == "root-after":
                assert "unconfirmed; inference held" in message and owner.session.state.pending
                assert bank.calls.count(("POST", "/native/sessions")) == 1
            else:
                assert "refused; payer unchanged" in message and owner.session.state.pending is None
                assert not bank.calls
            assert owner.session.state.mode == "host"
            assert all(method != "turn/start" for method, _ in native_calls)
            return
        assert message == "Payer: v. No prompt sent."
        ordinary = {**params, "input": [{"type": "text", "text": "A normal synthetic question"}]}
        assert (await send(2, ordinary))["result"]["turn"]["id"] == "native-turn"
        assert (
            await send(3, {**params, "input": [{"type": "text", "text": "openvegas use host"}]})
        )["error"]["message"] == "Payer: host. No prompt sent."
        assert [args for method, args in native_calls if method == "turn/start"] == [ordinary]
        assert (
            bank.calls.count(("POST", "/native/sessions"))
            == bank.calls.count(("DELETE", "/native/session"))
            == 1
        )
    finally:
        await c2s.put(None)
        await bridge
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,expected",
    [("chatgpt", codex.ORIGINS["chatgpt"]), ("apiKey", codex.ORIGINS["apiKey"]), ("unknown", None)],
)
async def test_native_auth_kind_not_an_inferred_model_route(kind, expected):
    rpc = SimpleNamespace(call=AsyncMock(side_effect=[{"config": {}}, {"account": {"type": kind}}]))
    if expected is None:
        with pytest.raises(IntegrationError):
            await codex.route(rpc, CWD)
    else:
        assert await codex.route(rpc, CWD) == expected
    assert [c.args for c in rpc.call.await_args_list] == [
        ("config/read", {"cwd": CWD, "includeLayers": True}),
        ("account/read", {"refreshToken": False}),
    ]


@pytest.mark.parametrize(
    "setting",
    [
        "ANTHROPIC_CUSTOM_HEADERS",
        "Claude_Code_Use_Bedrock",
        "https_proxy",
        "ANTHROPIC_VERTEX_BASE_URL",
    ],
)
def test_claude_refuses_settings_routing_overrides(monkeypatch, tmp_path, setting):
    import sys

    from openvegas.emotes import _windows_resources
    from openvegas.integrations import windows_session_lease

    def missing(*args):
        raise FileNotFoundError

    monkeypatch.setitem(
        sys.modules,
        "winreg",
        SimpleNamespace(
            HKEY_LOCAL_MACHINE=1,
            HKEY_CURRENT_USER=2,
            KEY_WOW64_64KEY=4,
            KEY_WOW64_32KEY=8,
            KEY_READ=16,
            OpenKey=missing,
        ),
    )
    roots = []

    def known(key):
        roots.append(key)
        return tmp_path / "machine"

    monkeypatch.setattr(windows_session_lease, "known_folder", known)
    monkeypatch.setattr(
        _windows_resources,
        "read_windows",
        lambda root, parts, limit: (root / parts[0]).read_bytes(),
    )
    env = {"USERPROFILE": str(tmp_path / "profile")}
    cwd = tmp_path / "project"
    cwd.mkdir()
    claude.check_settings(env, str(cwd))
    assert len(set(roots)) == 3
    root = cwd / ".claude"
    root.mkdir()
    (root / "settings.json").write_text(json.dumps({"env": {setting: "synthetic"}}))
    with pytest.raises(IntegrationError):
        claude.check_settings(env, str(cwd))


def test_plugin_serves_only_a_composer_command_without_using_model(tmp_path):
    import os
    import shutil
    import subprocess

    exe = shutil.which("node")
    if not exe:
        pytest.skip("Node needed for the Claude module unit test")
    path = tmp_path / "plugin.mjs"
    path.write_text(
        claude.PLUGIN.replace("__BASE__", json.dumps(URL)).replace("__KEY__", '"dummy"')
    )
    script = """
import assert from 'node:assert/strict';
import {register} from './plugin.mjs';
const events = {}, seen = [], value = {identifier:'id', model:'claude-native', cwd:'C:/work',base_url:'http://127.0.0.1:13212'};
register((name,...rest) => events[name] = rest.at(-1));
const $ = {command:{register:async spec=>assert.equal(spec.immediate,true)},settings:{read:async()=>({})},
  session:{version:async()=>({version:'2.1.322'}),id:async()=>value.identifier,
    model:async()=>value.model,cwd:async()=>value.cwd},env:{get:async()=>value.base_url},
  http:{fetch:async(url,req)=>{
    assert.equal(url,value.base_url+'/ov-local-control'); assert.equal(req.headers['X-OV-Command'],'dummy');
    const msg=JSON.parse(req.body); seen.push(msg);
    return {ok:true,text:JSON.stringify(msg.event==='start'?{challenge:'fresh'}:{message:'local result'})};
  }}};
await events['session.start']($,{},async()=>true);
for (const kind of ['plugin','bridge','sdk']) await events['command.run']($,{args:'v',origin:{kind}});
assert.equal(seen.length,1);
const reply=await events['command.run']($,{args:'v',origin:{kind:'composer'}});
assert.equal(reply.text,'local result');
assert.deepEqual(seen,[{event:'ready',value},{event:'start',action:'v'},{event:'observe',challenge:'fresh',value}]);
"""
    driver = tmp_path / "driver.mjs"
    driver.write_text(script, encoding="utf-8")
    env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR", "TEMP", "TMP") if k in os.environ}
    subprocess.run([exe, str(driver)], cwd=tmp_path, env=env, check=True, timeout=10)


def test_workspace_comparison_does_not_follow_network_or_fold_directory_case():
    assert win.same_workspace(r"\\?\C:\QA\work", "C:/QA/work")
    assert not win.same_workspace(r"\\server\share", r"\\server\share")
    assert not win.same_workspace(CWD, CWD.lower())


@pytest.mark.asyncio
@pytest.mark.parametrize("rig", ["claude"], indirect=True)
async def test_replayed_stale_boundary_cannot_install_session_lease(rig):
    owner, _, _, leases, _ = rig
    with pytest.raises(IntegrationError):
        await owner.boundary(object())
    leases.assert_not_called()


@pytest.mark.parametrize(
    "method,params,ok",
    [
        ("config/value/write", {"keyPath": "model", "value": "gpt-native"}, True),
        (
            "config/batchWrite",
            {
                "edits": [
                    {"keyPath": "model", "value": "gpt-native"},
                    {"keyPath": "model_reasoning_effort", "value": "high"},
                ]
            },
            True,
        ),
        (
            "config/batchWrite",
            {
                "edits": [
                    {"keyPath": "model", "value": "gpt-native"},
                    {"keyPath": "openai_base_url", "value": "elsewhere"},
                ]
            },
            False,
        ),
        (
            "config/value/write",
            {"filePath": "alternate.toml", "keyPath": "model", "value": "gpt-native"},
            False,
        ),
        ("config/value/write", {"keyPath": "model", "value": {"model_provider": "custom"}}, False),
        ("config/value/write", {"keyPath": "model_provider", "value": "custom"}, False),
    ],
)
def test_native_picker_writes_do_not_open_config_or_route_bypass(method, params, ok):
    assert codex.picker_write(method, params) is ok


def test_device_refuses_keyring_fallback_before_credential_read(monkeypatch):
    import sys

    import keyring

    class Vault:
        pass

    monkeypatch.setitem(
        sys.modules, "keyring.backends.Windows", SimpleNamespace(WinVaultKeyring=Vault)
    )
    fallback = Mock()
    monkeypatch.setattr(keyring, "get_keyring", lambda: fallback)
    credentials = Mock()
    credentials.load.return_value = DEVICE
    wrapped = win.WindowsDevice(credentials)
    with pytest.raises(IntegrationError):
        wrapped.load()
    credentials.load.assert_not_called()
    monkeypatch.setattr(keyring, "get_keyring", Vault)
    assert wrapped.load() == wrapped.load() == DEVICE
    credentials.load.assert_called_once_with()


def test_nonproduction_backend_fails_before_opening_client(monkeypatch):
    from openvegas.integrations import credentials

    monkeypatch.setattr(credentials, "backend_url", lambda: "http://127.0.0.1:8100")
    monkeypatch.setattr(
        win.DeviceBroker, "__init__", lambda *_a, **_kw: pytest.fail("opened client")
    )
    with pytest.raises(IntegrationError):
        win.Broker()


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "unmatched_reply",
        "active_ephemeral",
        "used_ephemeral",
        "other_root",
        "paginated",
        "ambiguous",
        "pending_lifecycle",
        "unsubscribed_root",
    ],
)
def test_codex_inventory_never_silently_adopts_another_chat(case):
    threads = codex.NativeThreads()

    def root(key, sid, ephemeral):
        threads.sent({"method": "thread/start", "params": {}, "id": key})
        threads.received(
            {
                "id": key,
                "result": {"thread": {"id": sid, "ephemeral": ephemeral, "parentThreadId": None}},
            }
        )

    root(1, SID, False)
    report = {"data": [OTHER, SID], "nextCursor": None}
    if case == "unmatched_reply":
        threads.received({"id": 99, "result": {"thread": {"id": OTHER, "ephemeral": True}}})
    elif case != "unknown":
        root(2, OTHER, case != "other_root")
        if case == "active_ephemeral":
            threads.received({"method": "turn/started", "params": {"threadId": OTHER}})
        elif case == "used_ephemeral":
            threads.sent({"method": "turn/start", "id": 3, "params": {"threadId": OTHER}})
        elif case == "paginated":
            report["nextCursor"] = "more"
        elif case == "ambiguous":
            report["data"].append(OTHER)
        elif case == "pending_lifecycle":
            threads.sent({"method": "thread/resume", "id": 6, "params": {}})
        elif case == "unsubscribed_root":
            threads.sent({"method": "thread/unsubscribe", "id": 6, "params": {"threadId": SID}})
            threads.received({"id": 6, "result": {"status": "unsubscribed"}})
    with pytest.raises(IntegrationError):
        threads.verify(SID, report)


@pytest.mark.parametrize("operation", ["ws", "responses", "compact", "search"])
@pytest.mark.parametrize(
    "claim", ["missing", "foreign", "conflict", "duplicate", "duplicate-json", "root"]
)
def test_pinned_root_required_before_any_billable_wallet_plan(operation, claim):
    adapter = codex.RootAdapter(
        "codex", codex.ORIGINS["apiKey"], "https://test.invalid/integrations/native", lambda: SID
    )
    direct = [("thread-id", SID if claim != "foreign" else OTHER)] if claim != "missing" else []
    extra = []
    if claim == "conflict":
        extra.append(("x-codex-turn-metadata", json.dumps({"thread_id": OTHER})))
    if claim == "duplicate":
        extra.extend(direct)
    if claim == "duplicate-json":
        extra.append(("x-codex-turn-metadata", '{"thread_id":"other","thread_id":"' + SID + '"}'))
    method, path, transport = {
        "ws": ("GET", "/responses", [("upgrade", "websocket")]),
        "responses": ("POST", "/responses", []),
        "compact": ("POST", "/responses/compact", []),
        "search": ("POST", "/alpha/search", []),
    }[operation]
    if claim == "root" and operation == "search":
        direct = [("x-codex-turn-metadata", json.dumps({"thread_id": SID}))]
    if claim == "root":
        plan = adapter.request_plan(method, path, direct + extra + transport, "v", TOKEN)
        assert dict(plan.headers)["authorization"] == "Bearer " + TOKEN
    else:
        with pytest.raises(IntegrationError):
            adapter.request_plan(method, path, direct + extra + transport, "v", TOKEN)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,thread,embedded,admitted",
    [
        ("host", OTHER, OTHER, True),
        ("v", SID, SID, True),
        ("v", OTHER, OTHER, False),
        ("v", SID, OTHER, False),
        ("v", None, SID, False),
        ("v", None, None, False),
    ],
)
async def test_each_socket_create_checked_again_without_rewriting_bytes(
    mode, thread, embedded, admitted
):
    observed = []

    class Receiver:
        state = SimpleNamespace(mode=mode)

        async def __call__(self, scope, receive, send):
            observed.append(await receive())

    guard = codex.RootIngress(Receiver(), lambda: SID)
    meta = {"thread_id": thread} if thread else {}
    if embedded:
        meta["x-codex-turn-metadata"] = json.dumps({"thread_id": embedded})
    event = {
        "type": "websocket.receive",
        "text": json.dumps(
            {
                "type": "response.create",
                "client_metadata": meta,
                "input": [{"type": "text", "text": "ordinary"}],
            }
        ),
    }
    if admitted:
        await guard({"type": "websocket"}, AsyncMock(return_value=event), AsyncMock())
        assert observed == [event] and observed[0] is event
    else:
        with pytest.raises(IntegrationError):
            await guard({"type": "websocket"}, AsyncMock(return_value=event), AsyncMock())
        assert not observed


def test_used_ephemeral_is_eligible_only_under_root_wire_containment():
    inventory = codex.NativeThreads()
    for key, sid, ephemeral in [(1, SID, False), (2, OTHER, True)]:
        inventory.sent({"id": key, "method": "thread/start"})
        inventory.received({"id": key, "result": {"thread": {"id": sid, "ephemeral": ephemeral}}})
    inventory.work(OTHER)
    report = {"data": [SID, OTHER]}
    with pytest.raises(IntegrationError):
        inventory.verify(SID, report)
    inventory.verify(SID, report, bounded=True)
    with pytest.raises(IntegrationError):
        inventory.verify(
            SID, {**report, "data": [SID, "eae0bbff-4e67-4563-842b-fbcee7246374"]}, bounded=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("rig", ["codex"], indirect=True)
async def test_fence_requires_identical_root_authority_at_both_admission_layers(rig):
    owner, *_ = rig
    session = owner.session
    assert codex.root_bounded(session)
    session.router.root = lambda: OTHER
    assert not codex.root_bounded(session)
    session.router.root = session.adapter.root
    session.router.inner.adapter = replace(session.adapter, root=lambda: OTHER)
    assert not codex.root_bounded(session)


def test_ephemeral_work_exemption_never_exempts_lifecycle_or_unbounded_work():
    threads = codex.NativeThreads()
    threads.sent({"id": 1, "method": "thread/start"})
    threads.received({"id": 1, "result": {"thread": {"id": OTHER, "ephemeral": True}}})
    bounded, unbounded = threads.fence(True), threads.fence(False)
    threads.work(OTHER)
    assert threads.fence(True) == bounded and threads.fence(False) != unbounded
    threads.sent({"id": 2, "method": "thread/unsubscribe", "params": {"threadId": OTHER}})
    after_send = threads.fence(True)
    assert after_send != bounded
    threads.received({"id": 2, "result": {}})
    assert threads.fence(True) != after_send


@pytest.mark.asyncio
@pytest.mark.parametrize("rig", ["codex"], indirect=True)
@pytest.mark.parametrize("step", ["native-workspace", "session-lease", "wallet-prepare"])
async def test_codex_refusal_names_first_check_without_leaking_native_details(rig, step):
    owner, bank, pick, leases, _ = rig
    if step == "native-workspace":
        pick = replace(pick, cwd="not-the-owned-workspace")
    elif step == "session-lease":
        leases.side_effect = OSError("private diagnostic must not reach the composer")
    else:
        bank.reject = "/native/options?host=codex"
    result = await owner.command("v", observe=AsyncMock(return_value=pick), guard=lambda: None)
    assert result == f"Switch refused; payer unchanged. No prompt sent. Control check: {step}."
    assert owner.session.state.pending is None and owner.session.state.mode == "host"
    assert bank.calls == (
        [("GET", "/native/options?host=codex")] if step == "wallet-prepare" else []
    )
