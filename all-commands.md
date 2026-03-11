Where to see balances:

As a User view:
openvegas balance
openvegas history

How much money OpenVegas has made -System/company view (SQL):

select account_id,balance
from wallet_accounts
where account_id in ('house','store','mint_reserve','rake_revenue');

# OpenVegas Commands

This is the complete command list currently exposed by the Python CLI in
`openvegas/cli.py`.

## Install and launch

```bash
# local install (current implementation is Python, not npm)
pip install -e .

# CLI help/version
openvegas --help
openvegas --version
openvegas ui
```

## Authentication

```bash
openvegas signup
openvegas login
openvegas login --otp
openvegas logout
openvegas status
```

## Wallet

```bash
openvegas balance
openvegas history
openvegas deposit <amount>
openvegas deposit-status <topup_id>
```

## Keys

```bash
openvegas keys set <anthropic|openai|gemini>
openvegas keys list
```

## Mint ($V from BYOK burn)

```bash
openvegas mint --amount <usd> --provider <anthropic|openai|gemini> [--mode <solo|split|sponsor>]
```

Examples:

```bash
openvegas mint --amount 5 --provider anthropic --mode solo
openvegas mint --amount 10 --provider openai --mode sponsor
openvegas mint --amount 3 --provider gemini --mode split
```

## Play games (backend endpoints)

```bash
openvegas play <horse|skillshot> --stake <amount> [--horse <n>] [--type <win|place|show>] [--demo-force-win]
```

Examples:

```bash
openvegas play horse --stake 5 --horse 2 --type win
openvegas play skillshot --stake 5
openvegas play horse --stake 1 --horse 1 --type win --demo-force-win
```

## Inference spend

```bash
openvegas ask "<prompt>" [--provider <openai|anthropic|gemini>] [--model <model_id>]
openvegas models [--provider <openai|anthropic|gemini>]
```

Examples:

```bash
openvegas ask "Summarize this repo"
openvegas ask "Write tests for this function" --provider anthropic --model claude-sonnet-4-20250514
openvegas models
openvegas models --provider openai
```

## Store and redemption catalog

```bash
openvegas store list
openvegas store buy <item_id> [--idempotency-key <key>]
openvegas store grants
```

Examples:

```bash
openvegas store buy ai_starter --idempotency-key run-001
openvegas store grants
```

## Provably-fair verification

```bash
openvegas verify <game_id> [--demo]
```

Demo verify example:

```bash
openvegas verify <game_id> --demo
```

## Config

```bash
openvegas config show
openvegas config set <key> <value>
```

Supported keys:

- `default_provider` (`openai|anthropic|gemini`)
- `default_model_<provider>` (example: `default_model_openai`)
- `theme`
- `animation` (`true|false|1|0|yes|no`)
- `backend_url`
- `supabase_url`
- `supabase_anon_key`

Examples:

```bash
openvegas config set default_provider openai
openvegas config set default_model_openai gpt-4o-mini
openvegas config set backend_url https://api.openvegas.gg
openvegas config show
```

## Offline demo commands (not CLI API flow)

`demo.py` runs local/offline simulations with fake balance:

```bash
python3 demo.py
python3 demo.py horse
python3 demo.py skillshot
python3 demo.py blackjack
python3 demo.py roulette
python3 demo.py slots
python3 demo.py poker
python3 demo.py baccarat
```

## Important distinction

- `openvegas ...`: backend/API flow (auth, mint, wallet, inference, backend games).
- `python3 demo.py ...`: offline demo flow (no real credits/tokens, no backend auth/session/policy).

## Agent API routes (HTTP)

These are not CLI commands; they are backend routes for `ov_agent_*` tokens:

```text
POST /v1/agent/sessions/start
POST /v1/agent/infer
GET  /v1/agent/budget?session_id=<id>
POST /v1/agent/boost/challenge
POST /v1/agent/boost/submit
POST /v1/agent/casino/sessions/start
GET  /v1/agent/casino/games
POST /v1/agent/casino/rounds/start
POST /v1/agent/casino/rounds/{round_id}/action
POST /v1/agent/casino/rounds/{round_id}/resolve
GET  /v1/agent/casino/rounds/{round_id}/verify
GET  /v1/agent/casino/sessions/{session_id}
```

## npm wrapper (real-user distribution convenience)

```bash
npx @openvegas/cli --help
npx @openvegas/cli balance

npm i -g @openvegas/cli
openvegas balance
```
