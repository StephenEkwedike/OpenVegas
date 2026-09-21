#!/usr/bin/env python3
"""Offline-first public model review. No keys, .env, DB writes or paid calls.

1. Inspect an explicitly supplied raw /api/v1/models JSON snapshot:
   python -m scripts.review_openrouter_models --input /absolute/models.json
2. Optional: --fetch-public performs ONE unauthenticated, bounded public GET.
   --save-snapshot /absolute/new-models.json saves its exact bytes for later review.
3. Generate an incomplete review template (repeat --model for each exact ID):
   ... --input /absolute/models.json --template --model author/exact-model
4. Fill the template with independently verified managed-account chat access,
   reviewed bounds/dates/capabilities and explicit OpenVegas retail $V rates.
   Set pricing_scope_acknowledged=true only after verifying the scope below.
   ... --input /absolute/models.json --observed-at 2026-09-21T12:00:00Z \
       --review /absolute/review.json --ack-account-access --ack-retail-prices

The observation date must describe when those bytes were obtained, not when an
old file was opened. Public prices must be observed within 24 hours. Reviews
expire within 30 days; current pricing may change sooner. The runtime must
enforce exact model, zero request fee, reviewed token-price caps and no plugins.
The tool rejects request charges, reasoning rates above the output cap, and
conditional pricing overrides. Reasoning may be included only within the
reviewed combined output-token budget; verify that behavior for the endpoint.
Image/audio/web rates are retained but excluded ONLY from this text-only route.
If a cache-write rate is listed, account review must verify there are no automatic
paid cache writes on the selected endpoint; explicit cache_control is forbidden.
These rates are NOT represented as free. Do not reuse this bundle for multimodal,
plugin or prompt-cache-write traffic without a separate adapter/pricing review.

Output defaults to stdout. --output writes a new JSON file exclusively, with no
symlink ancestors or overwrites. It never installs rows or sets environment
variables. The separate operator install must place provider_catalog rows and
model_reviews (OPENVEGAS_MODEL_REVIEWS_JSON) together and verify matching prices.
Rows remain disabled. PUBLIC LISTING DOES NOT PROVE MANAGED ACCOUNT ACCESS.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).absolute().parents[1]))

from openvegas.gateway.openrouter_catalog import (
    MAX_BYTES,
    ReviewError,
    candidate_report,
    fetch_public_models,
    parse_json,
    review_template,
    reviewed_bundle,
)


@contextmanager
def _parent(raw: str):
    path = Path(raw)
    if (
        not path.is_absolute()
        or str(path) != raw
        or ".." in path.parts
        or path.suffix.lower() != ".json"
        or path.name.startswith(".")
        or not raw.isprintable()
        or "\\" in raw
        or raw.startswith("//")
    ):
        raise ReviewError("Use an explicit canonical absolute .json path")
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ReviewError("Safe operator file IO requires POSIX directory handles")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptors = []
    try:
        fd = os.open(path.anchor, flags)
        descriptors.append(fd)
        for part in path.parts[1:-1]:
            fd = os.open(part, flags, dir_fd=fd)
            descriptors.append(fd)
        yield fd, path.name
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _read(path: str, *, limit: int = MAX_BYTES) -> bytes:
    with _parent(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not 0 < info.st_size <= limit
            ):
                raise ReviewError("Input must be a bounded single-link regular JSON file")
            chunks, remaining = [], limit + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(fd)
            if len(data) != info.st_size or (info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ReviewError("Input changed while reading")
            return data
        finally:
            os.close(fd)


def _write(path: str, data: bytes) -> None:
    with _parent(path) as (parent, name):
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        try:
            remaining = memoryview(data)
            while remaining:
                count = os.write(fd, remaining)
                if not count:
                    raise OSError("Short output write")
                remaining = remaining[count:]
            os.fsync(fd)
        finally:
            os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--input", help="Raw saved public models JSON; default operation is entirely offline"
    )
    source.add_argument(
        "--fetch-public", action="store_true", help="Explicit single public GET with no API key"
    )
    parser.add_argument(
        "--save-snapshot",
        help="With --fetch-public, write exact response bytes to a new .json file",
    )
    parser.add_argument(
        "--observed-at", help="Actual timezone-aware observation time of saved input"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--review", help="Explicit operator plan JSON; creates a disabled reviewed bundle"
    )
    mode.add_argument(
        "--template",
        action="store_true",
        help="Print an incomplete review plan for selected --model IDs",
    )
    parser.add_argument(
        "--model", action="append", default=[], help="Exact model selection for --template only"
    )
    parser.add_argument("--ack-account-access", action="store_true")
    parser.add_argument("--ack-retail-prices", action="store_true")
    parser.add_argument("--output", help="Write JSON exclusively; default stdout")
    args = parser.parse_args(argv)
    if args.save_snapshot and not args.fetch_public:
        parser.error("--save-snapshot requires --fetch-public")
    if args.fetch_public and args.observed_at:
        parser.error("A live public observation is timestamped by the tool")
    if bool(args.model) != args.template:
        parser.error("--template requires --model; --model is only valid with --template")
    if (args.ack_account_access or args.ack_retail_prices) and not args.review:
        parser.error("Acknowledgements apply only to --review")
    if args.review and (not args.ack_account_access or not args.ack_retail_prices):
        parser.error("--review requires both explicit acknowledgements")
    if args.review and not (args.observed_at or args.fetch_public):
        parser.error("Review of an offline snapshot requires its actual --observed-at timestamp")
    try:
        payload = asyncio.run(fetch_public_models()) if args.fetch_public else _read(args.input)
        observed = datetime.now(UTC).isoformat() if args.fetch_public else args.observed_at
        report = candidate_report(payload, observed_at=observed)
        if args.review:
            report = reviewed_bundle(
                payload,
                parse_json(_read(args.review, limit=1_000_000)),
                observed_at=observed,
                ack_account_access=args.ack_account_access,
                ack_retail_prices=args.ack_retail_prices,
            )
        elif args.template:
            report = review_template(report, args.model)
        data = (json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()
        if args.save_snapshot:
            _write(args.save_snapshot, payload)
        if args.output:
            _write(args.output, data)
        else:
            sys.stdout.write(data.decode())
        return 0
    except ReviewError as exc:
        print(f"Model review refused: {exc}", file=sys.stderr)
    except OSError:
        print(
            "Model review refused: unsafe/unavailable file or existing output; no overwrite attempted.",
            file=sys.stderr,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
