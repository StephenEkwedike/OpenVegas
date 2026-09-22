"""Audit a canonical turn; explicitly restore only proven committed text results.

Initial engineering release is restricted to disposable local PostgreSQL databases
whose names start with ov_test_. It never loads .env, accepts production targets,
calls providers, changes balances, or retries a request. Set the dedicated
OPENVEGAS_RECONCILIATION_DATABASE_URL explicitly (not DATABASE_URL).

Run from the repository using python -m scripts.reconcile_inference_turn. Default
is read-only. A private --prompt-file JSON object with exactly prompt/max_tokens
can prove the original input hash. Review its plan_token, then explicitly pass
--apply --confirm-request <UUID> --confirm-plan <token> --operator <operator UUID>.
Operator identity is for audit only; database access is the authorization boundary.
No transcript, provider response, database URL, or raw exception is printed.
The private inference_turn_reconciliations table must already exist; this command
never installs the proposed migration or silently falls back to a public audit log.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from openvegas.gateway.conversation import CanonicalConversation
from openvegas.gateway.reconciliation import (
    ReconciliationError,
    canonical_uuid,
    inspect_turn,
    request_payload_hash,
    restore_turn,
)
from openvegas.capabilities import REASONING_EFFORTS

DB_ENV = "OPENVEGAS_RECONCILIATION_DATABASE_URL"
_MAX_INPUT = 400_000


def validate_target(args, url: str) -> str:
    """Literal loopback + disposable DB only; no DSN query host overrides."""
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"postgres", "postgresql"}
            or parts.hostname not in {"127.0.0.1", "::1"}
            or parts.query
            or parts.fragment
            or not re.fullmatch(r"/ov_test_[a-z0-9_]{1,48}", parts.path)
        ):
            raise ValueError
        _ = parts.port
        for value in (args.user, args.thread, args.request):
            canonical_uuid(value)
        if args.apply and (
            not args.prompt_file
            or args.confirm_request != args.request
            or not re.fullmatch(r"[0-9a-f]{64}", args.confirm_plan or "")
        ):
            raise ValueError
        if args.apply:
            canonical_uuid(args.operator)
    except (ValueError, TypeError, AttributeError):
        raise ReconciliationError(
            "LOCAL_DISPOSABLE_TARGET_AND_EXPLICIT_CONFIRMATIONS_REQUIRED"
        ) from None
    return url


def read_prompt_file(path: str | None) -> dict:
    if path is None:
        return {"prompt": None, "max_tokens": 1024}
    # Avoid blocking special files and following a swapped leaf symlink. POSIX
    # private permissions are required; no weaker Windows fallback is claimed.
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise ReconciliationError("PRIVATE_PROMPT_FILE_UNSUPPORTED_PLATFORM")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > _MAX_INPUT
            or info.st_mode & 0o077
            or info.st_uid != os.geteuid()
        ):
            raise ReconciliationError("PROMPT_FILE_MUST_BE_PRIVATE_REGULAR_AND_BOUNDED")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(_MAX_INPUT + 1)
        if len(data) > _MAX_INPUT:
            raise ReconciliationError("PROMPT_FILE_TOO_LARGE")
        payload = json.loads(data)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"prompt", "max_tokens"}
            or not isinstance(payload["prompt"], str)
            or type(payload["max_tokens"]) is not int
        ):
            raise ReconciliationError("INVALID_PROMPT_FILE_SHAPE")
        # Validate bounded input before establishing any database connection.
        request_payload_hash(
            CanonicalConversation(),
            provider="",
            model="",
            prompt=payload["prompt"],
            max_tokens=payload["max_tokens"],
        )
        return payload
    finally:
        os.close(descriptor)


class _LocalDB:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def transaction(self):
        async with self.connection.transaction():
            yield self.connection


async def run(args) -> dict:
    url = validate_target(args, os.environ.get(DB_ENV, ""))
    prompt = read_prompt_file(args.prompt_file)
    effort = getattr(args, "reasoning_effort", None)
    if effort is not None:
        if not isinstance(effort, str) or effort not in REASONING_EFFORTS:
            raise ReconciliationError("INVALID_REASONING_EFFORT")
        prompt["reasoning_effort"] = effort
    import asyncpg

    connection = None
    try:
        async with asyncio.timeout(30):
            connection = await asyncpg.connect(
                url,
                timeout=5,
                command_timeout=10,
                ssl=False,
                server_settings={
                    "application_name": "openvegas-local-turn-reconciliation",
                    "search_path": "public",
                    "lock_timeout": "3000",
                    "statement_timeout": "10000",
                    "idle_in_transaction_session_timeout": "10000",
                },
            )
            scope = {"user_id": args.user, "thread_id": args.thread, "request_id": args.request}
            if not args.apply:
                async with connection.transaction(isolation="repeatable_read", readonly=True):
                    return {
                        "action": "inspection_only",
                        **await inspect_turn(connection, **scope, **prompt),
                    }
            return {
                "action": "restore_history_only",
                **await restore_turn(
                    _LocalDB(connection),
                    **scope,
                    **prompt,
                    operator_id=args.operator,
                    expected_plan=args.confirm_plan,
                ),
            }
    finally:
        if connection is not None:
            try:
                await asyncio.wait_for(connection.close(), timeout=3)
            except TimeoutError:
                connection.terminate()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument(
        "--prompt-file", help="Private JSON file with original prompt and max_tokens"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--reasoning-effort", choices=REASONING_EFFORTS,
        help="Original request effort; omit only when the original request used provider default",
    )
    parser.add_argument(
        "--operator", help="Operator UUID for audit, not an authorization credential"
    )
    parser.add_argument("--confirm-request")
    parser.add_argument("--confirm-plan")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run(args))
        print(json.dumps(report, sort_keys=True))
        return 2 if report["status"] == "blocked" else 0
    except ReconciliationError as exc:
        # This class is used only for static codes, never interpolated user/driver data.
        print(
            json.dumps(
                {
                    "status": "not_confirmed",
                    "reason": str(exc),
                    "instruction": "Inspect the same turn before retrying; no automatic retry was made.",
                },
                sort_keys=True,
            )
        )
        return 1
    except Exception:  # noqa: BLE001 - neither transcript nor DSN-bearing driver errors may escape
        print(
            json.dumps(
                {
                    "status": "not_confirmed",
                    "instruction": "Inspect the same turn before retrying; no automatic retry was made. "
                    "Verify local target, private prompt file, stored facts and explicit confirmations.",
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
