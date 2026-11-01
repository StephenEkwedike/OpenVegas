"""Explicit, process-scoped $V launch. Register ``native`` on the existing CLI.

The host still owns its agent loop, stdio, cwd, history and permissions. No
configuration or authentication file is written, and no subscription fallback
is installed. The server must enforce the quoted model, token cap and budget.
"""
from __future__ import annotations

from openvegas.integrations.game_result import amount as format_v

import asyncio
import json
import os
import re
from decimal import Decimal, InvalidOperation

import click

from .broker import Broker
from .credentials import IntegrationError, backend_url
from .installer import detect, host_environment

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}\Z")
_SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{1,200}\Z")
_LAUNCH_OPTIONS = {'--model': 1, '--limit-v': 1, '--max-output-tokens': 1,
                   '--yes': 0, '--dry-run': 0, '--help': 0, '--local-tools-only': 0}
_BLOCKED = {
    'claude': {'--model', '--fallback-model', '--settings', '--setting-sources', '--agents', '--agent',
               '--bare', '--cloud', '--environment', '--desktop', '--teleport', '--remote-control',
               '--remote-control-session-name-prefix', '--bg', '--background', '--max-budget-usd',
               '--plugin-dir', '--plugin-url'},
    'codex': {'--model', '--config', '--profile', '--oss', '--local-provider', '--remote',
              '--remote-auth-token-env', '--enable', '--disable', '-m', '-c', '-p'},
}
_COMMANDS = {
    'claude': {'agents', 'attach', 'auth', 'auto-mode', 'doctor', 'gateway', 'import', 'install',
               'logs', 'mcp', 'plugin', 'plugins', 'purge', 'respawn', 'rm', 'setup-token',
               'stop', 'kill', 'ultrareview', 'update', 'upgrade'},
    'codex': {'agents', 'exec', 'e', 'review', 'login', 'logout', 'mcp', 'plugin', 'mcp-server',
              'app-server', 'remote-control', 'app', 'completion', 'update', 'doctor', 'sandbox',
              'debug', 'apply', 'a', 'resume', 'queue', 'archive', 'delete', 'migrate-rollouts',
              'unarchive', 'fork', 'cloud', 'exec-server', 'features', 'help'},
}
_ALLOWED_COMMANDS = {'resume', 'fork', 'exec', 'e', 'review'}
_VALUE_OPTIONS = {
    'claude': {'--resume', '-r', '--name', '-n', '--permission-mode', '--effort', '--add-dir',
               '--session-id', '--output-format', '--input-format', '--system-prompt',
               '--append-system-prompt', '--system-prompt-file', '--append-system-prompt-file',
               '--allowedTools', '--allowed-tools', '--disallowedTools', '--disallowed-tools',
               '--tools', '--mcp-config', '--json-schema'},
    'codex': {'--cd', '-C', '--add-dir', '--sandbox', '-s', '--ask-for-approval', '-a',
              '--image', '-i', '--output-last-message', '-o', '--output-schema', '--color'},
}
_SERVER_SECRET_PREFIXES = (
    'STRIPE_', 'SUPABASE_', 'DATABASE_', 'DB_', 'REDIS_', 'POSTGRES_', 'PG',
    'MYSQL_', 'MONGO_', 'UPSTASH_', 'NEON_', 'SMTP_', 'RESEND_', 'SENDGRID_',
    'MAILGUN_', 'TWILIO_', 'JWT_', 'CRYPTO_', 'PRIMARY_CRYPTO', 'ENCRYPTION_',
    'FERNET_', 'BACKEND_', 'SERVER_', 'MISTRAL_', 'GEMINI_', 'GROQ_', 'XAI_',
    'COHERE_', 'DEEPSEEK_', 'TOGETHER_', 'FIREWORKS_', 'PERPLEXITY_', 'REPLICATE_',
    'HUGGINGFACE_', 'HUGGING_FACE_', 'AWS_', 'AZURE_', 'GOOGLE_', 'GCP_',
    'CLOUDFLARE_', 'S3_', 'R2_', 'VAULT_',
)
_SERVER_SECRET_NAMES = {
    'SECRET_KEY', 'JWTSECRET', 'JWTSECRETKEY', 'ACCESS_TOKEN', 'AUTH_TOKEN', 'BEARER_TOKEN',
    'API_KEY', 'ENCRYPTION_KEY', 'SIGNING_KEY', 'PRIVATE_KEY', 'MASTER_KEY', 'FERNET_KEY',
    'HF_TOKEN', 'SENTRY_AUTH_TOKEN', 'SERVICE_ROLE_KEY', 'SERVICE_KEY', 'SESSION_KEY',
}


