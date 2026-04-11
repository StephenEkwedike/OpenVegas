"""OpenClaw / agent runtime skill manifest.

An agent runtime like OpenClaw loads this manifest to discover
what actions OpenVegas exposes. The agent then calls these as
HTTP tool-use actions with its ov_agent_* bearer token.
"""

OPENVEGAS_SKILL_MANIFEST = {
    "name": "openvegas",
    "description": "Compute infrastructure for autonomous agents — inference routing, deterministic boosts, and casino games for probabilistic compute top-ups",
    "version": "0.3.7",
    "base_url": "https://app.openvegas.ai",
    "auth": {
        "type": "bearer",
        "token_prefix": "ov_agent_",
        "header": "Authorization",
        "note": "Token issued by org admin via: openvegas agent token issue --agent <name> --scopes infer,boost,casino.play,budget.read",
    },
    "actions": [
        {
            "name": "start_session",
            "description": "Start a metered session. Agent gets a $V spend envelope.",
            "method": "POST",
            "path": "/v1/agent/sessions/start",
            "body": {"envelope_v": "number"},
            "returns": {"session_id": "string", "envelope_v": "string", "remaining_v": "string"},
            "scope_required": "infer",
        },
        {
            "name": "infer",
            "description": "Run metered AI inference. Routes to OpenAI, Anthropic, or Gemini.",
            "method": "POST",
            "path": "/v1/agent/infer",
            "body": {
                "session_id": "string",
                "prompt": "string",
                "provider": "openai | anthropic | gemini",
                "model": "string",
            },
            "returns": {"text": "string", "v_cost": "string", "input_tokens": "int", "output_tokens": "int"},
            "scope_required": "infer",
        },
        {
            "name": "check_budget",
            "description": "Check remaining $V in current session envelope.",
            "method": "GET",
            "path": "/v1/agent/budget?session_id={session_id}",
            "returns": {"remaining_v": "string", "spent_v": "string", "status": "string"},
            "scope_required": "budget.read",
        },
        {
            "name": "request_boost_challenge",
            "description": "Request a deterministic coding challenge. Complete it to earn $V.",
            "method": "POST",
            "path": "/v1/agent/boost/challenge",
            "body": {"session_id": "string"},
            "returns": {"challenge_id": "string", "task_prompt": "string", "max_reward_v": "string"},
            "scope_required": "boost",
        },
        {
            "name": "submit_boost",
            "description": "Submit code artifact for deterministic scoring and reward.",
            "method": "POST",
            "path": "/v1/agent/boost/submit",
            "body": {"challenge_id": "string", "artifact_text": "string"},
            "returns": {"score": "number", "reward_v": "string", "details": "object"},
            "scope_required": "boost",
        },
        {
            "name": "casino_start_session",
            "description": "Start an agent-only casino session for probabilistic compute top-ups.",
            "method": "POST",
            "path": "/v1/agent/casino/sessions/start",
            "body": {"agent_session_id": "string", "max_loss_v": "number"},
            "returns": {"casino_session_id": "string", "max_loss_v": "string", "max_rounds": "int"},
            "scope_required": "casino.play",
        },
        {
            "name": "casino_list_games",
            "description": "List available casino games with rules and RTP.",
            "method": "GET",
            "path": "/v1/agent/casino/games",
            "scope_required": "casino.play",
        },
        {
            "name": "casino_start_round",
            "description": "Place a wager and start a new game round.",
            "method": "POST",
            "path": "/v1/agent/casino/rounds/start",
            "body": {
                "casino_session_id": "string",
                "game_code": "poker | blackjack | baccarat | roulette | slots",
                "wager_v": "number",
            },
            "returns": {"round_id": "string", "state": "object", "valid_actions": ["string"]},
            "scope_required": "casino.play",
        },
        {
            "name": "casino_action",
            "description": "Submit a game action (hit, stand, draw, hold, spin, bet_player, etc.).",
            "method": "POST",
            "path": "/v1/agent/casino/rounds/{round_id}/action",
            "body": {"action": "string", "payload": "object", "idempotency_key": "string"},
            "returns": {"state": "object", "valid_actions": ["string"]},
            "scope_required": "casino.play",
        },
        {
            "name": "casino_resolve",
            "description": "Finalize round, reveal RNG seed, settle payout.",
            "method": "POST",
            "path": "/v1/agent/casino/rounds/{round_id}/resolve",
            "returns": {"payout_v": "string", "net_v": "string", "outcome": "object", "rng_reveal": "string"},
            "scope_required": "casino.play",
        },
    ],
}
