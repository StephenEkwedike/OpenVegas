"""Explicit opt-in local Arena CLI. Credentials stay in permission-restricted files."""

import argparse
import json
import os
from pathlib import Path
import secrets
import time
import uuid
from urllib.parse import urlsplit

import httpx
from rich.console import Console
from rich.markup import escape

from .service import Arena


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as f:
        json.dump(data, f)


def private_read(path):
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Capability files must be private (chmod 600), not symlinks")
    return json.loads(path.read_text())


def local_url(value):
    p = urlsplit(value)
    if (
        p.scheme != "http"
        or p.hostname != "127.0.0.1"
        or p.username
        or p.password
        or p.path
        or p.query
        or p.fragment
    ):
        raise ValueError("Only http://127.0.0.1:PORT is allowed in this pilot")
    return value


def call(base, path, token, body=None, seat=None):
    headers = {"Authorization": "Bearer " + token}
    if seat:
        headers["X-Arena-Seat"] = seat
    with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
        response = client.request(
            "GET" if body is None else "POST", local_url(base) + path, headers=headers, json=body
        )
    if response.status_code != 200:
        raise ValueError(
            "Arena request failed: " + str(response.status_code) + " " + response.text[:250]
        )
    return response.json()


def play(identity, bot=False):
    from openvegas.tui.game_screen import card_faces

    console = Console()
    previous = None
    while True:
        view = call(identity["url"], "/matches/" + identity["match_id"], identity["token"])
        marker = (view.get("version"), view.get("joined"))
        if marker != previous:
            previous = marker
            console.print("\n[bold cyan]OPENVEGAS ARENA[/] / non-redeemable test chips")
            if view.get("status") == "waiting":
                console.print(f"Waiting for players: {view['joined']}/{view['capacity']}")
            else:
                console.print(
                    f"Seat {view['seat']} / {view['street']} / pot {view['pot']} / stacks {view['stacks']}"
                )
                console.print("Your cards")
                console.print(card_faces(view["cards"], console.width))
                console.print("Shared cards")
                console.print(card_faces(view["board"], console.width))
        if view.get("resolved"):
            console.print(
                f"Hand finished. Payouts: {view['payouts']}. No $V or inference credits changed."
            )
            return
        options = view.get("legal_actions", {})
        if not options:
            time.sleep(0.5)
            continue
        if bot:
            action = "check" if "check" in options else "call"
            amount = None
        else:
            console.print(escape(str(options)))
            command = console.input(
                "Move (raise N = total this street); quit exits and timeout folds: "
            ).strip()
            if command == "quit":
                return
            fields = command.split()
            if not fields:
                continue
            action = fields[0]
            amount = None
            if action not in options:
                console.print("Choose one of the displayed moves, or quit.")
                continue
            if action == "raise":
                try:
                    amount = int(fields[1])
                except (IndexError, ValueError):
                    console.print("Enter raise followed by a whole-chip amount.")
                    continue
        result = call(
            identity["url"],
            "/matches/" + identity["match_id"] + "/actions",
            identity["token"],
            {
                "version": view["version"],
                "action": action,
                "amount": amount,
                "request_id": uuid.uuid4().hex,
            },
        )
        if not result["accepted"]:
            console.print(escape(result["reason"]))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--home", type=Path, default=Path(".local/arena"))
    sub = p.add_subparsers(dest="command", required=True)
    server = sub.add_parser("serve")
    server.add_argument("--port", type=int, default=8793)
    create = sub.add_parser("create")
    create.add_argument("--seats", type=int, default=2)
    create.add_argument("--pool", choices=["human", "agent"], default="human")
    create.add_argument("--url", default="http://127.0.0.1:8793")
    create.add_argument("--invitation", type=Path, required=True)
    join = sub.add_parser("join")
    join.add_argument("invitation", type=Path)
    join.add_argument("--identity", type=Path, required=True)
    resume = sub.add_parser("play")
    resume.add_argument("identity", type=Path)
    resume.add_argument("--bot", action="store_true")
    demo = sub.add_parser("demo")
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--pairs", type=int, default=20)
    verify = sub.add_parser("verify")
    verify.add_argument("directory", type=Path)
    rights = sub.add_parser("rights")
    rights.add_argument("identity", type=Path)
    rights.add_argument(
        "--purpose",
        choices=["none", "internal_evaluation", "training", "evaluation_delivery"],
        required=True,
    )
    rights.add_argument("--record-id")
    export = sub.add_parser("export")
    export.add_argument("--match", action="append", required=True)
    export.add_argument(
        "--purpose",
        choices=["internal_evaluation", "training", "evaluation_delivery"],
        required=True,
    )
    export.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    operator_path = args.home / "operator.json"
    if args.command == "serve":
        import uvicorn
        from .api import make_app

        if not operator_path.exists():
            private_write(operator_path, {"token": secrets.token_urlsafe(32)})
        uvicorn.run(
            make_app(Arena(args.home / "arena.sqlite"), private_read(operator_path)["token"]),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
        )
    elif args.command == "create":
        if args.invitation.exists():
            p.error("Invitation file exists; use a fresh path")
        result = call(
            args.url,
            "/matches",
            private_read(operator_path)["token"],
            {"seats": args.seats, "pool": args.pool},
        )
        private_write(args.invitation, {**result, "url": local_url(args.url)})
        print("Private invitation saved. No wager, $V transfer or inference call.")
    elif args.command == "join":
        invite = private_read(args.invitation)
        if not args.identity.exists():
            private_write(
                args.identity,
                {
                    "url": local_url(invite["url"]),
                    "match_id": invite["match_id"],
                    "token": secrets.token_urlsafe(32),
                    "pool": invite["pool"],
                },
            )
        identity = private_read(args.identity)
        if identity["match_id"] != invite["match_id"] or identity["url"] != invite["url"]:
            p.error("Identity belongs to another match")
        view = call(
            invite["url"],
            "/matches/" + invite["match_id"] + "/join",
            invite["invite"],
            {"source_type": invite["pool"], "configuration": {}},
            identity["token"],
        )
        print(
            f"Joined seat {view['seat']}. Local actions are recorded for replay; export consent is off. Use play with the same private identity file to reconnect."
        )
    elif args.command == "play":
        identity = private_read(args.identity)
        if args.bot and identity["pool"] != "agent":
            p.error("Bots cannot join the human pool")
        play(identity, args.bot)
    elif args.command == "rights":
        identity = private_read(args.identity)
        call(
            identity["url"],
            "/matches/" + identity["match_id"] + "/rights",
            identity["token"],
            {
                "purpose": None if args.purpose == "none" else args.purpose,
                "record_id": args.record_id,
            },
        )
        print("Local export choice recorded. This does not authorize a data sale or transfer.")
    elif args.command == "export":
        from .dataset import export_matches

        export_matches(Arena(args.home / "arena.sqlite"), args.match, args.output, args.purpose)
        print(
            "Local bundle written. No external delivery; private-replay must not enter model inputs."
        )
    elif args.command == "verify":
        from .dataset import verify_bundle

        print(json.dumps(verify_bundle(args.directory), indent=2))
    else:
        from .dataset import demo_bundle

        demo_bundle(args.output, args.pairs)
        print("Synthetic local evaluation bundle written; no model calls or real player data.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(
            "Arena closed. Unfinished test-chip turn deadlines continue; no $V changed."
        ) from None
    except (ValueError, httpx.HTTPError) as exc:
        raise SystemExit(str(exc)) from None