def _server_secret(key):
    key = key.upper()
    return (key.startswith(_SERVER_SECRET_PREFIXES) or key in _SERVER_SECRET_NAMES
            or any(part in key for part in ('SECRET', 'PASSWORD', 'PRIVATE_KEY', 'ENCRYPTION_KEY',
                                            'CRYPTO_KEY', 'SIGNING_KEY', 'API_KEY')))


class Budget(click.ParamType):
    name = 'decimal'

    def convert(self, value, param, ctx):
        try:
            if isinstance(value, bool) or len(str(value)) > 64:
                raise ValueError
            result = Decimal(str(value))
            if not result.is_finite() or result <= 0 or abs(result.as_tuple().exponent) > 100:
                raise ValueError
            return result
        except (ValueError, InvalidOperation):
            self.fail('must be a positive, finite Decimal budget', param, ctx)


class NativeCommand(click.Command):
    """Launcher options end at the first host argument or explicit ``--``.

    Click must never reinterpret a trailing host option as spending consent or
    silently accept a second --model/--limit-v with last-value-wins behavior.
    """
    def parse_args(self, ctx, args):
        prefix, seen, host_seen, index = [], set(), hasattr(self, 'fixed_host'), 0
        while index < len(args):
            arg = args[index]
            if arg == '--':
                index += 1
                break
            key, equal, _ = arg.partition('=')
            if key in _LAUNCH_OPTIONS:
                if key in seen:
                    raise click.UsageError('Repeated launcher options are ambiguous.', ctx)
                seen.add(key)
                prefix.append(arg)
                if _LAUNCH_OPTIONS[key] and not equal:
                    index += 1
                    if index >= len(args) or args[index].startswith('--'):
                        raise click.UsageError('Missing launcher option value.', ctx)
                    prefix.append(args[index])
                index += 1
            elif not host_seen and not arg.startswith('-'):
                prefix.append(arg)
                host_seen = True
                index += 1
            else:
                break
        return super().parse_args(ctx, prefix + ['--'] + args[index:])


def validate_host_args(host, args):
    """Return the native subcommand index, while leaving every safe argument intact."""
    if any(not isinstance(arg, str) or '\x00' in arg for arg in args):
        raise IntegrationError('Invalid native host argument.')
    command_index, first_position, index = None, True, 0
    while index < len(args):
        arg = args[index]
        if not isinstance(arg, str) or '\x00' in arg:
            raise IntegrationError('Invalid native host argument.')
        if arg == '--':
            break  # The host itself treats everything after this as literal input.
        key = arg.partition('=')[0]
        blocked = _BLOCKED[host] | {'--limit-v', '--max-output-tokens', '--yes', '--dry-run'}
        if key in blocked or (key.startswith('--') and any(
                candidate.startswith(key) for candidate in blocked if candidate.startswith('--'))):
            raise IntegrationError('Host argument could change the consented model, billing route or credentials. '
                                   'Use launcher options before host arguments; routing overrides are not allowed.')
        if arg.startswith('-') and not arg.startswith('--') and len(arg) > 2:
            raise IntegrationError('Combined or attached short host options are ambiguous; use separate options and values.')
        if key in _VALUE_OPTIONS[host] and '=' not in arg:
            # Claude --resume may omit its value to open the picker.
            if index + 1 >= len(args) or args[index + 1].startswith('-'):
                if host == 'claude' and key in {'--resume', '-r'}:
                    index += 1
                    continue
                raise IntegrationError('Missing native host option value.')
            index += 2
            continue
        if not arg.startswith('-') and first_position:
            first_position = False
            if arg in _COMMANDS[host]:
                if host != 'codex' or arg not in _ALLOWED_COMMANDS:
                    raise IntegrationError('This command does not launch a foreground native inference session.')
                command_index = index
        index += 1
    return command_index


