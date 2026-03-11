# OpenVegas UX + Demo Isolation Enhancements Plan

## Summary
1. Keep real gameplay API and fairness flow unchanged.
2. Add a dedicated admin-only demo endpoint for forced-win recording mode.
3. Add terminal radio-select UX (`openvegas ui`) with safe input handling.
4. Add confetti animation for any profitable round (`net > 0`) when animation is enabled.
5. Ship npm wrapper distribution (`npx` + global install) while keeping Python CLI canonical.
6. Keep billing settlement webhook-authoritative; success/verify pages remain informational.

## Key Clarification
This is a simple, targeted enhancement and **not** a complete rehaul of all game endpoints.
- Existing `POST /games/{game}/play` remains intact.
- New demo behavior is isolated in `POST /games/{game}/play-demo` with admin+env gating.

## Scope

### In scope
1. New admin-only demo route.
2. Demo outcome isolation in storage and analytics.
3. `openvegas ui` Textual wizard.
4. Win-confetti on positive net outcomes.
5. npm wrapper commands for real users.

### Out of scope (this phase)
1. In-app user cashout endpoint.
2. Changes to canonical fairness/settlement logic for normal play.
3. Premium-plan-style entitlement work.

## Public Interface Changes
1. Keep normal request clean in `/server/routes/games.py`:

```python
class PlayRequest(BaseModel):
    amount: float
    type: str = "win"
    horse: int | None = None
    stop_position: int | None = None
```

2. Add demo request type:

```python
class DemoPlayRequest(BaseModel):
    amount: float
    type: str = "win"
    horse: int | None = None
    stop_position: int | None = None
```

3. Add route:
- `POST /games/{game_name}/play-demo` (admin-only, env-gated)

4. Add env flags in `.env.example`:

```env
OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED=0
OPENVEGAS_DEMO_ADMIN_USER_IDS=
```

5. Add CLI command:
- `openvegas ui`

## Implementation Plan

## 1) Add Isolated Admin Demo Endpoint
File: `/Users/stephenekwedike/Desktop/OpenVegas/server/routes/games.py`

1. Add admin check helper:

```python
import os

def _is_demo_admin(user_id: str) -> bool:
    if os.getenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "0") != "1":
        return False
    allow = {
        x.strip()
        for x in os.getenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "").split(",")
        if x.strip()
    }
    return user_id in allow
```

2. Add dedicated endpoint:

```python
@router.post("/{game_name}/play-demo")
async def play_game_demo(
    game_name: str,
    req: DemoPlayRequest,
    user: dict = Depends(get_current_user),
):
    if game_name not in GAMES:
        raise HTTPException(400, f"Unknown game: {game_name}")
    if not _is_demo_admin(user["user_id"]):
        raise HTTPException(403, "Demo mode not allowed")
    # same validation + escrow + settle shape as normal play,
    # but resolution uses bounded forced-win loop.
```

3. Forced-win loop (bounded):

```python
MAX_DEMO_ATTEMPTS = 250
result = None
for i in range(MAX_DEMO_ATTEMPTS):
    attempt_nonce = nonce + i
    candidate = await game.resolve(bet, rng, client_seed, attempt_nonce)
    if candidate.net > 0:
        result = candidate
        nonce = attempt_nonce
        break
if result is None:
    raise HTTPException(500, "Unable to force demo win within cap")
```

4. Mark outcome as demo-only:

```python
result.outcome_data = {
    **(result.outcome_data or {}),
    "demo_mode": True,
    "demo_forced_win": True,
    "demo_attempts": i + 1,
    "canonical_fairness": False,
}
```

5. Return explicit demo flags in API response (not storage-only):

```python
return {
    "game_id": game_id,
    "bet_amount": str(result.bet_amount),
    "payout": str(result.payout),
    "net": str(result.net),
    "outcome_data": result.outcome_data,
    "server_seed_hash": result.server_seed_hash,
    "provably_fair": False,
    "demo_mode": True,
    "canonical": False,
}
```

6. Keep deterministic search cap configurable and tuned per game:

```python
default_cap = int(os.getenv("OPENVEGAS_DEMO_MAX_ATTEMPTS", "120"))
game_cap = int(os.getenv(f"OPENVEGAS_DEMO_MAX_ATTEMPTS_{game_name.upper()}", default_cap))
MAX_DEMO_ATTEMPTS = max(1, min(game_cap, 500))
```

## 2) Persist Demo Isolation for Analytics/Fairness
Files:
- `/Users/stephenekwedike/Desktop/OpenVegas/supabase/migrations/014_demo_mode_isolation.sql`
- query call-sites that power metrics/leaderboards

