"""Classify a native invocation; do not rewrite the native argument vector."""
from .native import _COMMANDS, _VALUE_OPTIONS


def passthrough(host, args):
    commands = _COMMANDS[host] - ({'resume', 'fork'} if host == 'codex' else set())
    direct = {'--help', '-h', '--version', '-V'}
    values = _VALUE_OPTIONS[host] | {'--model'}
    if host == 'codex':
        values |= {'-p', '--profile', '-c', '--config', '-m', '--enable', '--disable',
                   '--local-provider', '--permissions'}
        direct |= {'--remote', '--remote-auth-token-env'}
    else:
        direct |= {'--print', '-p', '-v'}
        values |= {'--agent', '--settings', '--setting-sources', '--fallback-model'}

    skip = optional = False
    first_positional = True
    for arg in args:
        if skip:
            skip = False
            if not optional or not arg.startswith('-'):
                continue
        if arg == '--':
            break
        key, equals, _ = arg.partition('=')
        if key in direct:
            return True
        if key in values:
            skip = not equals
            optional = host == 'claude' and key in {'--resume', '-r'}
        elif not arg.startswith('-') and first_positional:
            if arg in commands:
                return True
            first_positional = False
    return False
