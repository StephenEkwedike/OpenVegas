"""One numeric-loopback ASGI service, existing Bank plus existing supplier Fixture."""
import asyncio
import json
import sys
import httpx

from openvegas.config import FALLBACK_DEFAULT_BACKEND_URL as BACKEND
from openvegas.integrations.inference_router_fixture import SYNTHETIC_KEY
from scripts.qa_stock_managed_bank import Bank, DEVICE, GRANT
from scripts.windows_switch_rehearsal import Fixture, LIMIT

DEVICE_ROUTES = {("GET", "/integrations/account"), ("GET", "/integrations/native/options?host=codex"),
                 ("POST", "/integrations/native/quote"), ("POST", "/integrations/native/sessions")}
GRANT_ROUTES = {("GET", "/integrations/native/session"), ("DELETE", "/integrations/native/session"),
                ("GET", "/integrations/native/codex/catalog?client_version=0.166.0")}
AUTH = {b"authorization", b"x-api-key", b"cookie", b"proxy-authorization", b"chatgpt-account-id",
        b"openai-organization", b"openai-project", b"x-openvegas-local-auth"}


class Service:
    def __init__(self, retry=False):
        if sys.flags.optimize:
            raise ValueError("Fixture validation requires assertions enabled")
        self.bank, self.fixture = Bank("codex"), Fixture("codex", retry)
        self.port = None
        self.bad = self.bank_http = self.supplier_open = 0

    async def reject(self, scope, send):
        self.bad += 1
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            await send({"type": "http.response.start", "status": 403, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

    async def __call__(self, scope, receive, send):
        try:
            assert scope["type"] in ("http", "websocket")
            assert scope["server"] == ("127.0.0.1", self.port)
            assert scope["client"][0] == "127.0.0.1"
            headers = scope.get("headers", ())
            assert [v for k,v in headers if k == b"host"] == [("127.0.0.1:"+str(self.port)).encode()]
            observed = [(k,v) for k,v in headers if k in AUTH]
            raw = scope.get("raw_path", scope["path"].encode())
            assert raw == scope["path"].encode("ascii")
            query = scope.get("query_string", b"")
            path = scope["path"] + ("?" + query.decode("ascii") if query else "")
            key = (scope.get("method"), path)
            token = DEVICE if key in DEVICE_ROUTES else GRANT if key in GRANT_ROUTES else None
            if scope["type"] == "http" and token is not None:
                assert observed == [(b"authorization", ("Bearer "+token).encode())]
                body = bytearray()
                while True:
                    event = await asyncio.wait_for(receive(), 8)
                    assert event["type"] == "http.request"
                    body.extend(event.get("body", b""))
                    assert len(body) <= LIMIT
                    if not event.get("more_body"): break
                request = httpx.Request(key[0], BACKEND+path, content=bytes(body),
                                        headers={"authorization": "Bearer "+token})
                response = self.bank(request)
                self.bank_http += 1
                if response.status_code != 200:
                    return await self.reject(scope, send)
                await send({"type":"http.response.start","status":200,
                            "headers":[(b"content-type", b"application/json")]})
                await send({"type":"http.response.body","body":response.content})
                return
            mode = {"/_host/v1/responses":"host", "/integrations/native/v1/responses":"v"}.get(path)
            assert mode and (scope["type"] == "websocket" or scope["method"] == "POST")
            token = SYNTHETIC_KEY if mode == "host" else GRANT
            assert observed == [(b"authorization", ("Bearer "+token).encode())]
            assert mode == "host" or self.bank.state == "active"
        except (AssertionError, ValueError, KeyError, TypeError, UnicodeError, TimeoutError):
            return await self.reject(scope, send)
        self.supplier_open += 1
        return await self.fixture(scope, receive, send)


def verdict(app, lane):
    f, b = app.fixture, app.bank
    assert app.bad == 0 and not f.bad and not f.extra_root and f.helper_policy_ok()
    assert b.events.count("unexpected") == 0 and b.events.count("issue") == 1
    assert not (f.repeat_v or f.repeat_host or f.unmatched)
    if lane == "roundtrip":
        assert f.rows == ["host","v","host"] and f.sealed and f.rendered == 3
        assert f.switch_acks == 2 and f.status_checks == 3 and f.clean_exit
        assert all(e.is_set() for e in f.sent.values())
        assert b.state == "closed" and b.events.count("revoke") == 1
    else:
        assert lane == "retry" and f.rows == ["host"] and f.rendered == 1
        assert f.interrupted.is_set() and f.drop_armed and not f.sealed
        assert f.native_error and f.native_reconnect and not f.sent[f.answers[1]].is_set()
        assert f.switch_acks == 1 and f.status_checks >= 2 and not f.clean_exit
        assert b.state == "active" and b.events.count("revoke") == 0
    return snapshot(app)


def snapshot(app):
    f, b = app.fixture, app.bank
    return {"rendered":f.rendered,"root_completed":len(f.rows),"switches":f.switch_acks,
            "statuses":f.status_checks,"exit_zero":int(f.clean_exit),"issue":b.events.count("issue"),
            "revoke":b.events.count("revoke"),"wire_drop":int(f.interrupted.is_set()),
            "retry_ui":int(f.native_reconnect),"error_ui":int(f.native_error),
            "repeat_v":f.repeat_v,"repeat_host":f.repeat_host,"unmatched":f.unmatched,
            "wallet_title":f.denied_wallet,"host_title":f.denied_titles-f.denied_wallet,
            "bank_http":app.bank_http,"supplier_open":app.supplier_open,
            "service_bad":app.bad,"fixture_bad":int(f.bad),"bank_bad":b.events.count("unexpected")}