def _decimal(value, *, allow_zero=False):
    try:
        if isinstance(value, bool) or len(str(value)) > 64:
            raise ValueError
        result = Decimal(str(value))
        if (not result.is_finite() or result < 0 or (result == 0 and not allow_zero)
                or abs(result.as_tuple().exponent) > 100):
            raise ValueError
        return result
    except (ValueError, InvalidOperation):
        raise IntegrationError('Native pricing/budget response is invalid; no host was launched.') from None


def _pricing(value):
    if not isinstance(value, dict) or not isinstance(value.get('fingerprint'), str) or not _SAFE_ID.fullmatch(value['fingerprint']):
        raise IntegrationError('Native pricing response is invalid; no host was launched.')
    return value['fingerprint'], _decimal(value.get('v_per_usd')), _decimal(value.get('multiplier'))


def child_environment(host, backend, model, max_tokens, token):
    """Use the original native home; strip stale provider routing and local keys."""
    env = dict(host_environment() or os.environ)
    local_connections = {key: os.environ[key] for key in (
        'OPENVEGAS_TERMINAL_SOCKET', 'OPENVEGAS_TERMINAL_TOKEN',
        'OPENVEGAS_SWITCH_SOCKET', 'OPENVEGAS_SWITCH_TOKEN',
    ) if key in os.environ}
    prefixes = ('ANTHROPIC_', 'OPENAI_', 'OPENROUTER_', 'OPENVEGAS_', 'AZURE_OPENAI_',
                'CLAUDE_CODE_OAUTH_', 'CLAUDE_CODE_API_KEY_', 'CLAUDE_CODE_SUBAGENT_',
                'CLAUDE_CODE_MAX_OUTPUT_')
    routes = {'CLAUDE_CODE_USE_' + name for name in (
        'ANTHROPIC_AWS', 'ANTHROPIC_GOOGLE_CLOUD', 'BEDROCK', 'CCR_V', 'FOUNDRY',
        'GATEWAY', 'MANTLE', 'VERTEX')}
    for key in list(env):
        if _server_secret(key) or key.startswith(prefixes) or key in routes or key in {'CODEX_API_KEY', 'CODEX_BASE_URL', 'CODEX_MODEL',
                                             'CODEX_MODEL_PROVIDER', 'CLAUDE_CODE_SIMPLE'}:
            env.pop(key)
    if host == 'claude':
        env.update(ANTHROPIC_BASE_URL=backend + '/integrations/native',
                   ANTHROPIC_API_KEY='', ANTHROPIC_AUTH_TOKEN=token,
                   ANTHROPIC_MODEL=model, ANTHROPIC_DEFAULT_MODEL=model,
                   CLAUDE_CODE_SUBAGENT_MODEL=model, CLAUDE_CODE_MAX_OUTPUT_TOKENS=str(max_tokens))
        # Pin only the actual family: mapping Fable/Opus to Haiku makes the host
        # infer unsupported adaptive-thinking capabilities from those aliases.
        for family in ('HAIKU', 'SONNET', 'OPUS', 'FABLE'):
            if 'claude-' + family.lower() + '-' in model:
                env['ANTHROPIC_DEFAULT_' + family + '_MODEL'] = model
    env['OPENVEGAS_NATIVE_TOKEN'] = token
    env.update(local_connections)
    return env


