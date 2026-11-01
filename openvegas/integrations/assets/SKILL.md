---
name: openvegas
description: Open same-terminal OpenVegas games, human PvP, bounded test-chip agent Arena, wallet, owned cosmetics and integration status. Use for OpenVegas play, pvp, arena, roulette, blackjack, $V balance, or owned emote requests.
---

Use the OpenVegas MCP tools. The native host retains all file, shell, approval,
context and model orchestration. Do not replace its agent loop.

## Play Dispatch First

Managed-terminal exact `openvegas play` commands are handled by a local pre-model
hook where the host supports it. F8 opens local OpenVegas controls even if the host
rejects prompts because its allowance is exhausted. It uses the same terminal,
not a new tab, and does not invoke a model. On Mac keyboards F8 may require Fn.
The HUD shows this shortcut and a low-allowance reminder only when host usage
metadata is available. Do not invent remaining usage from context-window size.
If this skill reaches the model instead, follow the MCP fallback below; that
fallback DOES require available host inference. Do not claim it works at zero quota.
Local menu entry never places a wager or switches billing automatically.
`openvegas use v` and `openvegas use host` retain their names and spending consent.

For human multiplayer, call `pvp` once. Claude uses `/openvegas pvp`; Codex uses
`$openvegas pvp`. This reuses the same-terminal relay with a configured human seat
and non-redeemable test chips. The human controls every form; never answer for them.

For agent competition use `arena`, not `play` or `pvp`. It is available only with
an operator-issued bounded agent grant. Bare `arena` shows status/limits. When
asked to compete autonomously, observe, find a match, then choose legal actions
using the returned player-visible state, match/version and grant revision. Use
a stable request_id per action. No move-by-move human prompts are needed for
test chips within this grant. Poll at most twice per second; stop on paused,
stopped, expired, budget exhaustion or service errors. Never raise limits, create
grants or fall back to real $V play. An uncertain request must be observed before
any further move; never silently start another hand. Host permissions and native
model billing still apply; do not bypass either. User requests to stop take priority.
The human controller can pause, override, resume or stop separately. After an
intervention refresh the revision; never replay a stale action. No grant permits
reading other seats' cards, hidden reasoning, data-export consent or resale.

When the optional decision tools are exposed, use `get_context`, `list_games`
and `checkpoint` before choosing an opportunity. `choose` records decline,
continue_work or stop; `join_game` records entry against that same checkpoint.
Participation is optional, never an automatic response to low coding credits.
Do not collect task text, source code, credentials or private reasoning. Optional
`task_outcome` accepts an identifier and an unverified self-report, not a claim
that a game win completed the task. Never read the private seat files.

The old human `arena` entry is now named `pvp`; the old human identity environment
variable remains an alias, but never enables autonomous tools. Do not read private
seat/grant files or configure credentials. Missing multiplayer setup is not a
wallet login error. No installed-host certification or public release is implied.

When this skill is selected with `play` (including `$openvegas:openvegas play`),
call the OpenVegas `play` tool once as the next action: `{}` for a game menu,
or `{"game":"roulette"}` (or the other named supported game) when explicitly
requested. Never set `text_only` unless the user explicitly requests static text
forms without animations. Missing terminal setup is NOT permission for that
fallback. For `terminal_setup_required` or `terminal_disconnected`, show the
returned setup_command and explain it runs after exiting the host in the SAME
terminal. Do not claim the game table opened, retry, or run attachment from an
agent shell tool. Game selection is not wager consent. The tool owns
game selection and human amount submission. Submitting a valid amount places
one wager immediately; there is no second confirmation screen. The amount form
discloses loss and forfeiture before submission. Never submit it for the user.
Do not add your own "are you sure" question after the user requests play.
Do not first fetch the wallet, render a
player deck, inspect project files, search plugins, or plan a game implementation.
If tools are deferred, discover only the OpenVegas `play` tool, then call it.
Do not retry automatically. Return an unavailable/error result plainly rather
than promise that a menu opened. Never fill out or confirm a wager for the user.
If the result is `form_not_completed`, report that the host returned an unanswered
form, not that the user cancelled or that the menu was displayed. A host can
reject a form before rendering it. Do not retry or substitute a wager request.

