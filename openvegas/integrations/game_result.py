"""Readable game receipts; the ledger payload remains separate and unchanged."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def net_outcome(result):
    """Use settled net, not the winning side or gross payout, to classify a bet."""
    if result.get('status', result.get('current_state')) != 'resolved' or result.get('error'):
        return None
    try:
        net = Decimal(str(result.get('net_v')))
        if not net.is_finite() or abs(net) > Decimal('1e15'):
            return None
        outcome = result.get('outcome')
        if net > 0 and isinstance(outcome, dict) and outcome.get('result') in {'forfeit', 'folded'}:
            return None
        return 'win' if net > 0 else 'loss' if net < 0 else 'push'
    except (InvalidOperation, ValueError, TypeError):
        return None


def amount(value, *, signed=False):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) > Decimal('1e15'):
            return "unavailable"
        number = number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if not number:
            number = abs(number)
        return '$V ' + ('+' if signed and number > 0 else '') + format(number, ',.2f')
    except (InvalidOperation, ValueError, TypeError):
        return "unavailable"


def receipt(result):
    status = result.get('status')
    if status == 'resolved':
        outcome = result.get('outcome')
        code = outcome.get('result') if isinstance(outcome, dict) else None
        verdict = net_outcome(result)
        label = {'win': 'You win.', 'loss': 'You lost this round.', 'push': 'Push.'}.get(verdict, 'Round settled.')
        if verdict == 'loss' and code in {'dealer_wins', 'forfeit', 'folded'}:
            label = {'dealer_wins': 'Dealer wins.', 'forfeit': 'Round forfeited.', 'folded': 'Round folded.'}[code]
        text = (label + '\nPayout: ' + amount(result.get('payout_v')) +
                ' | Net: ' + amount(result.get('net_v'), signed=True) +
                '\nBack to coding. No new wager placed.')
    else:
        message = result.get('message')
        text = message if isinstance(message, str) and message.strip() else 'Game request finished. Back to coding.'
    warning = result.get('display_warning')
    if isinstance(warning, str) and warning.strip():
        text += '\nDisplay warning: ' + warning
    # No terminal controls from transport errors or server messages.
    return ''.join(c for c in text if c in '\n\t' or (ord(c) >= 32 and not 127 <= ord(c) <= 159))[:6000]