1. Add `is_demo` flag on `game_history`:

```sql
ALTER TABLE game_history
ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_game_history_is_demo_created
ON game_history(is_demo, created_at DESC);
```

2. Demo route writes `is_demo = TRUE`; normal route keeps `FALSE`.

3. Add mandatory filter in canonical queries:

```sql
... WHERE is_demo = FALSE
```

4. Surface explicit label in CLI/UI for demo wins.

5. Enforce verify separation in route layer:
- canonical verify endpoint rejects demo rounds, or
- demo rounds use a separate demo verify endpoint.

```python
@router.get("/verify/{game_id}")
async def verify_game(game_id: str, user: dict = Depends(get_current_user)):
    row = await db.fetchrow(
        "SELECT id, user_id, is_demo, server_seed, server_seed_hash, client_seed, nonce, provably_fair "
        "FROM game_history WHERE id = $1 AND user_id = $2",
        game_id, user["user_id"],
    )
    if not row:
        raise HTTPException(404, "Game not found")
    if row["is_demo"]:
        raise HTTPException(400, "Demo round: use /games/demo/verify/{game_id}")
    return {
        "game_id": str(row["id"]),
        "server_seed": row["server_seed"],
        "server_seed_hash": row["server_seed_hash"],
        "client_seed": row["client_seed"],
        "nonce": row["nonce"],
        "provably_fair": row["provably_fair"],
    }
```

```python
@router.get("/demo/verify/{game_id}")
async def verify_demo_game(game_id: str, user: dict = Depends(get_current_user)):
    if not _is_demo_admin(user["user_id"]):
        raise HTTPException(403, "Demo verification not allowed")
    row = await db.fetchrow(
        "SELECT id, user_id, is_demo, outcome_data, server_seed_hash, nonce "
        "FROM game_history WHERE id = $1 AND user_id = $2 AND is_demo = TRUE",
        game_id, user["user_id"],
    )
    if not row:
        raise HTTPException(404, "Demo round not found")
    return {
        "game_id": str(row["id"]),
        "demo_mode": True,
        "canonical": False,
        "server_seed_hash": row["server_seed_hash"],
        "nonce": row["nonce"],
        "note": "Demo verification surface (non-canonical; do not use for real fairness stats)",
    }
```

6. Use demo-specific ledger labels so accounting can exclude demo flow cleanly:

```python
# keep canonical UUID game_id shape
game_id = str(uuid.uuid4())

# demo play lifecycle should be fully symmetrical and go through WalletService,
# not ad hoc ledger inserts, so invariants stay identical to canonical flows.
demo_ref = f"demo:{game_id}"
await wallet.place_bet(
    account_id,
    bet_amount,
    game_id,
    entry_type="demo_play",
    reference_id=demo_ref,
)

if payout > 0:
    await wallet.settle_win(
        account_id,
        payout,
        game_id,
        entry_type="demo_win",
        reference_id=demo_ref,
    )
    remaining = bet_amount - payout
    if remaining > 0:
        await wallet.settle_loss(
            game_id,
            remaining,
            entry_type="demo_loss",
            reference_id=demo_ref,
        )
else:
    await wallet.settle_loss(
        game_id,
        bet_amount,
        entry_type="demo_loss",
        reference_id=demo_ref,
    )
```

If current wallet APIs do not yet support typed/reference overrides, add explicit demo wrappers in `WalletService` and call those wrappers from demo routes.

```python
async def demo_place_bet(self, account_id: str, amount: Decimal, game_id: str, *, tx=None):
    await self._execute(
        LedgerEntry(
            debit_account=account_id,
            credit_account=f"escrow:{game_id}",
            amount=self._money(amount),
            entry_type="demo_play",
            reference_id=f"demo:{game_id}",
        ),
        tx=tx,
    )
```

```sql
-- reporting exclusion baseline
... WHERE entry_type NOT IN ('demo_play', 'demo_win', 'demo_loss')
```

7. Exclude demo rounds from promo/social surfaces by default:
- recent wins feeds
- achievement feeds
- marketing screenshot/video data pulls

```sql
... WHERE is_demo = FALSE
```

8. Keep demo verify output visibly non-canonical in clients:

```python
if result.get("demo_mode"):
    console.print("[bold yellow]DEMO VERIFY[/bold yellow] [dim](canonical: false)[/dim]")
```

## 3) Add Terminal Radio-Select Wizard (`openvegas ui`)
Files:
- `/Users/stephenekwedike/Desktop/OpenVegas/openvegas/tui/wizard.py`
- `/Users/stephenekwedike/Desktop/OpenVegas/openvegas/cli.py`

1. CLI entry point:

