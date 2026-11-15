"""Loopback-only synthetic wire fixture for the packaged Windows rehearsal. No upstream client."""
import asyncio
import json
import re
from secrets import token_hex
import socket
import subprocess
import threading
import time
import zlib
from uuid import UUID
from contextvars import ContextVar

MODES = ("host", "v", "host")
LIMIT = 2**20


class InterruptWire(Exception):
    """Validated wallet root request; emit only a start frame, never a completion."""


def check(value):
    if not value:
        raise ValueError("Synthetic turn not established")


def metadata(scope):
    # Read only these three non-secret correlation fields; claims never authorize a turn.
    keys=(b"x-codex-turn-metadata",b"thread-id",b"session-id")
    fields=[[v for k,v in scope.get("headers",()) if k==key] for key in keys]
    if not all(len(v)==1 and len(v[0])<=8192 for v in fields):
        return None,None
    claim=json.loads(fields[0][0])
    sid=fields[1][0].decode("ascii")
    if (fields[1]!=fields[2] or str(UUID(sid))!=sid or claim.get("thread_id")!=sid
            or claim.get("session_id")!=sid):
        return None,None
    return claim.get("thread_source"),sid


def markers(messages, known):
    found = []
    for msg in messages:
        role, content = msg.get("role"), msg.get("content")
        if role not in ("user", "assistant"):
            continue
        chunks = [content] if isinstance(content, str) else [
            part.get("text") for part in content or () if isinstance(part, dict)]
        for text in chunks:
            if isinstance(text, str) and text.strip() in known:
                found.append((role, text.strip()))
    return found


