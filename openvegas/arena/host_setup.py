"""Load only explicitly installed local pilot identities, never general host settings."""

import os
from pathlib import Path

from .__main__ import local_url, private_read
from .service import token_valid


def load_host(host, profile):
    if host not in {"claude", "codex"}:
        raise ValueError("unknown_host")
    path = Path(profile) / "integrations" / f"arena-{host}.json"
    if not path.exists():
        return
    data = private_read(path)
    if set(data) - {"agent_file", "human_file"}:
        raise ValueError("unexpected_pilot_setting")
    values = {}
    for key, env in (
        ("agent_file", "OPENVEGAS_ARENA_AGENT"),
        ("human_file", "OPENVEGAS_PVP_IDENTITY"),
    ):
        if key not in data:
            continue
        identity_path = Path(data[key])
        if not identity_path.is_absolute():
            raise ValueError("absolute_identity_required")
        identity = private_read(identity_path)
        local_url(identity["url"])
        token_valid(identity["token"])
        if key == "human_file" and identity.get("pool") != "human":
            raise ValueError("human_seat_required")
        values[env] = str(identity_path)
    os.environ.update(values)
