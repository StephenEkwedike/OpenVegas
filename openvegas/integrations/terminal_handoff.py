"""Authenticated game-form connection to an explicitly managed terminal relay."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import socket
import stat
import sys
from contextlib import suppress
from pathlib import Path

from .credentials import IntegrationError

SOCKET_ENV = "OPENVEGAS_TERMINAL_SOCKET"
TOKEN_ENV = "OPENVEGAS_TERMINAL_TOKEN"
MAX_PACKET = 131072


class TerminalDisconnected(IntegrationError):
    def __init__(self):
        super().__init__("The terminal game display disconnected. Back to coding. "
                         "Existing round records are retained; no automatic move, retry, refund or new wager. "
                         "This is a terminal display failure, not a login failure.")


async def close_writer(writer):
    writer.close()
    # A broken socket must not replace the actual game result/error during cleanup.
    with suppress(OSError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), 1)


def configured():
    return bool(os.getenv(SOCKET_ENV) and os.getenv(TOKEN_ENV))


def setup_required():
    """Fail before account access or wagering, rather than silently losing visuals."""
    from .credentials import profile

    command = ['ov', 'integration', 'terminal', '--attach']
    root = Path(__file__).resolve().parents[2]
    if getattr(sys, 'frozen', False):
        command[0] = sys.executable
    elif profile().resolve().is_relative_to(root / '.local') and (root / 'dev/openvegas').is_file():
        command[0] = str(root / 'dev/openvegas')
    return {'available': False, 'code': 'terminal_setup_required', 'surface': 'none',
            'setup_command': shlex.join(command),
            'message': 'The animated game table is not connected to this terminal. '
                       'An older terminal profile or a shared Codex daemon may be missing its connection. '
                       'Current OpenVegas Graphics profiles connect games automatically. '
                       'If already attached, update/restart that managed shell rather than repeatedly attaching. '
                       'Exit Codex/Claude to the shell in this SAME terminal, run the setup_command, '
                       'then restart the host here. No new terminal or editor tab is needed. '
                       'No game form, wager or automatic text-only fallback was started.'}


def validate_socket(path):
    """All relay clients require the same private, current-user capability path."""
    info, parent = os.lstat(path), os.lstat(os.path.dirname(path))
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        raise IntegrationError("Managed terminal connection is not private. No game opened.")


async def connect(*, kind='game'):
    path = os.environ[SOCKET_ENV]
    validate_socket(path)
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path, limit=MAX_PACKET), 3)
    writer.write(json.dumps({"token": os.environ[TOKEN_ENV], "kind": kind}).encode() + b"\n")
    await writer.drain()
    try:
        reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
        if reply.get("ready") is not True:
            raise IntegrationError(reply.get("message", "Managed terminal is unavailable."))
    except BaseException:
        await close_writer(writer)
        raise
    return reader, writer


async def check():
    """A no-display, no-wallet authenticated check for this shell's relay."""
    if not configured():
        return setup_required()
    try:
        _, writer = await connect(kind='status')
    except (IntegrationError, OSError, TimeoutError, ValueError):
        return {**setup_required(), 'code': 'terminal_disconnected'}
    writer.close()
    await writer.wait_closed()
    return {'available': True, 'surface': 'same_terminal',
            'message': 'Same-terminal game handoff connected. Run ordinary codex or claude here, then OpenVegas play.'}


class TerminalView:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.sequence = 0
        self.closed = False

    async def send(self, **packet):
        data = json.dumps(packet, allow_nan=False).encode() + b"\n"
        if len(data) > MAX_PACKET:
            raise IntegrationError("Game display exceeded its safe size. Recovery retained.")
        try:
            self.writer.write(data)
            await asyncio.wait_for(self.writer.drain(), 2)
        except (OSError, TimeoutError):
            raise TerminalDisconnected() from None

    async def ask(self, message, schema):
        self.sequence += 1
        await self.send(kind="form", id=self.sequence, message=message,
                        name=schema.__name__, schema=schema.model_json_schema())
        try:
            raw = await self.reader.readline()
            if not raw:
                self.closed = True
                raise TerminalDisconnected()
            reply = json.loads(raw)
        except (OSError, ValueError):
            raise TerminalDisconnected() from None
        if not isinstance(reply, dict):
            raise TerminalDisconnected()
        if reply.get("closed") is True:
            self.closed = True
            raise TerminalDisconnected()
        if reply.get("id") != self.sequence:
            raise IntegrationError("Stale game response rejected. No move submitted.")
        return schema.model_validate(reply["answer"]) if reply.get("answer") is not None else None

    async def status(self, message):
        await self.send(kind="status", message=message)

    async def present(self, game, result):
        # Already-public server state only; never journal, idempotency keys or auth.
        public = {key: result[key] for key in ("state", "outcome", "current_state", "payout_v", "net_v") if key in result}
        await self.send(kind="table", game=game, result=public)


