"""Private Codex server auth and lifecycle; never a real provider call."""

import asyncio
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from openvegas.integrations import codex_billing_route as module
from openvegas.integrations.credentials import IntegrationError

TOKEN = "ov_native_offline_route_fixture"
BASE = "http://127.0.0.1:12345"


@pytest.fixture
def rig(monkeypatch):
    calls, spawned = [], []
    config = {
        "cli_auth_credentials_store": "ephemeral",
        "model_provider": "openai",
        "openai_base_url": BASE + "/integrations/native/v1",
    }
    process = SimpleNamespace(returncode=None, terminated=0)

    def terminate():
        process.terminated += 1
        process.returncode = 0

    async def wait():
        return process.returncode

    process.terminate, process.wait = terminate, wait
    process.kill = terminate

    async def spawn(*args, **kwargs):
        spawned.append((args, kwargs))
        socket = Path(args[-1].removeprefix("unix://"))
        socket.touch()
        return process

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, message):
            value = json.loads(message)
            calls.append(value)
            self.value = value

        async def recv(self):
            method = self.value["method"]
            result = (
                {"config": config}
                if method == "config/read"
                else ({"account": {"type": "apiKey"}} if method == "account/read" else {})
            )
            return json.dumps({"id": self.value["id"], "result": result})

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "unix_connect", lambda *a, **k: Connection())
    return SimpleNamespace(calls=calls, spawned=spawned, config=config, process=process)


def test_private_ephemeral_verified_before_login_and_cleaned(rig):
    async def check():
        async with module.route(
            "/fixture/codex", BASE, "exact-model", {"OPENVEGAS_NATIVE_TOKEN": TOKEN}
        ) as endpoint:
            path = Path(endpoint.removeprefix("unix://"))
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            assert rig.process.returncode is None
            return path

    path = asyncio.run(check())
    assert not path.exists() and not path.parent.exists()
    assert rig.process.terminated == 1
    methods = [c["method"] for c in rig.calls]
    assert methods.index("config/read") < methods.index("account/login/start")
    assert rig.calls[methods.index("account/login/start")]["params"] == {
        "type": "apiKey",
        "apiKey": TOKEN,
    }
    argv, kwargs = rig.spawned[0]
    assert TOKEN not in str(argv)
    assert kwargs["env"]["OPENVEGAS_NATIVE_TOKEN"] == TOKEN
    assert 'model_provider="openai"' in argv
    assert 'cli_auth_credentials_store="ephemeral"' in argv
    assert 'web_search="disabled"' in argv
    assert "--startup-sign-out" not in argv


@pytest.mark.parametrize(
    "key,value",
    [
        ("cli_auth_credentials_store", "file"),
        ("cli_auth_credentials_store", "keyring"),
        ("cli_auth_credentials_store", None),
        ("model_provider", "different"),
        ("openai_base_url", "https://wrong.invalid"),
    ],
)
def test_effective_configuration_mismatch_fails_before_credentials(rig, key, value):
    rig.config[key] = value

    async def check():
        async with module.route(
            "/fixture/codex", BASE, "exact-model", {"OPENVEGAS_NATIVE_TOKEN": TOKEN}
        ):
            pytest.fail("unsafe configuration accepted")

    with pytest.raises(IntegrationError, match="configuration cannot isolate"):
        asyncio.run(check())
    assert all(c["method"] != "account/login/start" for c in rig.calls)
    assert rig.process.terminated == 1


def test_missing_grant_does_not_start_process(rig):
    async def check():
        async with module.route("/fixture/codex", BASE, "exact-model", {}):
            pytest.fail("missing auth accepted")

    with pytest.raises(IntegrationError, match="Missing OpenVegas"):
        asyncio.run(check())
    assert not rig.spawned


def test_cancellation_cleans_owned_server(rig):
    async def check():
        async with module.route(
            "/fixture/codex", BASE, "exact-model", {"OPENVEGAS_NATIVE_TOKEN": TOKEN}
        ):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(check())
    assert rig.process.terminated == 1


def test_error_does_not_expose_credentials(rig):
    async def check():
        async with module.route(
            "/fixture/codex", BASE, "exact-model", {"OPENVEGAS_NATIVE_TOKEN": TOKEN}
        ):
            raise RuntimeError(TOKEN)

    with pytest.raises(IntegrationError) as error:
        asyncio.run(check())
    assert TOKEN not in str(error.value)
    assert rig.process.terminated == 1
