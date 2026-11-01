# OpenVegas

OpenVegas is a terminal-based AI coding platform with a credit wallet and optional
casino-style games. Users spend `$V` on AI usage, buy credits on the website, or
wager existing credits. `$V` is an application accounting unit, not a model token
or a way to reset another product's subscription limits.

- `openvegas chat`: local conversational coding/file-tool runtime, with optional host microphone input.
- `openvegas ui`: guided inline terminal games/wallet menu.
- FastAPI serves both the API and static browser pages under `/ui`; there is no separate frontend server.

## Native Coding Hosts

The current package defines both `ov` and `openvegas` executable names:

```sh
ov go                 # One-time account/plugin setup; native billing unchanged
ov codex              # Explicitly confirmed $V-funded Codex session
ov claude             # Explicitly confirmed $V-funded Claude session
```

The short launchers show the selected model, pricing and spending limit before
asking for consent. `--model` and `--limit-v` override the defaults. Normal
`codex`/`claude` launches retain their own billing; OpenVegas does not refill
subscription quotas. The backend must separately enable reviewed native billing.
These commands are implemented locally; an older published package will need
a separately approved release/update.

For isolated local testing use `dev/ov` (or its absolute path from any project),
not a globally installed production CLI. See
[native testing and free animation previews](docs/task-briefs/native-integrations/NATIVE_TESTING.md).

## Development

Do not overwrite an existing `.env` or point tests at a hosted database. The
isolated environment uses local Supabase Auth/PostgreSQL, Redis and one API process.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements.lock -e '.[server,dev]'
supabase start --workdir dev
.venv/bin/python scripts/setup_local.py
.venv/bin/python scripts/migrate.py --env-file .env.local --apply
docker compose up -d --build
.venv/bin/python scripts/check_local.py probe
```

Open http://127.0.0.1:8000/ui. To test the host CLI against this local backend:

```sh
.venv/bin/python scripts/run_local.py cli login
.venv/bin/python scripts/run_local.py cli chat
```

[Local development](docs/LOCAL_DEVELOPMENT.md) covers prerequisites, host API mode,
separate local account state, stopping/restarting, and optional audio.

## Verification and deployment

- [Audit and actual evidence](docs/OPENVEGAS_AUDIT.md)
- [Manual and automated tests](docs/MANUAL_TESTING.md)
- [Railway restoration, ownership and production approval](docs/RAILWAY_DEPLOYMENT.md)

```sh
.venv/bin/python scripts/check_local.py fast
.venv/bin/python scripts/smoke_local_auth.py
```

The latter creates a **local-only** test account and verifies real Auth/wallet
flows. No Stripe or AI paid calls are made. Readiness alone is not product proof.
Production migrations and seed scripts never run automatically on API startup.

## Creative development

The separate `my-video` Remotion project is not needed to run the API or CLI.

```sh
cd my-video
npm install
npm run dev
npx remotion render OpenVegasHorseRace out/openvegas-horse.mp4
```