The game forms display boxed public card hands, roulette layout/results, and
slot reels. A settled-board acknowledgement offers done/quit; either returns
to coding without another wager (or auto-closes after 30 seconds). Keep returned
payout/net amounts unchanged. Roulette requires choosing Spin after selecting
a bet; choosing a bet is not a spin. Progress distinguishes server calls from
waiting for the user's form. Never describe a still-running tool as proof that
the wheel is spinning, a form opened, or the server is delayed. If no form is
visible, say so; inactive forms stop after 3 minutes. Server mutations time out
after 15 seconds with an uncertain outcome saved for explicitly confirmed
recovery. Never retry the tool automatically or promise that no debit occurred.
After play returns, use its concise text receipt for the normal reply. Do not
dump structuredContent, round IDs, escaped tables or card arrays. A settled loss
is a completed game, not an integration failure. Report payout and net distinctly;
preserve display/recovery warnings without implying that a settled wager failed.
Codex's current elicitation form is static text, not an animated graphics canvas;
enabling terminal images for pets does not change that. If the user explicitly
enabled the local VS Code game-view experiment, the SAME `play` tool opens a
graphical VS Code tab instead. Do not run shell commands to enable it yourself.
It uses the same wallet, game engine, human confirmations and recovery journal.
Back to Codex / Esc closes the tab and focuses the existing terminal. No new host
or billing mode is launched. `ov integration game-view --disable` restores forms.
Claude's OpenVegas HUD adds antialiased card images, an eight-second roulette coast
and reel motion on supported image terminals. Cards use the landing-page palette.
Reduced motion and unsupported terminals retain a static readable fallback.
Neither renderer answers a game form. Do not claim native visual acceptance
before it has actually been observed in that host.

### Same-Terminal Game Handoff

In an activated managed terminal, the same play tool automatically shows the
colored terminal table and restores the SAME coding session on Quit. No new tab
or window, no Ctrl+Z/fg between games, no ov codex/claude billing launcher.
The outer terminal relay owns the display; host and table have separate PTYs.
Do not inject terminal bytes, suspend the host or simulate human inputs.
Current OpenVegas Graphics profiles connect games automatically. Users start
ordinary codex/claude and use play, then choose a game; no per-game shell command.
Managed zsh terminals use Codex's supported --no-daemon and --no-alt-screen options
internally: the shared daemon loses per-terminal capabilities and inline mode
preserves the coding screen while games borrow the alternate buffer. No global Codex settings or
host binaries are modified. User-defined codex aliases/functions are preserved.
For a legacy or unmanaged terminal only, one-time activation at the user's shell:
`ov integration terminal --attach`. Then launch ordinary codex or claude again
in THAT SAME terminal (resume the existing conversation if desired). Do not
execute attachment in a captured agent tool or ! command. It cannot safely
attach to a host already running. A failed managed connection never silently
falls back or repeats a wager. Unmanaged terminals report setup required BEFORE
opening forms or placing a wager; static forms require an explicit user request.
`ov integration terminal --check` checks the current shell without a game/wallet
request. Older Graphics profiles enabled only pets; update that owned profile,
not billing or provider settings. Do not repeatedly tell a user to attach when
they already did: an old managed shell needs the compatibility update and a
one-time host restart. Never guess another terminal's socket or expose its token.
For `game_login_required`, explain that the OpenVegas game account needs a fresh
login, not another terminal attachment or a Codex/Claude login. Do not request
passwords in chat or use cosmetic credentials as a substitute for wagering access.
Use the exact returned `login_command` when present; it selects the same account
profile as the game helper. Never shorten a returned local launcher path to the
production CLI. Use plain `openvegas login` only when no login_command is supplied, not
`openvegas go` or `login --browser`: those connect cosmetic-only credentials and
cannot restore the full game account. Login is account recovery, not a per-game
launch step. Resume the ordinary host in the same terminal afterward.
For `terminal_display_disconnected`, report a display connection failure, not a
backend outage, expired login or user cancellation. Do not retry or forfeit a
wager automatically. Preserve any returned settlement and explain that a
`display_warning` does not undo it. An already-running terminal relay needs a
one-time relaunch of that existing terminal after an update; restarting only the
coding host does not reload the outer relay. Never open a new terminal for this.
Codex's forms alone cannot display an animated wheel. Do not claim native visual
acceptance from PTY tests. No new terminal is required or opened.

The user can launch ordinary `claude` or `codex` in any folder after one-time
plugin setup. Neither an `ov` launcher nor $V inference is required for cosmetics.
For the same wallet interaction in both hosts, accept plain language such as
`OpenVegas: show my wallet and owned cosmetics`. Explicit skill invocation uses
the host's syntax, not an OpenVegas command protocol: Claude `/openvegas` is a
direct Mod command; Codex `$openvegas` invokes a skill and may use host AI credits.
Never tell a Codex user that `/openvegas` is an available native slash command.

## Explicit Q50 Motion Review

Only when the user asks to test the Q50 study: in regular Claude, use the direct
commands `/openvegas preview q50`, then `/openvegas demo`. Stop with
`/openvegas preview off`; `/pets off` hides the pane. This is a labeled local
artwork review, not purchased/equipped inventory, and needs no model prompt.
In regular Codex, after `ov integration pets --install --review-pack q50-jewelry`,
use `/pets` and select `OV Demo Q50 Motion` (no-prompt loop) or
`OV Q50 Motion (Status)` (idle is still; review plays once). Requires supported
terminal graphics. The original six-pose studies remain separate. Never claim
this grants a premium entitlement, changes billing, or certifies native UX.