async def play(ctx, broker, *, requested_game=None):
    from .native_play import play_in_host
    try:
        reader, writer = await connect()
    except (TimeoutError, OSError, ValueError):
        return {"available": False, "message": "Managed game connection is disconnected. Exit the coding host, run "
                "ov integration terminal --attach in this SAME terminal, then restart the host. "
                "Do not open another terminal. No game request was sent; no automatic fallback or wager."}
    view = TerminalView(reader, writer)
    try:
        try:
            if requested_game:
                await view.present(requested_game, {})
            result = await play_in_host(ctx, broker, ask_override=view.ask,
                progress_override=view.status, present_override=view.present, requested_game=requested_game)
        except IntegrationError as exc:
            result = {"available": False, "message": str(exc)}
            if isinstance(exc, TerminalDisconnected):
                result["code"] = "terminal_display_disconnected"
        if not view.closed:
            try:
                await view.send(kind="done")
                # Wait for restoration before the host prints; never lose a
                # durable settlement or the original error if the view crashed.
                reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
                if not isinstance(reply, dict) or reply.get("closed") is not True:
                    raise TerminalDisconnected()
            except (IntegrationError, OSError, TimeoutError, ValueError):
                result = {**result, "display_warning": "Terminal display closed without a restoration acknowledgement. "
                          "The game result above is unchanged; no automatic retry."}
        return result
    finally:
        await close_writer(writer)


