"""Adapt the existing quoted horse wager to the shared human-input game flow."""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal, InvalidOperation

from openvegas.client import APIError

from .credentials import IntegrationError
from .game_result import amount
from .game_table import label, table


def money(value):
    try:
        number = Decimal(str(value))
        if number.is_finite() and abs(number) <= Decimal('1e15'):
            return number
    except (InvalidOperation, ValueError, TypeError):
        pass
    raise IntegrationError('Invalid horse pricing response. No automatic wager or retry.')


def public_race(raw):
    """Only bounded display fields cross to the terminal; no seeds or quote secrets."""
    result = {key: raw[key] for key in ('bet_type', 'selected_horse', 'bet_horse') if key in raw}
    horses = []
    for horse in raw.get('horses', [])[:8]:
        number = horse.get('number')
        if type(number) is int and 1 <= number <= 8:
            horses.append({'number': number, 'name': label(horse.get('name'), 22)})
    result['horses'] = horses
    result['finish_order_nums'] = [n for n in raw.get('finish_order_nums', [])[:8]
                                   if type(n) is int and 1 <= n <= 8]
    result['winner'] = label(raw.get('winner', ''), 22)
    checkpoints = []
    for checkpoint in raw.get('checkpoints', [])[:200]:
        if not isinstance(checkpoint, dict):
            continue
        row = {}
        for horse in horses:
            number = horse['number']
            value = checkpoint.get(str(number), checkpoint.get(number))
            try:
                position = money(value)
            except IntegrationError:
                continue
            if 0 <= position <= 1000:
                row[str(number)] = float(position)
        if len(row) == len(horses) and row:
            checkpoints.append(row)
    result['checkpoints'] = checkpoints
    return result


async def dispatch_horse(client, journal, *, present=None, progress=None):
    from .native_play import ACTION_TIMEOUT_SECONDS, notify

    operation = journal.data.get('pending', {})
    if operation.get('method') != 'play_horse_quote':
        raise IntegrationError('Invalid saved horse operation; no wager sent.')
    await notify(progress, f'Sending your chosen horse. Server response limit: {ACTION_TIMEOUT_SECONDS} seconds.')
    try:
        async with asyncio.timeout(ACTION_TIMEOUT_SECONDS):
            raw = await client.play_horse_quote(**operation['kwargs'])
    except APIError as exc:
        if exc.status == 409 and exc.data.get('error') == 'quote_expired':
            # The server persists this rejection before any debit. A new quote
            # must still be displayed and explicitly selected, never auto-bet.
            journal.data = {}
            journal.save()
            return None
        rejected = {
            (400, 'insufficient_balance'): 'Your balance changed. Choose a smaller budget in OpenVegas play.',
            (400, 'invalid_horse_selection'): 'That horse is no longer selectable. OpenVegas play opens a fresh board.',
            (409, 'quote_position_unselectable'): 'That horse is unavailable at this budget. OpenVegas play opens a fresh board.',
            (409, 'quote_integrity_error'): 'The horse quote could not be verified. OpenVegas play opens a fresh board.',
            (404, 'quote_not_found'): 'The horse quote is no longer available. OpenVegas play opens a fresh board.',
        }
        if message := rejected.get((exc.status, exc.data.get('error'))):
            # Exact persisted pre-debit rejections must not trap future games in
            # recovery. Unknown errors, including consumed quotes, stay pending.
            journal.data = {}
            journal.save()
            return {'status': 'unavailable', 'round_id': None,
                    'message': message + ' No horse wager placed; back to coding.'}
        raise
    except TimeoutError:
        raise IntegrationError('Horse result did not arrive in time. Its original request is saved; '
                               'no automatic retry or new wager. OpenVegas play offers recovery.') from None
    debit, payout, net = (money(raw.get(key)) for key in ('bet_amount', 'payout', 'net'))
    if (raw.get('error') or not raw.get('game_id') or raw.get('demo_mode')
            or debit != money(journal.data['wager']) or payout < 0 or net != payout - debit
            or payout not in {Decimal(0), money(journal.data['payout_if_hit'])}):
        raise IntegrationError('Horse settlement could not be verified. Recovery retained; no new wager.')
    result = {'current_state': 'resolved', 'round_id': str(raw['game_id']),
              'payout_v': str(payout), 'net_v': str(net), 'outcome': public_race(raw.get('outcome_data') or {})}
    journal.data.update(stage='horse_settled', result=result)
    journal.data.pop('pending')
    journal.save()
    if present:
        await present('horse', result)
    return {**result, 'status': 'resolved', 'table': table('horse', result),
            'message': 'Race settled by the existing ledger. Back to coding. No repeat wager.'}


async def horse_round(client, ask, journal, account, budget, *, present=None, progress=None):
    from .native_play import choice_model, menu_read

    cancelled = {'status': 'cancelled', 'round_id': None, 'message': 'Back to coding. No horse wager placed.'}
    bet = await ask('Horse Racing / Choose a finish: win (1st), place (top 2), show (top 3).\n'
                    'Quit returns to coding. No wager placed yet.',
                    choice_model('HorseBet', ['win', 'place', 'show', 'quit']))
    if bet is None or bet.choice == 'quit':
        return cancelled
    notice = ''
    while True:
        quote = await menu_read(client.create_horse_quote(
            bet_type=bet.choice, budget_v=str(budget), idempotency_key='native-horse-quote-' + uuid.uuid4().hex))
        if not quote.get('quote_id'):
            raise IntegrationError('Horse quote unavailable. No wager placed.')
        choices, rows = {}, []
        for row in quote.get('horses', [])[:8]:
            number = row.get('number')
            if type(number) is not int or not 1 <= number <= 8 or row.get('selectable') is not True:
                continue
            debit, payout = money(row.get('debit_v')), money(row.get('payout_if_hit_v'))
            if not 0 < debit <= budget or debit > money(account['balance']) or payout < 0:
                raise IntegrationError('Horse price exceeds your budget or is invalid. No wager placed.')
            text = f"{number}. {label(row.get('name'), 22)} / {amount(debit)} / pays {amount(payout)}"
            choices[text] = (number, debit, payout)
            rows.append({'number': number, 'name': label(row.get('name'), 22)})
        if not choices:
            return {**cancelled, 'message': 'No horses available within that budget. No wager placed; back to coding.'}
        if present:
            await present('horse', {'current_state': 'awaiting_action', 'state': {'horses': rows}})
        selected = await ask(notice + f'Budget: {amount(budget)}. Choose a horse to place its displayed bet.\n'
                             'You can lose the stake. Quit spends nothing.',
                             choice_model('HorseChoice', [*choices, 'quit']))
        if selected is None or selected.choice == 'quit':
            return cancelled
        number, debit, payout = choices[selected.choice]
        journal.data = {'user_id': account['user_id'], 'game': 'horse', 'wager': str(debit),
                        'payout_if_hit': str(payout), 'stage': 'horse_pending',
                        'pending': {'method': 'play_horse_quote', 'kwargs': {
                            'quote_id': quote['quote_id'], 'horse': number,
                            'idempotency_key': 'native-horse-play-' + uuid.uuid4().hex}}}
        journal.save()
        result = await dispatch_horse(client, journal, present=present, progress=progress)
        if result is not None:
            return result
        notice = 'Quote expired without a wager. Choose again at the refreshed price.\n'
