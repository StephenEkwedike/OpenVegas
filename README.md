# OpenVegas

Terminal arcade + API for wagering `$V`, running provably fair games, and handling inference credit flows.

## Local Setup

```bash
cd /Users/stephenekwedike/Desktop/OpenVegas
cp .env.example .env
```

Fill `.env` with real values:
- `SUPABASE_JWT_SECRET`
- `DATABASE_URL` (replace `[YOUR-PASSWORD]`)
- `REDIS_URL` (optional)

Install and run:

```bash
pip install -e ".[server,dev]"
uvicorn server.main:app --reload
```

Open:
- API health: `http://127.0.0.1:8000/health`
- UI landing page: `http://127.0.0.1:8000/ui`

## Supabase Schema

Apply SQL files in `/supabase/migrations` in order (`001` -> `012`), then apply `/supabase/seed.sql`.

## Remotion Video

```bash
cd my-video
npm i
npm run dev
```

Render:

```bash
npx remotion render OpenVegasHorseRace out/openvegas-horse.mp4
```