```python
@cli.command("ui")
def interactive_ui():
    from openvegas.tui.wizard import run_wizard
    run_wizard()
```

2. Safe radio guard (no null-selection crash):

```python
with RadioSet(id="action"):
    yield RadioButton("Deposit", value=True)  # default selection
    yield RadioButton("Play Horse")
    yield RadioButton("Mint")
    yield RadioButton("Balance")
    yield RadioButton("Verify")

radio = self.query_one("#action", RadioSet)
pressed = radio.pressed_button
if pressed is None:
    self.query_one("#output", Static).update("Select an action first.")
    return
action = pressed.label.plain
```

Compatibility note:
- verify default-selection constructor behavior against pinned `textual` version.
- if `value=True` is not supported in the pinned version, set the default selection in `on_mount`.

```python
def on_mount(self) -> None:
    self.client = OpenVegasClient()
    radio = self.query_one("#action", RadioSet)
    if radio.pressed_button is None and radio.children:
        first = radio.children[0]
        if isinstance(first, RadioButton):
            first.value = True
```

Blue radio selection styling (if supported by current Textual CSS selectors):

```python
CSS = """
RadioSet:focus {
    border: round #2563eb;
}
RadioButton.-selected {
    color: #60a5fa;
    text-style: bold;
}
"""
```

3. Parse typed amount as Decimal before API call:

```python
from decimal import Decimal, InvalidOperation

try:
    amount = Decimal(arg)
except InvalidOperation:
    self.query_one("#output", Static).update("Invalid amount.")
    return

data = await self.client.create_topup_checkout(amount)
```

## 4) Add Confetti for Any Positive Net Outcome
Files:
- `/Users/stephenekwedike/Desktop/OpenVegas/openvegas/tui/confetti.py`
- `/Users/stephenekwedike/Desktop/OpenVegas/openvegas/cli.py`
- `/Users/stephenekwedike/Desktop/OpenVegas/ui/index.html`

1. CLI confetti renderer stays terminal-safe (short, bounded, optional):

```python
import random
import time
from rich.console import Console

def render_confetti(console: Console, frames: int = 10, width: int = 52) -> None:
    colors = ["red", "yellow", "green", "cyan", "magenta", "blue"]
    for _ in range(frames):
        line = "".join(
            f"[{random.choice(colors)}]{random.choice('*+x•')}[/]" for _ in range(width)
        )
        console.print(line)
        time.sleep(0.02)
```

2. Trigger when `net > 0` and animation enabled:

```python
from openvegas.config import load_config
from openvegas.tui.confetti import render_confetti

# keep this in command orchestration path, not pure formatting helpers
if net > 0 and load_config().get("animation", True):
    render_confetti(console)
```

3. Add “real” confetti only for web `/ui` flows (not terminal CLI), using your provided React/CSS approach translated to existing page stack:
- create overlay container
- generate ~50 particles
- animate with `@keyframes fall`
- auto-reset after ~3s
- respect reduced-motion

```css
@keyframes fall {
  0% { transform: translateY(0) rotate(0deg); opacity: 1; }
  100% { transform: translateY(100vh) rotate(360deg); opacity: 0; }
}

.animate-fall { animation: fall 3s ease-out forwards; }

@media (prefers-reduced-motion: reduce) {
  .animate-fall { animation: none !important; }
}
```

```js
function triggerConfetti() {
  const root = document.getElementById("confetti-root");
  if (!root) return;
  root.innerHTML = "";
  const colors = ["#2563eb", "#60a5fa", "#22c55e", "#f59e0b", "#ef4444", "#a855f7"];
  for (let i = 0; i < 50; i += 1) {
    const p = document.createElement("div");
    p.className = "particle animate-fall";
    p.style.left = `${Math.random() * 100}%`;
    p.style.animationDelay = `${Math.random() * 2}s`;
    p.style.backgroundColor = colors[Math.floor(Math.random() * colors.length)];
    root.appendChild(p);
  }
  setTimeout(() => { root.innerHTML = ""; }, 3200);
}
```

## 5) npm Wrapper Distribution
Files:
- `/Users/stephenekwedike/Desktop/OpenVegas/npm-cli/package.json`
- `/Users/stephenekwedike/Desktop/OpenVegas/npm-cli/bin/openvegas.js`

1. Package setup:

```json
{
  "name": "@openvegas/cli",
  "version": "0.1.0",
  "bin": {
    "openvegas": "bin/openvegas.js"
  },
  "type": "module"
}
```

2. Wrapper script with robust fallback messaging:

