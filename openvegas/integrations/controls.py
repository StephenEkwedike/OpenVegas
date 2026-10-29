"""Shared user-facing action names; keep host syntax and existing MCP tools."""
from __future__ import annotations

READ_ACTIONS = (
    ("help", "Show OpenVegas controls", None),
    ("hud", "Show the OpenVegas lobby, wallet and navigation", "get_game_state"),
    ("games", "Open game controls and instructions; never place a bet", "get_game_state"),
    ("wallet", "Show your $V balance and active billing route", "get_wallet"),
    ("cosmetics", "List owned cosmetics and equipped companion/completion", "get_game_state"),
    ("status", "Show integration and rendering availability", "get_game_state"),
    ("billing", "Explain the billing route without switching or spending", "redeem_info"),
)
ALIASES = {"balance": "wallet", "owned": "cosmetics", "inventory": "cosmetics", "redeem": "billing"}


def action_catalog():
    return [{"name": name, "description": description, "tool": tool}
            for name, description, tool in READ_ACTIONS]


def lobby_navigation(host: str, base_url: str):
    """Fixed, credential-free destinations on the configured backend only."""
    from urllib.parse import urlsplit

    base = base_url.rstrip("/")
    parsed = urlsplit(base)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path
            or not parsed.hostname or any(ord(c) < 33 for c in base)
            or (parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
        return {"available": False, "message": "Navigation unavailable; no alternate backend was selected."}
    return {
        "title": "OpenVegas Lounge",
        "mode": "native_mod" if host == "claude" else "text_links",
        "links": [{"label": label, "url": base + path} for label, path in (
            ("Game guide", "/ui/how-to-play"), ("Wallet", "/ui/balance"),
            ("Cosmetics", "/ui/emotes"),
        )],
        "games": {"command": "/openvegas play" if host == "claude" else "$openvegas play", "entry": "Play",
                  "instruction": "Open the game table in this same terminal. "
                                 "Choose and confirm the wager yourself; navigation never places a bet. "
                                 "Requires an activated game connection and a matching full CLI login. "
                                 "Pet image support alone does not activate game handoff."},
        "notice": "Navigation only. No credits spent, purchases made or billing changes."
                  + (" Codex CLI displays text and links, not a persistent custom HUD." if host == "codex" else ""),
    }


def help_text():
    return "OpenVegas controls:\n" + "\n".join(
        f"{name}: {description}" for name, description, _ in READ_ACTIONS
    ) + (
        "\nplay: Choose a game; submitting your amount places one wager."
        "\nuse v: Review direct API pricing and approve a session ceiling in this terminal."
        "\nuse host: Return to your original host billing. No automatic prompt or fallback."
        "\nAnimations: /pets opens the controls; /pets off hides them in either host."
        "\nEquipment: ask 'OpenVegas equip companion <item_id>' or "
        "'OpenVegas unequip companion' (completion is the other slot); confirm the change."
        "\nThese assistant requests can use host AI credits. Equipping never purchases an item."
    )


def skill_controls():
    rows = ["| Action | Meaning | Existing tool |", "| --- | --- | --- |"]
    rows += [f"| `{name}` | {description} | " + (f"`{tool}`" if tool else "Explain this table; no tool needed") + " |"
             for name, description, tool in READ_ACTIONS]
    return "\n".join(rows)
