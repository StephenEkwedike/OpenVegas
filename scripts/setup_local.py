#!/usr/bin/env python3
"""Create a new .env.local using only the running, unlinked local Supabase stack."""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    target = ROOT / ".env.local"
    if target.exists():
        print(".env.local already exists; no files changed.")
        return 1
    try:
        result = subprocess.run(
            ["supabase", "status", "--workdir", str(ROOT / "dev"), "-o", "json"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        status = json.loads(result.stdout)
        replacements = {
            "DATABASE_URL": status["DB_URL"], "SUPABASE_URL": status["API_URL"],
            "SUPABASE_ANON_KEY": status["ANON_KEY"],
            "SUPABASE_JWT_SECRET": status.get("JWT_SECRET", ""),
        }
        from urllib.parse import urlsplit
        if any(urlsplit(replacements[key]).hostname not in {"localhost", "127.0.0.1", "::1"}
               for key in ("DATABASE_URL", "SUPABASE_URL")):
            raise ValueError("Non-local status endpoint")
        lines = []
        for line in (ROOT / ".env.local.example").read_text().splitlines():
            name = line.split("=", 1)[0]
            lines.append(f"{name}={replacements[name]}" if name in replacements else line)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write("\n".join(lines) + "\n")
    except Exception as exc:  # noqa: BLE001 -- Do not echo local status credentials.
        print(f"Local configuration unavailable ({type(exc).__name__}); start the dev Supabase stack first.")
        return 1
    print("Created private .env.local from local Supabase. Existing .env was not read or changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