def host_argv(executable, host, backend, model, host_args, *, local_tools_only=False, hosted_web=False):
    index = validate_host_args(host, host_args)
    if host == 'claude':
        if model == 'anthropic/claude-haiku-4.5':
            # Native model identity lets Claude choose its own correct thinking
            # protocol. Only the outbound supplier ID is overridden; no history,
            # permission, credential or persisted configuration is rewritten.
            native_id = 'claude-haiku-4-5-20251001'
            return [executable, '--settings', json.dumps({'modelOverrides': {native_id: model}}),
                    '--model', native_id, *host_args]
        return [executable, '--model', model, *host_args]
    provider = {
        'name': 'OpenVegas $V', 'base_url': backend + '/integrations/native/v1',
        'env_key': 'OPENVEGAS_NATIVE_TOKEN', 'wire_api': 'responses', 'requires_openai_auth': False,
        'request_max_retries': 0, 'stream_max_retries': 0, 'supports_websockets': False,
    }
    # A complete table, not individual leaf merges with a stale provider's auth
    # settings. Dedicated flags take precedence without rewriting user config.
    table = '{' + ','.join(key + '=' + json.dumps(value) for key, value in provider.items()) + '}'
    controls = ['-c', 'model_provider="openvegas_v"', '-c', 'model_providers.openvegas_v=' + table,
                '-c', 'model=' + json.dumps(model), '-c', 'review_model=' + json.dumps(model)]
    if model == 'openai/gpt-4.1-nano':
        controls += ['-c', 'model_supports_reasoning_summaries=false', '-c', 'model_reasoning_summary="none"']
    if local_tools_only:
        if '--search' in host_args:
            raise IntegrationError('--search conflicts with explicit local-tools-only mode.')
        controls += ['-c', 'web_search="disabled"']
    elif hosted_web:
        controls += ['-c', 'web_search="live"']
    if index is None or host_args[index] != 'review':
        controls += ['--model', model]
    # Resume/exec have their own model flags. Put consented overrides there too,
    # not only on the root parser where an old saved model might win.
    at = index + 1 if index is not None else 0
    return [executable, *host_args[:at], *controls, *host_args[at:]]


async def run_host(argv, env):
    """Inherit all three streams and cwd. No PTY, shell, proxy loop or detachment."""
    process = await asyncio.create_subprocess_exec(*argv, env=env)
    try:
        code = await process.wait()
        return code if code >= 0 else 128 - code
    except asyncio.CancelledError:
        if process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(asyncio.shield(process.wait()), 3)
            except TimeoutError:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass
        raise


