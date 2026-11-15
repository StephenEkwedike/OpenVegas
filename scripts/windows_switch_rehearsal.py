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

MODES = ("host", "v", "host")
LIMIT = 2**20


def check(value):
    if not value:
        raise ValueError("Synthetic turn not established")


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
    def __init__(self, host):
        self.host = host
        self.prompts = ["ov-qa-input-" + token_hex(12) for _ in MODES]
        self.answers = ["ov-qa-answer-" + token_hex(12) for _ in MODES]
        self.rows, self.bad, self.armed, self.model = [], False, None, None
        self.rendered = self.switch_acks = self.status_checks = 0
        self.lock = threading.Lock()

    def arm(self, i):
        with self.lock:
            check(self.armed is None and len(self.rows) == i and not self.bad)
            self.armed = i

    def turn(self, mode, body, previous=()):
        with self.lock:
            i = len(self.rows)
            check(i < len(MODES) and self.armed == i and mode == MODES[i] and not self.bad)
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
            check(observed == expected)
            self.model, self.armed = model, None
            self.rows.append(mode)
            return self.answers[i], [*expected, ("assistant", self.answers[i])]

    @staticmethod
    def routes(path, leaf):
        return {"/_host/v1/" + leaf: "host", "/integrations/native/v1/" + leaf: "v"}.get(path)

    async def __call__(self, scope, receive, send):
        # The production rehearsal adapter already restricts the identity. This
        # fixture never reads Authorization, records headers, or forwards traffic.
        try:
            if scope["type"] == "websocket":
                return await self.ws(scope, receive, send)
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
            if self.host == "claude" and self.routes(path, "messages/count_tokens"):
                mime, data = b"application/json", b'{"input_tokens":12}'
            else:
                mode = self.routes(path, "responses" if self.host == "codex" else "messages")
                answer, _ = self.turn(mode, body)
                mime = b"text/event-stream"
                data = b"".join(("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n").encode()
                                for e in frames(self.host, body["model"], answer, "resp_" + token_hex(12)))
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", mime)]})
            await send({"type": "http.response.body", "body": data})
        except Exception:
            self.bad = True
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": 400, "headers": []})
                await send({"type": "http.response.body", "body": b"{}"})

    async def ws(self, scope, receive, send):
        mode = self.routes(scope["path"], "responses")
        check(self.host == "codex" and mode and (await receive())["type"] == "websocket.connect")
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
                prior = history[prev] if prev else []
                answer, history[rid] = self.turn(mode, body, prior)
            for e in frames("codex", body["model"], answer, rid):
                await send({"type": "websocket.send", "text": json.dumps(e)})


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
    checkpoint("rehearsal-composer")
    # Stop before sending ANY input at a trust, auth, or first-run UI.
    term.expect(r"\? for shortcuts|Type your message|shortcuts for help", "native composer",
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
        fixture.arm(i)
        from scripts.windows_codex_editor import enter
        check(host == "codex")
        enter(term, fixture.prompts[i], checkpoint, "turn-" + str(i))
        term.expect(re.escape(fixture.answers[i]), "rendered synthetic answer", timeout=40, diagnose="claude-start")
        fixture.rendered += 1
        time.sleep(1)
    checkpoint("rehearsal-exit")
    term.type_line("/exit")
    term.require_success()
    check(fixture.rows == list(MODES) and not fixture.bad and fixture.armed is None)


def payer(node, app, env, cwd, sid, action, expected):
    check(action in ("host", "v", "status") and re.fullmatch("[0-9a-f]{32}", sid))
    result = subprocess.run([node, app, "integration", "terminal", "--windows-session", sid, "--payer", action],
                            env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, shell=False, timeout=25)
    check(result.returncode == 0 and result.stdout.strip() == ("Synthetic Windows payer: " + expected).encode())


async def run(host, drive, checkpoint, stats):
    import uvicorn

    fixture = Fixture(host)
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
            stats.update(supplier_answers=len(fixture.rows), linked=max(0, len(fixture.rows)-1),
                         rendered=fixture.rendered, switch_acks=fixture.switch_acks,
                         status_checks=fixture.status_checks, rejected=int(fixture.bad))
            server.should_exit = True
            await asyncio.wait_for(serving, 10)