def remote_screen(connection):
    """Build the relay's game view; no credentials or API client here."""
    from typing import ClassVar, Literal

    from pydantic import Field, create_model
    from textual.widgets import Static

    from openvegas.tui.game_screen import GameScreen

    from .native_play import HostForm

    class RemoteScreen(GameScreen):
        arena_view = None
        BINDINGS: ClassVar = [("escape", "leave", "Back to coding"), ("ctrl+c", "leave", "Quit"),
                    ("q", "leave", "Quit"), ("m", "motion", "Motion on/off")]

        async def ask(self, message, schema):
            if schema.__name__ == 'RoundResult':
                message = 'Result received. Done / Esc returns to coding; no repeat wager.'
            task = asyncio.create_task(super().ask(message, schema))
            await asyncio.sleep(0)
            if schema.__name__ == 'ArenaCommand':
                from textual.widgets import Input
                self.query_one('#choices').display = False
                entry = self.query_one('#amount', Input)
                entry.display = True
                entry.disabled = False
                entry.placeholder = 'check / call / raise N / fold / quit'
                entry.value = ''
                entry.focus()
            self.query_one("#status", Static).update("" if schema.__name__ == "Wager" else
                "Test chips only. Enter a move on your turn; Esc returns to coding." if schema.__name__ == 'ArenaCommand' else
                "Your turn. Quit ends the game; unfinished stake forfeited. Return to coding in this same terminal.")
            self.update_feedback()
            try:
                return await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        def on_input_submitted(self, event):
            if self.schema is not None and self.schema.__name__ == 'ArenaCommand':
                self.answer(event.value)
            else:
                super().on_input_submitted(event)

        def draw(self, moving=None):
            if self.arena_view is not None:
                from openvegas.arena.native import table
                width = max(24, self.query_one('#canvas').size.width - 2)
                self.query_one('#table', Static).update(table(self.arena_view, width))
                return
            super().draw(moving)

        async def run_game(self):
            reader, writer = await asyncio.open_connection(sock=connection, limit=MAX_PACKET)
            self.query_one("#heading", Static).update("OPENVEGAS / GAME BREAK")

            async def display(packet):
                if packet.get("kind") == "status":
                    await self.progress(packet["message"])
                elif packet.get("kind") == "table":
                    self.present_table(packet['game'], packet['result'])
                elif packet.get('kind') == 'arena_table':
                    from openvegas.arena.native import instructions
                    self.arena_view = packet['observation']
                    self.query_one('#heading', Static).update('OPENVEGAS ARENA / TEST CHIPS / NO $V')
                    self.query_one('#prompt', Static).update(instructions(self.arena_view))
                    self.draw()

            try:
                while raw := await reader.readline():
                    packet = json.loads(raw)
                    kind = packet.get("kind")
                    if kind == "done":
                        break
                    if kind in {"status", "table", "arena_table"}:
                        await display(packet)
                    elif kind == "form":
                        fields = {}
                        for name, spec in packet["schema"]["properties"].items():
                            values = spec.get("enum", [spec["const"]] if "const" in spec else None)
                            datatype = Literal[tuple(values)] if values else bool if spec["type"] == "boolean" else str
                            fields[name] = (datatype, Field(description=spec.get("description", "")))
                        schema = create_model(packet["name"], __base__=HostForm, **fields)
                        answering = asyncio.create_task(self.ask(packet["message"], schema))
                        incoming = asyncio.create_task(reader.readline())
                        try:
                            while True:
                                done, _ = await asyncio.wait([answering, incoming], return_when=asyncio.FIRST_COMPLETED)
                                if incoming in done:
                                    raw = incoming.result()
                                    if not raw:
                                        return
                                    update = json.loads(raw)
                                    if update.get("kind") == "done":
                                        return
                                    if update.get("kind") not in {"status", "table", "arena_table"}:
                                        raise IntegrationError("Unexpected game display packet.")
                                    # Progress is not a socket disconnect and must
                                    # not dismiss or answer the user's open form.
                                    await display(update)
                                    incoming = asyncio.create_task(reader.readline())
                                if answering in done:
                                    break
                            answer = answering.result()
                        finally:
                            for task in (answering, incoming):
                                if not task.done():
                                    task.cancel()
                            await asyncio.gather(answering, incoming, return_exceptions=True)
                        writer.write(json.dumps({"id": packet["id"], "answer": answer.model_dump() if answer else None}).encode() + b"\n")
                        await writer.drain()
            finally:
                self.exit()
                # Parent keeps its duplicate socket until the screen is restored.
                await close_writer(writer)

        def action_leave(self):
            if self.waiting and not self.waiting.done():
                if self.schema.__name__ in {"RoundAction", "Number"}:
                    self.answer("quit")
                else:
                    self.waiting.set_result(None)
            # During a server mutation, don't send a new action or kill recovery.
            # Network timeouts are bounded by the shared round runner.

    return RemoteScreen()


def render(connection):
    # Mouse capture belongs only to this owned game screen. The relay restores
    # the host's original modes when the screen closes.
    remote_screen(connection).run(mouse=True)


def renderer_command(fd):
    """Load one coherent source version, never the relay's cached Python modules."""
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--_render-terminal-game', str(fd)]
    root = str(Path(__file__).resolve().parents[2])
    return [sys.executable, '-I', '-c',
            'import sys; sys.path.insert(0, ' + repr(root) + '); '
            'from openvegas.integrations.terminal_handoff import render_main; '
            'render_main(int(sys.argv[1]))', str(fd)]


def exec_renderer(connection):
    fd = connection.fileno()
    os.set_inheritable(fd, True)
    # The already-authenticated socket is sufficient to render public state.
    # Do not let the renderer open another privileged relay connection.
    env = {key: value for key, value in os.environ.items() if key not in {SOCKET_ENV, TOKEN_ENV}}
    os.execve(sys.executable, renderer_command(fd), env)


def render_main(fd):
    if fd < 3 or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError('Game renderer requires its owned terminal and socket.')
    os.set_inheritable(fd, False)
    with socket.socket(fileno=fd) as connection:
        if connection.family != socket.AF_UNIX or connection.type != socket.SOCK_STREAM:
            raise RuntimeError('Game renderer requires a local stream socket.')
        connection.getpeername()
        render(connection)
