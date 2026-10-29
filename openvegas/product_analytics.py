"""Explicitly opted-in command lifecycle counts; never terminal recording."""
from __future__ import annotations

import asyncio
import os
import queue
import threading
from urllib.parse import urlsplit
from uuid import uuid4

import click
import httpx

from openvegas.config import load_config, save_config_atomic

CONSENT_KEY = "product_analytics"
SEND_TIMEOUT_SECONDS = 0.6
FEATURES = {"claude": "claude", "codex": "codex", "chat": "chat", "ui": "games",
            "play": "games", "emote": "emotes", "tv": "tv", "integration": "integration", "go": "integration"}


def enabled(config: dict) -> bool:
    consent = config.get(CONSENT_KEY)
    return (os.getenv("DO_NOT_TRACK") != "1" and isinstance(consent, dict)
            and consent.get("enabled") is True and consent.get("version") == 1)


def endpoint(config: dict) -> str | None:
    value = str(config.get("backend_url", "")).rstrip("/")
    try:
        url = urlsplit(value)
        if url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
            return None
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}):
            return None
        if not url.hostname:
            return None
        return value + "/analytics/activity"
    except ValueError:
        return None


async def _send(url: str, payload: dict) -> None:
    # A disposable client deliberately has no session/auth or default config headers.
    async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS, trust_env=False,
                                     follow_redirects=False) as client:
            # Only delivery is attempted; never buffer or drain an ignored body.
            async with client.stream("POST", url, json=payload):
                pass


def send(url: str, payload: dict) -> None:
    asyncio.run(_send(url, payload))


class ActivitySession:
    def __init__(self, feature: str):
        self.feature = feature
        self.session_id = str(uuid4())
        self.url = None
        self.jobs: queue.Queue = queue.Queue(maxsize=3)
        self.worker = None
        try:
            config = load_config()
            if enabled(config) and feature in set(FEATURES.values()):
                self.url = endpoint(config)
        except (OSError, ValueError, TypeError, AttributeError):
            self.url = None

    def _consume(self):
        while True:
            payload = self.jobs.get()
            if payload is None:
                return
            try:
                # Consent withdrawal while a long-running command is active stops future sends.
                if enabled(load_config()):
                    send(self.url, payload)
            except (httpx.HTTPError, OSError, ValueError, TypeError, AttributeError):
                continue  # No retries or logging of private request/config values.

    def emit(self, finished=False, outcome="none"):
        if not self.url:
            return
        payload = {"event": "cli_command_finished" if finished else "cli_command_started",
                   "surface": "cli", "feature": self.feature,
                   "action": "finish" if finished else "start", "outcome": outcome,
                   "session_id": self.session_id, "consent": True}
        try:
            self.jobs.put_nowait(payload)
        except queue.Full:
            return
        if self.worker is None:
            self.worker = threading.Thread(target=self._consume, name="ov-activity", daemon=True)
            try:
                self.worker.start()
            except RuntimeError:
                self.worker = None
                self.url = None

    def close(self, outcome):
        self.emit(finished=True, outcome=outcome)
        if self.worker:
            self.jobs.put_nowait(None)
            # Delivery is best-effort; do not delay exit for an unavailable service.
            self.worker.join(timeout=1.3)


class ActivityGroup(click.Group):
    def invoke(self, ctx):
        args = list(getattr(ctx, "_protected_args", ())) + list(ctx.args)
        command = args[0] if args else ""
        feature = FEATURES.get(command)
        if command == "native" and len(args) > 1 and args[1] in {"claude", "codex"}:
            feature = args[1]
        if feature is None:
            return super().invoke(ctx)
        session = ActivitySession(feature)
        session.emit()
        outcome = "error"
        try:
            result = super().invoke(ctx)
            outcome = "error" if isinstance(result, int) and result != 0 else "ok"
            return result
        except (KeyboardInterrupt, click.Abort):
            outcome = "cancelled"
            raise
        except (SystemExit, click.exceptions.Exit) as exc:
            code = getattr(exc, "exit_code", getattr(exc, "code", 1))
            outcome = "ok" if code == 0 else "cancelled" if code in {130, -2} else "error"
            raise
        finally:
            session.close(outcome)


@click.command("analytics")
@click.argument("setting", type=click.Choice(["on", "off", "status"]), default="status")
def analytics(setting):
    """Opt in/out of anonymous activity counts. Never records terminal contents."""
    config = load_config()
    if setting != "status":
        config[CONSENT_KEY] = {"version": 1, "enabled": setting == "on"}
        save_config_atomic(config)
    click.echo("Anonymous activity: " + ("on" if enabled(config) else "off"))
    click.echo("Only feature/start/finish/outcome counts; no prompts, code, paths, account IDs or terminal recording.")
    click.echo("Applies to OpenVegas commands, not independently launched Claude/Codex sessions. Disable with: ov analytics off")
