"""Explicit offline public-art preview, never a customer ownership grant.

Run from the repository root with:
  .venv/bin/python tests/test_emotes/compositor_demo.py --seconds 90

No backend, credentials, microphone, transcript file, or AI calls. Commands:
/run /fail /cancel /approve /voice /history /size /exit. During /run, type a draft,
scroll/select old output, and press Ctrl+C to cancel. /voice inserts a FIXED
fixture, not captured audio. A watchdog exits after --seconds (maximum 180).
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def deny_network(*args, **kwargs):
    raise RuntimeError("Offline compositor demo prohibits network")


async def demo(seconds):
    from prompt_toolkit import PromptSession
    from rich.console import Console
    from rich.prompt import Confirm

    from openvegas.emotes.bridge import ChatEmoteBridge
    from openvegas.emotes.compositor import OwnedChatCompositor, TurnCancelled
    from openvegas.emotes.resources import PackRepository

    console = Console()
    repo = PackRepository()
    # Explicit harness-only public bundled preview. Production chat uses
    # create_owned_chat and server-authorized RemoteLibrary, never this guard.
    owner = OwnedChatCompositor(
        PromptSession(), console, session_id="offline-compositor-demo",
        pack=repo.load("pixel-courier"), completion_pack=repo.load("skyline-dunk"),
        access_guard=lambda: True, replay_on_close=True,
    )
    bridge = ChatEmoteBridge("offline-compositor-demo", publish=owner.publish)

    async def work(command, token):
        for i in range(30):
            await asyncio.sleep(0.4)
            console.print(f"Offline work {i + 1}/30. Draft and history remain usable.")
        bridge.finish(success=command != "/fail", turn=token)
        console.print("Offline fixture failed." if command == "/fail" else "Offline fixture finished.")

    try:
        async with asyncio.timeout(seconds), owner:
            console.print("OFFLINE PUBLIC-ART PREVIEW. No purchases, AI, backend or real voice.")
            console.print("/run /fail /cancel /approve /voice /history /size /exit | Ctrl+C cancels work")
            while True:
                command = await owner.prompt_async("demo: ")
                console.print(f"[on #333333]> {command}[/]")
                if command == "/exit":
                    break
                if command == "/size":
                    size = owner.app.output.get_size()
                    console.print(f"Viewport {size.columns}x{size.rows}; {owner.dock_mode} dock, {owner.dock_height} rows (cap 16, at most one-third).")
                elif command == "/history":
                    for i in range(100):
                        console.print(f"Selectable historical row {i:03d}")
                elif command == "/voice":
                    owner.insert_voice("fixture dictation, not microphone audio")
                elif command == "/approve":
                    result = await owner.run_external(lambda: Confirm.ask("Offline approval fixture?", console=console))
                    console.print(f"Approval result: {result}")
                elif command in {"/run", "/fail", "/cancel"}:
                    token = bridge.begin()
                    if command == "/cancel":
                        bridge.cancel(turn=token)
                        continue
                    try:
                        async with bridge.supervise(turn=token):
                            await owner.run_turn(work(command, token), on_cancel=lambda token=token: bridge.cancel(turn=token))
                    except TurnCancelled:
                        console.print("Cancelled, no celebration. Draft retained.")
                else:
                    console.print("Fixture only; use /run to exercise animation and typing.")
    except (TimeoutError, EOFError, KeyboardInterrupt):
        pass
    finally:
        bridge.close()
        await owner.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=90, choices=range(1, 181), metavar="1-180")
    args = parser.parse_args()
    for name in ("create_connection", "getaddrinfo", "gethostbyname"):
        setattr(socket, name, deny_network)
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            setattr(socket.socket, name, deny_network)
    asyncio.run(demo(args.seconds))