async def launch(host, model, limit_v, max_output_tokens=2048, *, yes=False, dry_run=False,
                 host_args=(), broker=None, emit=click.echo, confirm=None, local_tools_only=False,
                 provider='openrouter', runner=None):
    """Return the host exit code. Injection points are for bounded offline tests."""
    if host not in {'claude', 'codex'} or not isinstance(model, str) or not _MODEL.fullmatch(model):
        raise IntegrationError('Choose a supported host and an exact model identifier.')
    if provider not in {'openrouter', 'anthropic' if host == 'claude' else 'openai'}:
        raise IntegrationError('Provider must match the native host.')
    limit_v = _decimal(limit_v)
    if type(max_output_tokens) is not int or max_output_tokens < 1:
        raise IntegrationError('max-output-tokens must be a positive integer.')
    validate_host_args(host, host_args)
    if local_tools_only and host != 'codex':
        raise IntegrationError('--local-tools-only is the explicit Codex web-search compatibility option.')
    if local_tools_only and '--search' in host_args:
        raise IntegrationError('--search conflicts with explicit local-tools-only mode.')
    backend = broker.backend if broker is not None else backend_url()
    if dry_run:
        emit(f'Dry run: {host}, exact model {model}, budget ceiling {format_v(limit_v)}, output cap {max_output_tokens}.')
        emit('Route: ' + backend + '/integrations/native' + ('/v1/responses' if host == 'codex' else '/v1/messages'))
        emit('Pricing/markup: server quote required; no authentication, grant, host process or configuration changes.')
        emit('Native cwd, stdio, history and permissions are retained. Ordinary claude/codex billing is unchanged.')
        if host == 'codex':
            emit('Hosted web search: disabled for this invocation only.' if local_tools_only else
                 'Hosted web search requires an explicit reviewed server quote; otherwise use --local-tools-only.')
        return 0
    info = detect(host)
    if not info:
        raise IntegrationError('Native host executable/version unavailable. No spending session was opened.')
    owned_broker = broker is None
    broker = broker or Broker()
    session_token = None
    try:
        device = broker.credentials.load()
        if not isinstance(device, str) or not device.startswith('ov_device_'):
            raise IntegrationError('Existing device sign-in required. Run openvegas go; this command never signs in implicitly.')
        selection = {'host': host, 'model': model, 'max_tokens': max_output_tokens}
        if provider != 'openrouter':
            selection['provider'] = provider
        quote = await broker.request('POST', '/native/quote', token=device, data=selection)
        if not isinstance(quote, dict) or any(quote.get(key) != value for key, value in selection.items()):
            raise IntegrationError('Native quote changed the requested model or token cap; no host was launched.')
        fingerprint, rate, multiplier = _pricing(quote.get('pricing'))
        pricing = quote['pricing']
        web = pricing.get('native_web')
        if host == 'codex' and not local_tools_only and not web:
            raise IntegrationError('This model has no reviewed hosted-search quote. '
                                   'Use ordinary codex billing or explicitly add --local-tools-only.')
        unit_fields = ('supplier_input_usd_per_million', 'supplier_output_usd_per_million')
        unit_prices = None
        if any(field in pricing for field in unit_fields):
            unit_prices = [_decimal(pricing.get(field), allow_zero=True) for field in unit_fields]
        if 'v_per_supplier_usd' in pricing and _decimal(pricing['v_per_supplier_usd']) != rate * multiplier:
            raise IntegrationError('Native quote contains inconsistent conversion prices; no host was launched.')
        emit(f'OpenVegas $V inference: {host}; exact model {model}; output cap {max_output_tokens}.')
        emit(f'Session budget ceiling: {format_v(limit_v)}. Server pricing: {format_v(rate)}/USD x {multiplier} '
             f'(markup {(multiplier - 1) * 100}%).')
        if unit_prices is not None:
            incoming, outgoing = unit_prices
            emit(f'Quoted supplier USD per million tokens: input {incoming:f}; output {outgoing:f}.')
            emit(f'Converted retail $V per million tokens: input {format_v(incoming * rate * multiplier)}; '
                 f'output {format_v(outgoing * rate * multiplier)}.')
        else:
            emit('Model input/output unit prices were not supplied in this quote.')
        emit('This invocation spends OpenVegas credits, not subscription quota. Native tools, history, cwd and permissions remain with the host.')
        emit('Ordinary claude/codex launches are unchanged. No automatic subscription-exhaustion fallback or model switching.')
        media = pricing.get('native_media', {}).get('modalities', [])
        emit('Reviewed native attachments: ' + (', '.join(media) if media else 'none') +
             '. Computer/container tools and deferred tool search are not enabled on this route.')
        if web and not local_tools_only:
            from openvegas.gateway.native_pricing import web_fee
            try:
                fee = web_fee(pricing, 1)
            except ValueError:
                raise IntegrationError('Unreviewed hosted-search quote; no session opened.') from None
            emit(f'Hosted LIVE web search: at most one search per request; supplier US${fee} plus model tokens, '
                 f'with the same {multiplier}x markup. Two model rounds are reserved conservatively.')
        if local_tools_only:
            emit('Explicit local-tools-only mode: hosted web search is DISABLED for this invocation. Local tools and MCP remain host-owned.')
        if host == 'claude':
            emit('Saved subscription credentials are preserved; cached-subscription coexistence is not yet verified.')
        if not yes:
            accepted = confirm() if confirm else click.confirm('Authorize this quoted $V session?', default=False)
            if not accepted:
                emit('Spending not authorized; no session opened.')
                return 0
        result = await broker.request('POST', '/native/sessions', token=device, data={
            **selection, 'limit_v': format(limit_v, 'f'), 'confirmed': True, 'pricing_fingerprint': fingerprint})
        # Retain only a native-session bearer, even if other response fields
        # are malformed. Never forward or close a provider/device/host key.
        if isinstance(result, dict):
            token = result.get('token')
            if isinstance(token, str) and re.fullmatch(r'ov_native_[A-Za-z0-9_-]{16,128}', token):
                session_token = token
        if (session_token is None or not isinstance(result.get('session_id'), str)
                or not _SAFE_ID.fullmatch(result['session_id'])
                or type(result.get('expires_in')) is not int or result['expires_in'] <= 0
                or _decimal(result.get('budget_v')) != limit_v
                or _pricing(result.get('pricing')) != (fingerprint, rate, multiplier)
                or result['pricing'] != quote['pricing']):
            raise IntegrationError('Native session does not match the consented quote; no host was launched.')
        status = await broker.request('GET', '/native/session', token=session_token)
        if (not isinstance(status, dict) or status.get('active') is False
                or status.get('status') in {'closed', 'expired', 'revoked', 'exhausted'}
                or any(key in status and status[key] != value for key, value in selection.items())
                or ('session_id' in status and status['session_id'] != result['session_id'])):
            raise IntegrationError('Native spending session is not active or changed; no host was launched.')
        argv = host_argv(info['executable'], host, backend, model, list(host_args),
                         local_tools_only=local_tools_only, hosted_web=bool(web) and not local_tools_only)
        env = child_environment(host, backend, model, max_output_tokens, session_token)
        emit(f'Session authorized for up to {result["expires_in"]} seconds; closing it when the native host exits.')
        return await (runner or run_host)(argv, env)
    except (IntegrationError, click.Abort, asyncio.CancelledError):
        raise
    except Exception:  # noqa: BLE001 - Do not expose exceptions containing credentials.
        raise IntegrationError('Native launch failed; no credentials or host configuration were changed.') from None
    finally:
        if session_token:
            try:
                async with asyncio.timeout(5):
                    await broker.request('DELETE', '/native/session', token=session_token)
            except Exception:  # noqa: BLE001 - Cleanup must not hide the host's exit status.
                emit('Warning: session close could not be confirmed; its server budget and expiry still apply.')
        if owned_broker:
            try:
                await broker.close()
            except Exception:  # noqa: BLE001, S110 - Never print credential-bearing transport errors.
                pass


