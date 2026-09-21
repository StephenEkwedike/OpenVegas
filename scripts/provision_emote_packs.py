#!/usr/bin/env python3
"""Provision six private emote candidates locally; dry-run unless --apply.

Usage (from the checkout, using its installed Python dependencies):
  python -m scripts.provision_emote_packs --example
  python -m scripts.provision_emote_packs --plan /PRIVATE/release.json \
      --destination /PRIVATE/releases/candidate-1
  # Review the dry-run hashes, then repeat with --apply.

Create the destination PARENT first with owner-only permissions (chmod 700).
Supply separately authored private manifests/sheets, not current public previews.
Pin each file's SHA-256, exact pack ID and version in the plan. Include a bounded
provenance.json recording source_sha256 (and matching version if supplied).
Sources/destination must be absolute, outside public/package/Git trees, with no
symlink ancestors.

The installed root contains release-manifest.json plus six delivery_resource
directories with manifest.json, PNG and the exact provenance.json. Release
metadata records byte hashes and an artwork_fingerprint for approval records;
it contains no source paths or dates. Review your provenance for secrets before
supplying it. Output is deterministic for the same input bytes. --apply seals
files 0400/directories 0500 for the current user; the eventual backend operator
must separately arrange read-only access for the application UID. Existing
outputs are never overwritten. Interrupted writes require operator inspection
and a fresh destination. This POSIX operator tool does not certify Windows UX.

No .env/config is loaded; no network, billing, catalog, or deployment calls occur.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).absolute().parents[1]))

from openvegas.emotes.manifest import PackError
from openvegas.emotes.provisioning import PACK_SLOTS, provision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--example", action="store_true", help="Print an intentionally incomplete plan template"
    )
    parser.add_argument("--plan", help="Explicit six-pack release plan JSON")
    parser.add_argument("--destination", help="New private local root; parent must already exist")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write validated candidates locally, never enable sales",
    )
    args = parser.parse_args(argv)
    if args.example:
        if args.plan or args.destination or args.apply:
            parser.error("--example cannot be combined with provisioning arguments")
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "release_id": "candidate-1",
                    "packs": [
                        {
                            "pack_id": pack_id,
                            "version": "1.0.0",
                            "source_directory": f"/PRIVATE/{pack_id.removeprefix('openvegas.')}",
                            "manifest_sha256": "REPLACE_WITH_EXACT_SHA256",
                            "sheet_sha256": "REPLACE_WITH_EXACT_SHA256",
                            "provenance_sha256": "REPLACE_WITH_EXACT_SHA256",
                        }
                        for pack_id in sorted(PACK_SLOTS)
                    ],
                },
                indent=2,
            )
        )
        return 0
    if not args.plan or not args.destination:
        parser.error("--plan and --destination are required; omit --apply for a dry-run")
    try:
        result = provision(args.plan, args.destination, apply=args.apply)
    except PackError as exc:
        print(f"Provisioning refused: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print(
            "Provisioning refused: unsafe/unavailable local resource or interrupted write. "
            "No existing files were overwritten; inspect any partial destination.",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
