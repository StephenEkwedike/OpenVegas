"""Browser-approved human-game credential, never an agent/payment/admin credential."""

import re

from fastapi import HTTPException

from server.services.integration_auth import IntegrationAuth


def allowed_game_request(method, path):
    if method == "GET" and path in {
        "/integrations/account",
        "/wallet/balance",
        "/casino/human/games",
    }:
        return True
    if method == "POST" and path in {
        "/casino/human/sessions/start",
        "/casino/human/rounds/start",
        "/games/horse/quotes",
        "/games/horse/play",
    }:
        return True
    uid = r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}"
    return bool(
        (
            method == "POST"
            and re.fullmatch("/casino/human/rounds/" + uid + "/(action|resolve)", path)
        )
        or (
            method == "GET"
            and re.fullmatch("/casino/human/(sessions/" + uid + "|rounds/" + uid + "/verify)", path)
        )
    )


async def resolve_game_user(request, token):
    from server.services.dependencies import get_redis

    if not allowed_game_request(request.method, request.url.path):
        raise HTTPException(403, "This terminal login permits human games and balance reads only")
    identity = await IntegrationAuth(get_redis()).resolve(token)
    if identity["kind"] != "game":
        raise HTTPException(403, "A separately approved game login is required")
    return {
        "user_id": identity["user_id"],
        "account_type": "human",
        "role": "authenticated",
        "auth_scope": "human_games",
    }