@click.command('native', cls=NativeCommand)
@click.argument('host', type=click.Choice(['claude', 'codex']))
@click.option('--model', help='Exact model; default: Claude Haiku 4.5 or OpenAI GPT-5.4 Nano. Server approval still required.')
@click.option('--limit-v', type=Budget(), help='Session ceiling; default: Claude 50 $V, Codex 10 $V. Confirm before spending.')
@click.option('--max-output-tokens', default=2048, show_default=True, type=click.IntRange(min=1))
@click.option('--yes', is_flag=True, help='Consent to the displayed server-priced spending session.')
@click.option('--dry-run', is_flag=True, help='Show the plan without authentication, processes or changes.')
@click.option('--local-tools-only', is_flag=True, help='Explicit Codex compatibility mode: disable hosted web search for this invocation only.')
@click.argument('host_args', nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def native(ctx, host, model, limit_v, max_output_tokens, yes, dry_run, local_tools_only, host_args):
    """Launch HOST with an explicit $V budget. Put native options after --.

    Native resume/continue, permissions and cwd flags pass through unchanged.
    Routing/model/config overrides require a new launch and spending consent.
    """
    try:
        if yes and (model is None or limit_v is None):
            raise IntegrationError('--yes requires explicit --model and --limit-v; omitted defaults require interactive consent.')
        if model is None:
            model = {'claude': 'anthropic/claude-haiku-4.5', 'codex': 'openai/gpt-5.4-nano'}[host]
        if limit_v is None:
            limit_v = '50' if host == 'claude' else '10'
        code = asyncio.run(launch(host, model, limit_v, max_output_tokens, yes=yes,
                                  dry_run=dry_run, host_args=host_args, local_tools_only=local_tools_only))
    except IntegrationError as exc:
        raise click.ClickException(str(exc)) from None
    except KeyboardInterrupt:
        ctx.exit(130)
    ctx.exit(code)


class HostCommand(NativeCommand):
    """Short entry point sharing the exact native parser and spending guard."""

    def __init__(self, host):
        import copy
        self.fixed_host = host
        super().__init__(host, callback=native.callback,
                         params=[copy.copy(p) for p in native.params if p.name != 'host'],
                         help=f'Launch {host} with a confirmed OpenVegas $V budget. Native options follow --.')

    def invoke(self, ctx):
        ctx.params['host'] = self.fixed_host
        return super().invoke(ctx)


if __name__ == '__main__':
    native()