## Shared OpenVegas Controls

Claude: `/openvegas play`. Codex: `$openvegas play`.
For that menu, call the `play` tool once with no arguments. The user selects a
game in the chooser; do not require typing a game name.

For an explicit `play` request, call the `play` tool once. Pass only the optional
`game` when the user named it; never choose a wager or move.
This opens the table through an activated same-terminal connection and returns
to coding, or reports setup required before any wager. The explicitly enabled
VS Code game tab described above remains a separate opt-in experiment.
Keep games in the current host: never require `ov codex`, `ov claude`, a second
terminal, or a shell game command to use this flow. Do not silently substitute
`!ov game` if Codex's model allowance prevents the skill from being invoked;
report that in-session entry is unavailable in that session. Only discuss the
separate shell game interface if the user explicitly asks for that alternative.
These forms reuse the existing full CLI account; browser-only cosmetic
credentials cannot wager. Do not promise native acceptance before the user has
verified their host version.
The user chooses the game, wager and moves; NEVER answer those forms yourself,
recommend a wager to recover losses, start repeated rounds, or use demo autoplay.
Invalid wager amounts reopen the same human form without a new tool call or
spending. Brief exit hint: type `quit` in the amount field, select `quit` in
choice menus, or choose False on recovery confirmation to return to coding.
Valid amount submission authorizes one wager; invalid input does not. Do not
retry the tool yourself or submit an amount on the user's behalf.
Quit ends the current game and forfeits an unfinished stake, never refunds it.
An already-decided result retains its actual settlement. The next play starts
a fresh session, even for the same game. There is no normal resume option.
An older paused round or interrupted transaction is closed with explicit human
confirmation before a new wager; recovery uses the original idempotency key. If the host lacks
form support or the full CLI account is unavailable, report the returned reason
without a shell workaround, new billing launcher, or cosmetic-grant escalation.
Claude: `/openvegas play roulette`. Codex: `$openvegas play roulette` (select its namespaced skill
if needed). Claude's direct Mod command calls the tool without an AI prompt.
Codex's skill request may use a normal AI request; game moves themselves do not
call an AI model. Native subscription quota is unchanged.

For `hud`, `games`, or a bare OpenVegas request, call `get_game_state` once.
Present a compact **OPENVEGAS / PLAYER DECK** with the actual balance and billing mode,
equipped companion/completion when available, then the returned `lobby.links`
as clickable Markdown links. Never invent a balance, ownership or game URL.
`Game guide` is instructions, not a playable browser casino. Offer `OpenVegas play`
for the interactive same-terminal table, but do not start a game on a navigation-only
request. Do not attach tokens or private data to links, or open them automatically.
Codex CLI has no verified custom persistent HUD surface: these are text/link
controls, not buttons over its prompt. Do not print cursor-control escapes.
Claude's Mod supplies real clickable HUD controls through its supported surface.
Selecting game navigation never authorizes spending or switching billing.
Keep the text-only version restrained: one heading, one wallet/billing line,
equipped slots when requested, and one row of the returned navigation links.
Use Games, Wallet and Cosmetics as the control names. Do not draw fake progress
meters, ASCII screen borders, or an imitation interactive terminal dashboard.
Preserve the real labels and destinations of the returned navigation links.

Use the same action words and meanings in both hosts. Accept short requests such
as `wallet`, `cosmetics`, `status`, `billing`, or `help` when this skill is selected.
Claude's direct commands are `/openvegas <action>`; Codex's skill requests are
`$openvegas <action>`. If the host displays a namespaced skill, select that entry.
The common plain-text form is `OpenVegas <action>`. Do not make up new verbs or
send users to a different coding launcher for these controls.

__OPENVEGAS_CONTROLS_TABLE__

`balance` aliases `wallet`; `owned` and `inventory` alias `cosmetics`; `redeem`
aliases `billing`. Keep these older names working, but teach the canonical names.
For `cosmetics`, summarize `cosmetics.entitlements` and `cosmetics.equipped` from
the actual result. Unavailable is not an empty inventory. For `status`, include
the active billing route and the reported renderer limitations; do not claim
native visual certification. `help` requires no account lookup or network tool.

Equipment requests use identical plain-text wording in both hosts:
`OpenVegas equip companion <item_id>` or `OpenVegas unequip companion` (use
`completion` for the other slot). Reuse `get_game_state` and `select_cosmetic`;
confirm the exact slot/item before setting `confirmed=true`. Unequip means
`item_id=null`, not purchase, refund, deletion or forfeiting ownership. Preserve
the other slot. These are assistant requests, not new immediate Claude commands.
If a selected item is unavailable for in-session rendering in this host, explain
that before asking for confirmation. Never offer a private-to-public export.