class Fixture:
    def __init__(self, host, interrupt=False):
        self.host = host
        self.prompts = ["ov-qa-input-" + token_hex(12) for _ in MODES]
        self.answers = ["ov-qa-answer-" + token_hex(12) for _ in MODES]
        self.rows, self.bad, self.armed, self.model = [], False, None, None
        self.rendered = self.switch_acks = self.status_checks = 0
        self.lock = threading.Lock()
        self.root, self.denied_titles, self.first_fault = None, 0, None
        self.denied_wallet = 0
        self.excess_titles = self.after_seal = self.extra_root = 0
        self.clean_exit = self.sealed = False
        self.interrupt = interrupt
        self.interrupted = threading.Event()
        self.drop_armed = self.native_reconnect = self.native_error = False
        self.reopens = self.repeat_v = self.repeat_host = self.unmatched = 0
        self.sent = {a: threading.Event() for a in self.answers}
        self.issue = ContextVar("wire-issue", default="wire")

    def arm(self, i):
        with self.lock:
            check(self.armed is None and len(self.rows) == i and not self.bad)
            self.armed = i

    def turn(self, mode, body, previous=(), root=None):
        with self.lock:
            i = len(self.rows)
            if self.drop_armed:
                # Count physical generation attempts, not socket opens or warmups.
                seen = markers(body.get("input") or [], set(self.prompts + self.answers))
                if root == self.root and ("user", self.prompts[1]) in [*previous, *seen]:
                    self.repeat_v += int(mode == "v")
                    self.repeat_host += int(mode == "host")
                else:
                    self.unmatched += 1
                self.issue.set("post-interrupt-ingress")
                raise ValueError("No replay authorization")
            if self.sealed:
                self.issue.set("extra-root")
                self.extra_root+=1
                raise ValueError("Sealed foreground series")
            self.issue.set("unarmed" if self.armed != i else "payer")
            check(i < len(MODES) and self.armed == i and mode == MODES[i] and not self.bad)
            self.issue.set("model-or-shape")
            model = body.get("model")
            check(isinstance(model, str) and 0 < len(model) < 160 and self.model in (None, model))
            messages = body.get("messages" if self.host == "claude" else "input")
            check(isinstance(messages, list))
            observed = [*previous, *markers(messages, set(self.prompts + self.answers))]
            expected = []
            for j in range(i + 1):
                expected.append(("user", self.prompts[j]))
                if j < i:
                    expected.append(("assistant", self.answers[j]))
            self.issue.set("history")
            check(observed == expected)
            if self.host == "codex":
                self.issue.set("different-root")
                check(root is not None and self.root in (None,root))
                self.root = root
            self.model, self.armed = model, None
            if self.interrupt and i == 1:
                self.drop_armed = True
                raise InterruptWire()
            self.rows.append(mode)
            self.sealed = len(self.rows) == len(MODES)
            self.issue.set("none")
            return self.answers[i], [*expected, ("assistant", self.answers[i])]

    @staticmethod
    def routes(path, leaf):
        return {"/_host/v1/" + leaf: "host", "/integrations/native/v1/" + leaf: "v"}.get(path)

    async def __call__(self, scope, receive, send):
        # The production rehearsal adapter already restricts the identity. This
        # fixture never reads Authorization, records headers, or forwards traffic.
        try:
            root = None
            if self.host == "codex":
                source,root=metadata(scope)
                mode=self.routes(scope["path"],"responses")
                if source=="thread_title":
                    # Stock PE has no independent helper witness. Never synthesize
                    # a title, forward it, or credit it as a customer/root answer.
                    self.issue.set("helper-route")
                    check(mode in ("host","v"))
                    self.issue.set("helper-root")
                    check(self.root and root!=self.root)
                    self.issue.set("helper-limit")
                    if self.sealed:
                        self.after_seal+=1
                    if mode=="v":
                        self.denied_wallet+=1
                    if self.denied_titles>=12:
                        self.excess_titles+=1
                    check(self.denied_titles<12)
                    self.denied_titles+=1
                    # A wallet helper is a permanent final gate failure. Refuse it
                    # immediately; never let it consume the separate root nonce.
                    if scope["type"]=="websocket":
                        check((await receive())["type"]=="websocket.connect")
                        await send({"type":"websocket.close","code":1008})
                    else:
                        await send({"type":"http.response.start","status":403,"headers":[]})
                        await send({"type":"http.response.body","body":b"{}"})
                    self.issue.set("none")
                    return
                self.issue.set("root-metadata")
                check(source=="user" and root)
                self.issue.set("wire")
            if scope["type"] == "websocket":
                return await self.ws(scope, receive, send, root)
            check(scope["type"] == "http" and scope["method"] == "POST")
            raw = bytearray()
            while True:
                event = await asyncio.wait_for(receive(), 8)
                check(event["type"] == "http.request")
                raw.extend(event["body"])
                check(len(raw) <= LIMIT)
                if not event.get("more_body"):
                    break
            if raw[:2] == b"\x1f\x8b":
                dec = zlib.decompressobj(31)
                raw = dec.decompress(raw, LIMIT + 1)
                check(len(raw) <= LIMIT and dec.eof and not dec.unused_data)
            body = json.loads(raw)
            path = scope["path"]
            answer = None
            if self.host == "claude" and self.routes(path, "messages/count_tokens"):
                mime, data = b"application/json", b'{"input_tokens":12}'
            else:
                mode = self.routes(path, "responses" if self.host == "codex" else "messages")
                answer, _ = self.turn(mode, body, root=root)
                mime = b"text/event-stream"
                data = b"".join(("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n").encode()
                                for e in frames(self.host, body["model"], answer, "resp_" + token_hex(12)))
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", mime)]})
            await send({"type": "http.response.body", "body": data})
            if answer:
                self.sent[answer].set()
        except InterruptWire:
            check(scope["type"] == "http")
            created = next(frames("codex", self.model, None, "resp_" + token_hex(12)))
            await send({"type":"http.response.start","status":200,"headers":[(b"content-type",b"text/event-stream")]})
            await send({"type":"http.response.body","body":("event: response.created\ndata: " +
                        json.dumps(created) + "\n\n").encode()})
            self.interrupted.set()
        except Exception:
            self.first_fault=self.first_fault or self.issue.get()

            self.bad = True
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": 400, "headers": []})
                await send({"type": "http.response.body", "body": b"{}"})

    async def ws(self, scope, receive, send, root):
        mode = self.routes(scope["path"], "responses")
        check(self.host == "codex" and mode and (await receive())["type"] == "websocket.connect")
        if self.drop_armed and self.root == root:
            self.reopens += 1
        await send({"type": "websocket.accept"})
        history = {}
        while True:
            event = await receive()
            if event["type"] == "websocket.disconnect":
                return
            raw = event.get("text") or event.get("bytes") or b""
            check(0 < len(raw) <= LIMIT)
            body = json.loads(raw)
            check(body.get("type") == "response.create" and len(history) < 12)
            rid = "resp_" + token_hex(12)
            if body.get("generate") is False:
                answer, history[rid] = None, []
            else:
                prev = body.get("previous_response_id")
                self.issue.set("foreign-previous")
                prior = history[prev] if prev else []
                try:
                    answer, history[rid] = self.turn(mode, body, prior, root)
                except InterruptWire:
                    created = next(frames("codex", self.model, None, rid))
                    await send({"type": "websocket.send", "text": json.dumps(created)})
                    await send({"type": "websocket.close", "code": 1011})
                    self.interrupted.set()
                    return
            for e in frames("codex", body["model"], answer, rid):
                await send({"type": "websocket.send", "text": json.dumps(e)})
            if answer:
                self.sent[answer].set()


