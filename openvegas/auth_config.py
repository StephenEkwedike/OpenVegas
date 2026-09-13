"""Validation for publicly discoverable Supabase connection settings."""

import base64
import json
from urllib.parse import urlsplit


def public_auth_config(url: str, key: str) -> dict[str, str]:
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Invalid public authentication URL")
    if not parsed.hostname or not (parsed.scheme == "https" or
            (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("Authentication URL requires HTTPS outside loopback")
    if key.startswith("sb_publishable_"):
        return {"supabase_url": url.rstrip("/"), "supabase_anon_key": key}
    # Inspect only to avoid publishing a service-role credential, not to authenticate users.
    try:
        payload = key.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if not isinstance(claims, dict) or claims.get("role") != "anon":
            raise ValueError("Only a public anonymous authentication key may be published")
    except (IndexError, TypeError, KeyError, json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("Only a public anonymous authentication key may be published") from exc
    return {"supabase_url": url.rstrip("/"), "supabase_anon_key": key}