```javascript
#!/usr/bin/env node
import {spawnSync} from "node:child_process";

const args = process.argv.slice(2);
let r = spawnSync("openvegas", args, {stdio: "inherit"});
if (r.status === 0) process.exit(0);

r = spawnSync("pipx", ["run", "openvegas", ...args], {stdio: "inherit"});
if (r.status === 0) process.exit(0);

console.error(
  "OpenVegas CLI not found. Install one of:\n" +
  "  pipx install openvegas\n" +
  "or\n" +
  "  pip install openvegas"
);
process.exit(1);
```

3. Validate PyPI package name before publish; if canonical name differs, adjust wrapper fallback command.

4. Validate binary name before release as part of packaging checks:

```bash
python3 -m pip index versions openvegas
pipx run openvegas --version
```

If binary/package names differ, wrapper fallback should use the published names explicitly.

## 6) Billing Docs Consistency
Files:
- `/Users/stephenekwedike/Desktop/OpenVegas/README.md`
- `/Users/stephenekwedike/Desktop/OpenVegas/all-commands.md`

Add explicit operational text:
1. Payments flow into Stripe account tied to `STRIPE_SECRET_KEY`.
2. Top-up credit issuance is webhook-driven, not success-page-driven.
3. Cashout to bank is manual in Stripe Dashboard in this phase.

## Test Plan

### Unit
1. Admin helper returns false for non-allowlisted users.
2. Confetti trigger only fires on `net > 0`.
3. Wizard amount parser rejects invalid numeric input.

### Integration
1. Normal `/games/{game}/play` behavior unchanged.
2. `/games/{game}/play-demo` rejects non-admin (`403`).
3. `/games/{game}/play-demo` returns profitable demo outcome for admin when enabled.
4. Demo rows written with `is_demo = TRUE` and demo metadata flags.

### Data/Analytics
1. Canonical leaderboard/fairness queries exclude demo rows (`WHERE is_demo = FALSE`).
2. Demo outcomes are visibly labeled in CLI/UI output.
3. Wallet/billing/revenue reporting excludes demo-tagged traffic unless report is explicitly demo-inclusive.
4. Recent wins/achievement/social/marketing data surfaces exclude demo rows unless explicitly demo-inclusive.

### Accounting Isolation
1. Demo play writes demo-specific ledger entry types (`demo_play`, `demo_win`, `demo_loss`) or equivalent demo flag.
2. Default revenue/billing reporting queries exclude demo ledger traffic.
3. Explicit test for demo accounting exclusion:

```python
async def test_demo_play_excluded_from_default_reporting(...):
    # create demo play
    # assert ledger entry_type in ('demo_play', 'demo_win', 'demo_loss')
    # assert default reporting query excludes demo ledger rows
    ...
```
4. Canonical wallet integrity test:

```python
async def test_demo_wallet_integrity_no_default_financial_leakage(...):
    # execute demo round
    # assert writes went through wallet service path (not ad hoc SQL bypass)
    # assert only demo-labeled ledger path used for demo settlement
    # assert default financial summary queries exclude demo-ledger rows
    # assert demo-inclusive summaries include them when explicitly requested
    ...
```

### Distribution
1. `npx @openvegas/cli balance` invokes CLI.
2. Global install path `npm i -g @openvegas/cli` + `openvegas balance` works.
3. Missing Python/pipx path yields actionable message.
4. npm package exposes exactly one executable in `bin` for deterministic `npx` resolution.

```json
{
  "bin": {
    "openvegas": "bin/openvegas.js"
  }
}
```

## Acceptance Criteria
1. Forced win is isolated to `/games/{game}/play-demo`; no force-win parameter exists on standard play request.
2. Demo outcomes are marked non-canonical and excluded from production fairness metrics, leaderboards, and house-edge analytics.
3. `openvegas ui` provides radio-select workflow with null-selection guard and typed Decimal inputs.
4. Confetti appears for any winning outcome where `net > 0` and animation is enabled.
5. npm wrapper supports both `npx` and global install usage.
6. Docs clearly state Stripe destination account behavior and manual cashout path.
7. Canonical verify endpoint rejects demo rounds (or redirects to a separate demo verify surface).
8. Wallet/billing/revenue reporting excludes demo-tagged traffic unless explicitly demo-inclusive.
9. Demo ledger entries use demo-specific entry types or flags and are excluded from default accounting/revenue reports.
10. Canonical wallet balance integrity is preserved: demo rounds only affect demo-labeled ledger paths and are excluded from default financial summaries unless explicitly included.
11. npm wrapper publishes exactly one `bin` executable so `npx` resolution is deterministic.

## Assumptions
1. Demo mode is strictly for recording/testing and admin use.
2. Python package remains canonical; npm is distribution convenience.
3. No in-app withdrawal/cashout endpoint is added in this phase.
