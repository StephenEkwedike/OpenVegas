"""Offline synthetic evaluation; no model calls, wallets or network access."""

import argparse
import json
from pathlib import Path

from .runner import suite, verify_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--repeats", type=int, default=10)
    verify = sub.add_parser("verify")
    verify.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = (
        suite(args.output, args.repeats) if args.command == "run" else verify_bundle(args.directory)
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
