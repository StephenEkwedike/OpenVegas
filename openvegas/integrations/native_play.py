"""One human-controlled casino round through native MCP forms, never terminal escapes.

Uses a CLI login or browser-approved game credential, not a cosmetic host grant. The private journal
shares the emote store's no-follow locking and atomic writes. An ambiguous write
is never automatically retried: the next invocation asks before replaying its
original idempotency key. No model-supplied wager/action arguments are accepted.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from openvegas.client import APIError
from openvegas.emotes.spool import _locked, _read, atomic_write

from .credentials import IntegrationError, profile
from .game_result import amount
from .game_table import GAMES, table

MENU_TIMEOUT_SECONDS = 12
ACTION_TIMEOUT_SECONDS = 15
FORM_TIMEOUT_SECONDS = 180
RESULT_TIMEOUT_SECONDS = 30


async def notify(callback, message):
    # Progress is best-effort, never part of consent or settlement.
    if callback is not None:
        try:
            async with asyncio.timeout(0.5):
                await callback(message)
        except Exception:
            pass


async def menu_read(operation):
    try:
        async with asyncio.timeout(MENU_TIMEOUT_SECONDS):
            return await operation
    except TimeoutError:
        raise IntegrationError("OpenVegas game menu could not load within 12 seconds. "
                               "Check the local backend and account connection. No new wager was placed.") from None


def _form_schema(schema):
    # Codex rejects root titles (property titles are supported by both hosts).
    schema.pop("title", None)


class HostForm(BaseModel):
    model_config = ConfigDict(json_schema_extra=_form_schema)


class Confirm(HostForm):
    confirm: bool = Field(description="Confirm the exact action above. Choose False to return to coding.")


class Number(HostForm):
    # Codex's form UI supports string choices, but not numeric input controls.
    number: Literal[tuple([*(str(n) for n in range(37)), "quit"])] = Field(
        description="Choose 0 through 36, or quit to return to coding.")


def choice_model(name, choices):
    return create_model(name, __base__=HostForm,
                        choice=(Literal[tuple(choices)], Field(description="Choose an option")))


def summary(game, result):
    return table(game, result)


class Journal:
    def __init__(self, directory):
        self.directory = directory
        try:
            self.data = json.loads(_read(directory, "round.json", 65536))
        except FileNotFoundError:
            self.data = {}
        if not isinstance(self.data, dict):
            raise IntegrationError("Invalid game recovery record; no new wager was started.")

    def save(self):
        atomic_write(self.directory, "round.json", json.dumps(self.data, allow_nan=False).encode())
        # Wager recovery must survive a process crash, unlike transient emote events.
        if isinstance(self.directory, int):
            fd = os.open("round.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.directory)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(self.directory)


async def run_round(client, ask, journal, account, *, retry_invalid_input=True, progress=None,
                    requested_game=None, continue_existing=False, present=None, wager_on_submit=False,
                    show_lobby=False, browser_action=None):
    if requested_game is not None and requested_game not in GAMES:
        raise IntegrationError("Unsupported game. No wager was placed.")
    async def approved(message):
        answer = await ask(message, Confirm)
        return answer is not None and answer.confirm is True

    async def send(method, kwargs):
        journal.data["pending"] = {"method": method, "kwargs": kwargs}
        journal.save()
        return await dispatch()

    async def dispatch():
        operation = journal.data["pending"]
        allowed = {"human_casino_start_session", "human_casino_start_round",
                   "human_casino_action", "human_casino_resolve"}
        if operation["method"] not in allowed:
            raise IntegrationError("Invalid saved game operation.")
        stage = {"human_casino_start_session": "Opening your game session",
                 "human_casino_start_round": "Placing your confirmed wager and loading the table",
                 "human_casino_action": "Sending your chosen move",
                 "human_casino_resolve": "Settling the server result"}[operation["method"]]
        if operation["method"] == "human_casino_action" and operation["kwargs"]["action"] == "spin":
            stage = "Requesting the spin result"
        await notify(progress, f"{stage}. Server response limit: {ACTION_TIMEOUT_SECONDS:g} seconds.")
        try:
            async with asyncio.timeout(ACTION_TIMEOUT_SECONDS):
                result = await getattr(client, operation["method"])(**operation["kwargs"])
        except TimeoutError:
            raise IntegrationError(f"{stage} did not return within {ACTION_TIMEOUT_SECONDS:g} seconds. "
                "The server may already have accepted it. Recovery is saved; no automatic retry, "
                "new wager or refund. Back to coding. Use OpenVegas play to confirm recovery of "
                "that same operation, not a new bet.") from None
        except APIError as exc:
            if (operation['method'] == 'human_casino_action' and
                    operation['kwargs']['action'] in {'double', 'split'} and exc.status == 400 and
                    exc.data.get('detail') == 'Not enough $V for that extra stake. Choose hit or stand.'):
                journal.data.pop('pending')
                journal.data['result']['notice'] = exc.data['detail']
                journal.save()
                return journal.data['result']
            if exc.status != 409 or not exc.data.get("error"):
                raise
            result = exc.data
        if result.get("error"):
            if (operation['method'] == 'human_casino_action' and result['error'] == 'extra_stake_limit'
                    and result.get('current_state') == 'awaiting_action'):
                journal.data.pop('pending')
                journal.data['result']['notice'] = 'Extra stake exceeds your session limit. Choose hit or stand.'
                journal.save()
                return journal.data['result']
            session_errors = {"session_expired": "closed", "session_round_capped": "round_capped",
                              "session_loss_capped": "loss_capped"}
            session_state = session_errors.get(result["error"])
            if (operation["method"] == "human_casino_start_round" and
                    session_state and result.get("current_state") == session_state):
                # These exact backend responses are persisted before any debit.
                # Close this attempt, but require a new invocation and consent.
                journal.data["stage"] = "terminal"
                journal.data["result"] = {**result, "round_id": None}
                journal.data.pop("pending")
                journal.save()
                return result
            terminal_errors = {"round_already_resolved": "resolved", "round_expired": "expired",
                               "invalid_transition": "canceled"}
            state = terminal_errors.get(result["error"])
            # Only the authenticated backend's exact terminal-state response can
            # close recovery. Network/validation failures remain pending.
            if (operation["method"] in {"human_casino_action", "human_casino_resolve"}
                    and state and result.get("current_state") == state):
                journal.data["stage"] = "terminal"
                journal.data["result"] = {**result, "round_id": operation["kwargs"]["round_id"]}
                journal.data.pop("pending")
                journal.save()
                return result
            raise IntegrationError("Game backend rejected the action. Recovery record retained.")
        journal.data["result"] = result
        journal.data["stage"] = operation["method"]
        if result.get("current_state") == "resolved":
            journal.data["stage"] = "human_casino_resolve"
        journal.data.pop("pending")
        journal.save()
        return result

    def stopped():
        return {"status": "interrupted" if journal.data else "cancelled",
                "round_id": journal.data.get("result", {}).get("round_id"),
                "message": "Back to coding. No wager was refunded. Any interrupted transaction "
                           "must be closed before a fresh game; it will not resume normal play."}

    async def end_round():
        """Explicit Quit ends play, but a decided result keeps its actual payout."""
        if journal.data.get("pending"):
            raise IntegrationError("An interrupted transaction must be confirmed for recovery before closing; no refund or new wager.")
        result = journal.data.get("result", {})
        if (result.get("round_id") and journal.data.get("stage") not in {"terminal", "human_casino_resolve"}):
            if result.get("current_state") != "resolvable":
                await send("human_casino_action", {"round_id": result["round_id"], "action": "forfeit",
                           "payload": {}, "idempotency_key": "native-play-" + uuid.uuid4().hex})
            if journal.data.get("result", {}).get("current_state") == "resolvable":
                await send("human_casino_resolve", {"round_id": result["round_id"],
                           "idempotency_key": "native-play-" + uuid.uuid4().hex})
            if journal.data.get("stage") not in {"terminal", "human_casino_resolve"}:
                raise IntegrationError("Previous round could not be closed. No new wager was placed.")
        else:
            journal.data["stage"] = "terminal"
            journal.save()
        return {"status": "closed", "round_id": result.get("round_id"),
                "message": "Game ended; back to coding. Unfinished wagers are forfeited, not refunded. "
                           "Already-decided results keep their normal settlement. Your next game starts fresh."}

    data = journal.data
    if data and data.get("user_id") != account["user_id"]:
        raise IntegrationError("Saved round belongs to a different account.")
    if data and data.get("stage") in {"human_casino_resolve", "horse_settled", "terminal"} and not data.get("pending"):
        # A settled round is retained until the next explicit tool invocation.
        journal.data = data = {}
        journal.save()
    if data and data.get('game') == 'horse':
        from .horse_play import dispatch_horse
        if not await approved('Recover ONLY the interrupted horse wager with its original idempotency key? '
                              'This may debit the originally selected price; no second wager.'):
            return stopped()
        recovered = await dispatch_horse(client, journal, present=present, progress=progress)
        return recovered or {'status': 'cancelled', 'round_id': None,
                             'message': 'Horse quote expired without a wager. OpenVegas play starts a fresh selection.'}
    if data and continue_existing:
        if data.get("pending"):
            if not await approved("Recover ONLY the interrupted operation with its original idempotency key?"):
                return stopped()
            await dispatch()
    elif data:
        message = ("End the previous " + data["game"] + " game (" + amount(data["wager"]) +
                   ") before starting " + (requested_game or "a new game") +
                   "? Any unfinished stake is forfeited, not refunded; an already-decided result settles normally.")
        if data.get("pending"):
            message += " The last response was interrupted. Confirm recovery of ONLY that operation with its original idempotency key, then close it. An unprocessed original wager may still be debited and forfeited. No second wager is authorized."
        if not await approved(message):
            return stopped()
        if data.get("pending"):
            await dispatch()
        await end_round()
        # Refresh after settlement; never reuse the balance from before a payout.
        fresh = await menu_read(client._request("GET", "/integrations/account"))
        if fresh.get("user_id") != account["user_id"]:
            raise IntegrationError("Game account changed. No new wager was started.")
        account = fresh
        journal.data = data = {}
        journal.save()
    if not data:
        if show_lobby and not requested_game:
            mode = choice_model('ChooseMode', ['quick_play', 'arena', 'sign_in', 'purchase_v', 'quit'])
            message = 'Welcome to OpenVegas!\nQuick Play (vs House) / Arena (Coming Soon)\nEsc / Quit returns to coding.'
            while True:
                selected_mode = await ask(message, mode)
                if selected_mode is None or selected_mode.choice == 'quit':
                    return stopped()
                if selected_mode.choice == 'quick_play':
                    break
                if selected_mode.choice in {'sign_in','purchase_v'}:
                    if browser_action is None:
                        message='Browser actions are unavailable in this client. Update the installed OpenVegas integration.'
                        continue
                    if not await browser_action(selected_mode.choice):
                        return stopped()
                    fresh=await menu_read(client._request('GET','/integrations/account'))
                    if fresh.get('user_id')!=account['user_id']:
                        raise IntegrationError('Signed in to a different account. Reopen OpenVegas play in this same terminal. No wager started.')
                    account=fresh
                    message='Welcome back! Balance: '+amount(account['balance'])+'\nChoose Quick Play or quit.'
                    continue
                message = ('Arena is coming soon. Public PvP is not open.\n'
                           'No wager placed. Choose Quick Play to play against the house, or quit.')
        catalog = await menu_read(client.human_casino_list_games())
        games = [g["game_code"] for g in catalog.get("games", [])
                 if g.get("game_code") in GAMES]
        # Horse racing uses the existing /games/horse quote service, not the
        # casino session catalog. Do not send it to human_casino_start_round.
        games = list(dict.fromkeys([*games, 'horse']))
        if not games:
            raise IntegrationError("No human games are enabled on this backend.")
        try:
            minimum = Decimal(str(catalog["min_wager_v"]))
        except (KeyError, InvalidOperation, TypeError, ValueError):
            raise IntegrationError("Game service must expose its minimum wager before native play. Update the backend.")
        if not minimum.is_finite() or minimum <= 0:
            raise IntegrationError("Invalid game minimum; no wager was started.")
        selection = choice_model("ChooseGame", [*games, "quit"])
        if requested_game and requested_game not in games:
            raise IntegrationError("Requested game is not enabled on this backend. No wager was placed.")
        selected = selection(choice=requested_game) if requested_game else await ask(
            "OpenVegas / Play. Balance: " + amount(account["balance"]) +
            ". Games can lose credits. Choose a game, or quit to return to coding.", selection)
        if selected is None or selected.choice == "quit":
            return stopped()
        while True:
            try:
                balance = Decimal(str(account["balance"]))
            except (KeyError, InvalidOperation, TypeError, ValueError):
                raise IntegrationError("Wallet balance is unavailable. No wager was started; back to coding.") from None
            if not balance.is_finite() or balance < 0:
                raise IntegrationError("Wallet balance is unavailable. No wager was started; back to coding.")
            if balance >= minimum:
                break
            if browser_action is None:
                return {"status": "unavailable", "round_id": None,
                        "message": f"Balance: {amount(balance)}; minimum wager: {amount(minimum)}. "
                                   "No valid wager is available. No wager was started; back to coding."}
            topup = await ask(
                f"Balance: {amount(balance)}. This game needs at least {amount(minimum)}.\n"
                "Purchase $V in your browser, or quit to return to coding. No wager placed.",
                choice_model('LowBalance', ['purchase_v', 'quit']),
            )
            if topup is None or topup.choice == 'quit' or not await browser_action('purchase_v'):
                return stopped()
            fresh = await menu_read(client._request('GET', '/integrations/account'))
            if fresh.get('user_id') != account['user_id']:
                raise IntegrationError('Game account changed. No new wager was started.')
            account = fresh
        stake_model = create_model("Wager", __base__=HostForm, wager=(str, Field(
            description="Bet amount in $V, or quit to return to coding.")))
        balance_line = f"Balance: {amount(balance)}"
        minimum_line = f"Enter at least {amount(minimum)}."
        instructions = "Press Enter to submit, or type quit to return to coding."
        prompt = f"{balance_line}\n{minimum_line}\n{instructions}"
        if selected.choice == 'blackjack':
            instructions += '\nOptional Split / Double add a stake only when you choose them.'
            prompt = f"{balance_line}\n{minimum_line}\n{instructions}"
        if selected.choice == 'horse':
            instructions = 'Enter a budget, then choose your horse. Type quit to return to coding.'
            prompt = f"{balance_line}\n{minimum_line}\n{instructions}"
        # Retry human input only, before any journal write, session or debit.
        # Finite shell commands opt out so fixed invalid arguments cannot loop.
        while True:
            stake = await ask(prompt, stake_model)
            if stake is None or stake.wager.strip().lower() == "quit":
                return stopped()
            raw = stake.wager.strip()
            try:
                wager = Decimal(raw) if len(raw) <= 128 else Decimal("NaN")
            except InvalidOperation:
                wager = Decimal("NaN")
            if (wager.is_finite() and minimum <= wager <= balance
                    and wager.as_tuple().exponent >= -6):
                break
            if wager.is_finite() and wager < minimum:
                error = minimum_line
            elif wager.is_finite() and wager > balance:
                error = "Amount exceeds your balance."
            else:
                error = "Enter a valid amount."
            prompt = f"{balance_line}\nError: {error}\n{instructions}"
            if not retry_invalid_input:
                raise IntegrationError(prompt)
        if selected.choice == 'horse':
            from .horse_play import horse_round
            return await horse_round(client, ask, journal, account, wager, present=present, progress=progress)
        if not wager_on_submit and not await approved(f"Place one {selected.choice} wager of {amount(wager)}? You can lose the entire wager. "
                              "No top-up or AI billing change. Quit ends this game and forfeits any unfinished stake; it never refunds it."):
            return stopped()
        journal.data = data = {"user_id": account["user_id"], "game": selected.choice,
                               "wager": str(wager)}
        await send("human_casino_start_session", {"max_loss_v": str(min(balance, wager * 8) if selected.choice == 'blackjack' else wager), "max_rounds": 1,
                                                    "idempotency_key": "native-play-" + uuid.uuid4().hex})

    if data["stage"] == "human_casino_start_session":
        session_id = data["result"]["casino_session_id"]
        await send("human_casino_start_round", {"casino_session_id": session_id,
                   "game_code": data["game"], "wager_v": data["wager"],
                   "idempotency_key": "native-play-" + uuid.uuid4().hex})
    # Each input is a host form; no inference loop is used to decide game moves.
    for _ in range(64):
        result = data["result"]
        if present:
            display = {**result, "current_state": "resolved"} if data["stage"] == "human_casino_resolve" else result
            await present(data["game"], display)
        if data["stage"] == "terminal":
            return {"status": result["current_state"], "round_id": result["round_id"],
                    "message": "The server reports this round is closed. No new wager or refund was made. "
                               "Check wallet history for its settlement. Back to coding."}
        if data["stage"] == "human_casino_resolve":
            return {"status": "resolved", **{k: result[k] for k in
                    ("round_id", "outcome", "payout_v", "net_v") if k in result},
                    "table": summary(data["game"], {**result, "current_state": "resolved"}),
                    "message": "Round settled by the existing ledger. Back to coding. No repeat wager."}
        if result.get("current_state") == "resolvable":
            await send("human_casino_resolve", {"round_id": result["round_id"],
                       "idempotency_key": "native-play-" + uuid.uuid4().hex})
            continue
        allowed = {"hit", "stand", "double", "split", "spin", "bet_red", "bet_black", "bet_odd", "bet_even",
                   "bet_number", "bet_player", "bet_banker", "bet_tie", "call", "fold",
                   "reveal_river", "showdown"}
        actions = [a for a in result.get("valid_actions", []) if a in allowed]
        if not actions:
            raise IntegrationError("No supported action is available. Round retained; no additional wager.")
        hint = "\nBet selected. Choose Spin below to request the result." if data["game"] == "roulette" and actions == ["spin"] else ""
        if data['game'] == 'blackjack':
            hint += '\nSplit and Double each add one hand stake. Each hand plays in turn.'
        if result.get('notice'):
            hint += '\n' + str(result['notice'])
        selected = await ask(summary(data["game"], result) + hint +
                             "\nChoose a move, or quit to end this game and return to coding. Quit forfeits the unfinished stake; no refund or resume.",
                             choice_model("RoundAction", [*actions, "quit"]))
        if selected is None:
            return stopped()
        if selected.choice == "quit":
            return await end_round()
        payload = {}
        if selected.choice == "bet_number":
            number = await ask("Choose a roulette number, 0 to 36, or quit to return to coding.", Number)
            if number is None:
                return stopped()
            if number.number == "quit":
                return await end_round()
            payload["number"] = int(number.number)
        await send("human_casino_action", {"round_id": result["round_id"], "action": selected.choice,
                   "payload": payload, "idempotency_key": "native-play-" + uuid.uuid4().hex})
    return stopped()


async def play_in_host(ctx, broker, *, ask_override=None, progress_override=None, requested_game=None,
                       present_override=None):
    capabilities = ctx.session.client_capabilities if ask_override is None else None
    if ask_override is None and (not capabilities or not capabilities.elicitation or capabilities.elicitation.form is None):
        return {"available": False, "message": "This host session does not support interactive game forms. "
                "Update/restart the host; no wager was placed. Do not answer forms on the user's behalf."}
    if ask_override is None and not ctx.session.can_send_request:
        return {"available": False, "message": "This transport cannot display interactive forms. "
                "Use the installed stdio integration in a fresh host session; no wager was placed."}
    from openvegas.client import APIError, OpenVegasClient
    client = OpenVegasClient()
    client.base_url = broker.backend
    client._http_client.follow_redirects = False
    progress_count = 0

    async def progress(message):
        nonlocal progress_count
        progress_count += 1
        if progress_override is not None:
            await notify(progress_override, message)
        if ctx is not None and hasattr(ctx, "report_progress"):
            async def report(text):
                await ctx.report_progress(progress_count, message=text)
            await notify(report, message)

    try:
        await progress("Checking the game account. No new wager has been placed.")
        responses = []

        async def ask(message, schema):
            final = schema.__name__ == "RoundResult"
            hint = ("Round settled. Choose Done to return to coding; this result closes in 30 seconds."
                    if final else "Waiting for your game form response, not the server. "
                    "Choose an option or quit / Esc to return to coding. Inactive forms close after 3 minutes.")
            if "Choose Spin below" in message:
                hint = "Bet selected. Waiting for you to choose Spin, or quit / Esc to return to coding."
            await progress(hint)
            try:
                async with asyncio.timeout(RESULT_TIMEOUT_SECONDS if final else FORM_TIMEOUT_SECONDS):
                    if ask_override is not None:
                        return await ask_override(message, schema)
                    answer = await ctx.elicit(message=message, schema=schema)
                    responses.append(answer.action)
                    return answer.data if answer.action == "accept" else None
            except TimeoutError:
                if final:
                    return None  # The already-settled result still returns to the host.
                raise IntegrationError("No game-form response was received within 3 minutes. "
                    "Back to coding. If no form was visible, restart the host and reopen OpenVegas play. "
                    "Any existing wager remains recorded, not refunded; no automatic action or retry.") from None

        from .game_browser import browser_action, signed_out_menu, use_game_login
        use_game_login(client,broker.backend)
        try:
            account=await menu_read(client._request('GET','/integrations/account'))
        except APIError as exc:
            if exc.status not in {401,403}:raise
            account=await signed_out_menu(ask,client,broker)
            if account is None:
                return {'status':'cancelled','message':'Back to coding. No game or wager started.'}
        integrated=await menu_read(broker.account())
        if not account.get('user_id') or account['user_id']!=integrated['user_id']:
            raise IntegrationError('CLI and integration accounts differ. Sign in to the same account before playing.')
        identity=hashlib.sha256((broker.backend+'\n'+account['user_id']).encode()).hexdigest()
        async def browser_control(action):
            return await browser_action(action,ask,client,broker)
        with _locked(profile() / "native-play" / identity) as directory:
            result = await run_round(client, ask, Journal(directory), account, progress=progress,
                                     requested_game=requested_game, present=present_override, wager_on_submit=True,
                                     show_lobby=True,browser_action=browser_control)
        if result["status"] == "resolved":
            # Settlement is durable before this display-only acknowledgement.
            # Closing/failing this form cannot retry, refund or repeat the wager.
            try:
                await ask(result["table"] + "\nDone or Esc returns to coding. No repeat wager.",
                          choice_model("RoundResult", ["done", "quit"]))
            except Exception:
                pass  # The settled table is still included in the tool result.
        if result["status"] == "cancelled" and responses and responses[0] != "accept":
            return {**result, "status": "form_not_completed", "form_response": responses[0],
                    "message": "The host returned " + responses[0] + " without a game selection. "
                    "No round was started. If you did not dismiss a visible form, the host may have "
                    "rejected it before display; do not report that the menu opened or that the user quit. "
                    "No automatic retry."}
        return result
    except IntegrationError:
        raise
    except Exception:
        return {"available": False, "message": "Play stopped safely. Check your full CLI login and game service. "
                "Cosmetic grants cannot wager. If a round was started, its recovery record is retained; "
                "OpenVegas play asks before retrying. No automatic new wager or refund."}
    finally:
        await client._http_client.aclose()