def frames(host, model, answer, rid):
    # Responses and Messages framing follow the existing public Windows fixtures.
    if host == "codex":
        item = {"id": "msg_" + rid[5:], "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": answer, "annotations": []}]}
        response = {"object": "response", "id": rid, "model": model, "status": "completed",
                    "output": [item] if answer else [], "usage": {"input_tokens": 10, "output_tokens": int(bool(answer)),
                                                                 "total_tokens": 10 + int(bool(answer))}}
        yield {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}}
        if answer:
            yield {"type": "response.output_item.added", "output_index": 0,
                   "item": {**item, "status": "in_progress", "content": []}}
            yield {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
                   "content_index": 0, "delta": answer}
            yield {"type": "response.output_item.done", "output_index": 0, "item": item}
        yield {"type": "response.completed", "response": response}
        return
    usage = {"input_tokens": 12, "output_tokens": 1}
    for kind, value in [
        ("message_start", {"message": {"id": "msg_" + rid[5:], "type": "message", "role": "assistant",
                                       "model": model, "content": [], "stop_reason": None, "usage": usage}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": answer}}),
        ("content_block_stop", {"index": 0}),
        ("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": usage}),
        ("message_stop", {}),
    ]:
        yield {"type": kind, **value}


def interact(host, term, fixture, control, checkpoint):
    from scripts import verify_windows_conpty as pty

    checkpoint("rehearsal-banner")
    locator = term.expect(r"--windows-session\s+[0-9a-f]{32}", "rehearsal locator", timeout=65)
    sid = locator.split()[-1]
    check(re.fullmatch("[0-9a-f]{32}", sid))
    if host == "codex":
        from scripts.windows_fixture_onboard import run as onboard
        onboard(term, checkpoint)

    checkpoint("rehearsal-composer")
    # Stop before sending ANY input at a trust, auth, or first-run UI.
    term.expect(r"OpenAI\s+Codex|Claude\s+Code|Sonnet\s+4\.6|\? for shortcuts|Type your message|shortcuts for help", "native composer",
                timeout=85, diagnose="claude-start")
    for i, mode in enumerate(MODES):
        checkpoint("rehearsal-control-" + str(i))
        if i:
            control(sid, mode, mode)
            fixture.switch_acks += 1
        control(sid, "status", mode)
        fixture.status_checks += 1
        check(not pty.screen_failure("claude-start", term.frame()))
        checkpoint("rehearsal-answer-" + str(i))
        from scripts.windows_codex_editor import enter
        enter(term, fixture.prompts[i], checkpoint, "turn-" + str(i), host=host,
              before_submit=lambda: fixture.arm(i))
        if fixture.interrupt and i == 1:
            checkpoint("wallet-wire-interrupt")
            check(fixture.interrupted.wait(30))
            checkpoint("native-retry-outcome")
            deadline = time.monotonic() + 85
            while time.monotonic() < deadline:
                term.read()
                frame = term.frame()
                check(fixture.answers[1] not in frame)
                fixture.native_reconnect |= bool(re.search(r"reconnecting\W+\d+\s*/\s*\d+", frame, re.I))
                fixture.native_error |= bool(re.search(
                    r"stream disconnected before completion|exceeded retry limit|"
                    r"unexpected status (?:400|403|409|502)|stream closed before response.completed", frame, re.I))
                if fixture.native_error or fixture.repeat_v or fixture.repeat_host or fixture.unmatched:
                    break
                time.sleep(.04)
            check(fixture.native_error and (fixture.reopens or fixture.native_reconnect))
            check(not (fixture.bad or fixture.repeat_v or fixture.repeat_host or fixture.unmatched))
            control(sid, "status", "v")
            fixture.status_checks += 1
            break
        term.expect(re.escape(fixture.answers[i]), "rendered synthetic answer", timeout=40, diagnose="claude-start")
        fixture.rendered += 1
        # User-visible text can precede the supplier's completed frame. For
        # shutdown wait for actual delivery, not an unconditional extra second.
        if i == len(MODES)-1:
            checkpoint("sealed-final-frame")
            check(fixture.sealed and fixture.sent[fixture.answers[i]].wait(5))
        else:
            time.sleep(1)
    checkpoint("rehearsal-exit")
    enter(term, "/exit", checkpoint, "exit", host=host)
    checkpoint("owned-process-exit")
    term.require_success()
    fixture.clean_exit=True
    checkpoint("foreground-exited-zero")
    expected = ["host"] if fixture.interrupt else list(MODES)
    check(fixture.rows == expected and fixture.sealed != fixture.interrupt and fixture.armed is None)
    checkpoint("rehearsal-helper-policy")
    check(not fixture.denied_wallet and not fixture.bad and not fixture.extra_root)


def payer(node, app, env, cwd, sid, action, expected):
    check(action in ("host", "v", "status") and re.fullmatch("[0-9a-f]{32}", sid))
    result = subprocess.run([node, app, "integration", "terminal", "--windows-session", sid, "--payer", action],
                            env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, shell=False, timeout=25)
    check(result.returncode == 0 and result.stdout.strip() == ("Synthetic Windows payer: " + expected).encode())


async def run(host, drive, checkpoint, stats, *, interrupt=False):
    import uvicorn

    fixture = Fixture(host, interrupt)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        server = uvicorn.Server(uvicorn.Config(fixture, lifespan="off", access_log=False, log_config=None,
                                                log_level="critical", ws_max_size=LIMIT, timeout_keep_alive=5))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done():
                        await serving
                        raise ValueError("Loopback fixture failed")
                    await asyncio.sleep(.02)
            await asyncio.to_thread(drive, host, checkpoint, fixture, sock.getsockname()[1])
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 10)

            if host=="codex":
                print(f"::notice::unnamed lane={'interrupt' if interrupt else 'roundtrip'} "
                      f"rendered={fixture.rendered} links={max(0,len(fixture.rows)-1)} "
                      f"switches={fixture.switch_acks} statuses={fixture.status_checks} "
                      f"drop={int(fixture.interrupted.is_set())} reopens={fixture.reopens} "
                      f"retry_ui={int(fixture.native_reconnect)} terminal_error={int(fixture.native_error)} "
                      f"repeat_v={fixture.repeat_v} repeat_host={fixture.repeat_host} unmatched={fixture.unmatched} "
                      f"wallet_completed={int('v' in fixture.rows)} exit_zero={int(fixture.clean_exit)}",flush=True)
                print(f"::notice::cleanup codex exit_zero={int(fixture.clean_exit)} sealed={int(fixture.sealed)} "
                      f"final_frame={int(fixture.sent[fixture.answers[-1]].is_set())} root_answers={len(fixture.rows)} "
                      f"extra_root={fixture.extra_root} title_over_cap={fixture.excess_titles} "
                      f"title_after_seal={fixture.after_seal}",flush=True)
                reason=fixture.first_fault or ("wallet-helper" if fixture.denied_wallet else "none")
                print(f"::notice::wire codex denied_titles={fixture.denied_titles} denied_wallet={fixture.denied_wallet} root={int(fixture.root is not None)} fault={reason}",flush=True)
            stats.update(supplier_answers=len(fixture.rows), linked=max(0, len(fixture.rows)-1),
                         rendered=fixture.rendered, switch_acks=fixture.switch_acks,
                         status_checks=fixture.status_checks, rejected=int(fixture.bad or bool(fixture.denied_wallet)))
        if host == "codex":
            check(fixture.clean_exit and fixture.sealed != interrupt and not fixture.extra_root
                  and not fixture.denied_wallet and not fixture.bad)
