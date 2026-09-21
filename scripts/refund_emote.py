#!/usr/bin/env python3
"""Operator-only $V refund. Read-only by default; never loads .env or calls Stripe."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
from urllib.parse import urlsplit
from uuid import UUID


def validate_target(args, url: str) -> str:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"postgres", "postgresql"}
            or not parts.hostname
            or parts.query
            or parts.fragment
        ):
            raise ValueError
        _ = parts.port
        for value in (args.user, args.order):
            if str(UUID(value)) != value or not UUID(value).int:
                raise ValueError
        if args.apply:
            if (
                not args.operator
                or str(UUID(args.operator)) != args.operator
                or not UUID(args.operator).int
            ):
                raise ValueError
            if args.confirm_order != args.order:
                raise ValueError
            if parts.hostname not in {"localhost", "127.0.0.1", "::1"} and (
                not args.allow_remote or args.confirm_host != parts.hostname
            ):
                raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError(
            "Require a valid DATABASE_URL, canonical user/order UUIDs, and explicit apply confirmations; credentials hidden"
        ) from None
    return url


async def run(args) -> dict:
    url = validate_target(args, os.getenv("DATABASE_URL", ""))
    import asyncpg

    from openvegas.store.refunds import inspect_refund, refund_cosmetic
    from server.services.dependencies import PostgresDB

    remote = urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}
    pool = await asyncpg.create_pool(
        url, min_size=1, max_size=1, timeout=5, command_timeout=10,
        ssl=ssl.create_default_context() if remote else False,
    )
    try:
        db = PostgresDB(pool)
        if not args.apply:
            async with pool.acquire() as conn, conn.transaction(readonly=True):
                return {
                    "action": "inspection_only",
                    **await inspect_refund(conn, user_id=args.user, order_id=args.order),
                }
        async with asyncio.timeout(30):
            return {
                "action": "refund_v_only",
                **await refund_cosmetic(
                    db,
                    user_id=args.user,
                    order_id=args.order,
                    operator_id=args.operator,
                    reason=args.reason,
                ),
            }
    finally:
        try:
            await asyncio.wait_for(pool.close(), timeout=5)
        except TimeoutError:
            pool.terminate()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--order", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-order")
    parser.add_argument(
        "--operator",
        help="Authenticated support operator's UUID, for audit only; not an authorization token",
    )
    parser.add_argument(
        "--reason",
        choices=("customer_request", "defective_pack", "duplicate_purchase", "operator_correction"),
        default="customer_request",
    )
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--confirm-host")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args)), sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - driver exceptions can contain credentials
        print(
            f"Refund not confirmed ({type(exc).__name__}); inspect the same order before retrying. No raw credentials or driver details are printed."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