Use the same names **Companion** (waiting dance), **Completion** (answer animation),
**Owned cosmetics**, **Equipped**, and **Billing** in both hosts. A native answer
is not independently verified task success. Never silently substitute preview
art, buy an item, enable $V inference, or change billing to match the other host.

Animation controls are `/pets` and `/pets off` in BOTH terminal hosts. In Claude,
`/pets` opens the owned-art pane; its Test dance + completion button uses no AI.
In Codex, `/pets` opens its native picker; select OV Demo 1/2/3 for public loops
without an AI prompt, or an OpenVegas Preview for genuine host-turn timing.
Do not run a model prompt just to show an animation, change billing, or pretend
public previews are purchases. The two hosts own their controls and syntax.

- `get_wallet`: current signed-in wallet balance, without spending credits.
- `get_game_state`: owned/equipped cosmetics and actual integration status.
- `select_cosmetic`: ask which owned cosmetic the user wants, then confirm before equipping it.
- `redeem_info`: explain available billing routes; never configure a gateway or charge automatically.

Never ask for passwords, tokens or provider API keys, inspect credential files,
change host permissions, or claim a local completion earned credits. If signed out,
tell the user to run `openvegas go` in their shell; the existing OpenVegas website
handles authentication. Do not run the installer for them without an explicit request.

Normal host subscription billing stays unchanged. $V does not refill Claude or
ChatGPT subscription quota. The user enters `openvegas use v` (Claude:
`/openvegas use v`; Codex: `$openvegas use v`) to open a direct-provider quote
and bounded spending consent in the same terminal. `openvegas use host` restores
their original host billing. An installed pre-model hook handles these exact
commands without inference; the host may resume the same conversation to apply
the route. Never execute these commands through a shell tool, expose keys, or
invent a hot switch. If this prompt reached the model, report that the pre-model
connection is missing and ask for a native-host restart in the same configured
terminal. Do not use `ov codex` or `ov claude` as the normal command flow.
`get_wallet` reports the active route and budget. Unreviewed direct models remain
unavailable; there is no automatic provider/subscription fallback.
This plugin has no inference-spending or purchase tool.
Codex purchased/entitled cosmetic controls remain text-only. Public previews can
use Codex CLI's own in-session pet renderer: the user runs `ov integration pets
--install` once, starts a fresh Codex CLI session in a supported graphics terminal,
and selects an OpenVegas preview with `/pets`. `/pets off` disables it. Running
loops the companion dance; Ready plays one authored completion clip, then idle.
These are public previews, not purchased or entitlement-gated artwork. Never
export private owned assets to Codex's persistent pet/cache folders. VS Code's
integrated terminal needs image support. On macOS, `ov integration terminal
--install` adds an optional OpenVegas Graphics terminal profile: choose it in
the terminal '+' dropdown, then run plain `codex`. It declares Sixel via a real
terminfo entry and omits the brand variable Codex 0.163 misdetects, without
wrapping either host or changing the default profile. Workspace GPU/image
overrides can still disable graphics. `ov demo codex` remains an optional
capability-probed shortcut, never a required billing launcher. Demo pets loop
at idle without an AI prompt; they are not real task completion. Prompts typed
afterward use the host's normal billing. tmux/Zellij remain unsupported.
Native typing/scrolling/visual acceptance is still unverified; a finished host
turn is not proof a task succeeded. This adapter does not run another agent loop.

Claude's terminal Mod fetches the actual equipped companion/completion through
the scoped private delivery service. No preview is presented as owned. Missing
ownership, revoked/expired grants, unavailable delivery or refresh failure clears
the art; a cached file is never authorization. `/openvegas` refreshes the pane;
the first successful owned-art load opens it without taking keyboard focus.
`/openvegas pause` disables motion without touching host prompts or approvals.
`/pets off` hides the pane and stops its animation, persisting across reloads;
`/pets` opens it again. `/openvegas demo` (or the Test dance + completion button) plays a bounded visual
test inside Claude, using current equipped/leased art without inference. A real
turn, pause, expired ownership or session end stops it. Maximize the terminal
if it reports insufficient space. A demo is not native lifecycle certification.
The companion loops during an observed main native turn while its ownership
lease stays valid. One completion clip plays only on the matching native `answer` event, never on local Stop,
abort, refusal or error. An answer is not proof that a task or test succeeded.
No private frame bytes belong in MCP answers, skills or plugin distributions.

Display all wallet, stake, payout and net amounts as `$V 0.00` (two decimal places, currency prefix; e.g. `$V -49.80`). Keep original full-precision tool values for calculations and requests. Never invent a balance when unavailable.
