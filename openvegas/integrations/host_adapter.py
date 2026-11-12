"""Version-pinned launch descriptions, not proof of native policy equivalence.

Keep the original invocation immutable. No credentials or transcripts belong in
this record; authentication is an opaque reference to the original environment.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .credentials import IntegrationError

ADAPTER_VERSION = 'native-fixes-20261104.1'
PINNED_VERSIONS = {'codex': '0.166.0', 'claude': '2.1.301'}


def require_version(host, version):
    if PINNED_VERSIONS.get(host) != version:
        raise IntegrationError('Unsupported host adapter version; host billing unchanged.')


@dataclass(frozen=True)
class LaunchOption:
    category: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class HostLaunchSpec:
    host: str
    executable: str = field(repr=False)
    version: str
    original_argv: tuple[str, ...] = field(repr=False)
    cwd: str = field(repr=False)
    provider: str | None
    auth_reference: str = 'original-process-environment'
    options: tuple[LaunchOption, ...] = field(init=False, repr=False)
    _resume_prefix: tuple[str, ...] = field(init=False, repr=False)
    _resume_error: str | None = field(init=False, repr=False)

    def __post_init__(self):
        object.__setattr__(self, 'original_argv', tuple(self.original_argv))
        object.__setattr__(self, 'options', classify_original(self.host, self.original_argv))
        from .billing_host import resume_args
        try:
            template = resume_args(self.host, self.original_argv, '00000000-0000-0000-0000-000000000000')
            object.__setattr__(self, '_resume_prefix', tuple(template[:-2]))
            object.__setattr__(self, '_resume_error', None)
        except IntegrationError as exc:
            # An ordinary original launch may contain a task. It can run, but
            # cannot later be reconstructed as a prompt-free resume.
            object.__setattr__(self, '_resume_prefix', ())
            object.__setattr__(self, '_resume_error', str(exc))

    def value(self, *names):
        values = [option.argv[-1] if len(option.argv) == 2 else option.argv[0].partition('=')[2]
                  for option in self.options if option.argv[0].partition('=')[0] in names]
        return values[-1] if values else None

    @property
    def model(self):
        return self.value('--model', '-m')

    @property
    def effort(self):
        return self.value('--effort')

    @property
    def policy_inputs(self):
        return tuple(option for option in self.options if option.category == 'policy')

    def original_resume(self, identifier, model=None):
        args = self.resume_arguments(identifier)
        selected = model or self.model
        return [self.executable, *args, *(['--model', selected] if selected else [])]

    def managed_codex_client(self, env, *, restore_id=None, restored_model=None):
        """Admit only original argv that can be attached without moving policy flags."""
        from .billing_host import session_id
        from .native import _MODEL
        require_version(self.host, self.version)
        if (self.host != 'codex' or env.get('OPENVEGAS_CODEX_MANAGED_HANDOFF', '1') != '1'
                or self.provider != 'openai' or self.cwd != os.getcwd()
                or env.get('OPENVEGAS_NATIVE_TOKEN')):
            raise IntegrationError('Same-chat Codex handoff needs an opted-in, original host-billed '
                                   'managed session. Stay on host or start a fresh ov codex --wallet.')
        client, seen = [], set()
        args = iter(self.original_argv)
        for arg in args:
            key, equal, value = arg.partition('=')
            if key in ('--model', '-m'):
                value = value if equal else next(args, '')
                if 'model' not in seen and not (key == '-m' and equal) and _MODEL.fullmatch(value):
                    seen.add('model')
                    client.extend(('--model', value))
                    continue
            if arg in ('--no-daemon', '--no-alt-screen') and arg not in seen:
                seen.add(arg)
                if arg != '--no-daemon':
                    client.append(arg)
                continue
            if arg == 'resume' and arg not in seen:
                identifier = next(args, '')
                if session_id(identifier):
                    seen.add(arg)
                    client.extend((arg, identifier))
                    continue
            raise IntegrationError('This invocation cannot be transferred to the managed Codex server; '
                                   'original argv and permissions are unchanged. Use a fresh ov codex --wallet.')
        if restore_id is not None:
            if not session_id(restore_id) or (restored_model is not None and not _MODEL.fullmatch(restored_model)):
                raise IntegrationError('Invalid verified Codex restore identity or model.')
            model = restored_model or self.model
            return [*(['--no-alt-screen'] if '--no-alt-screen' in seen else []), 'resume', restore_id,
                    *(['--model', model] if model else [])]
        return client

    def resume_arguments(self, identifier):
        from .billing_host import session_id
        if self._resume_error:
            raise IntegrationError(self._resume_error)
        if not session_id(identifier):
            raise IntegrationError('Native conversation ID is unavailable; billing unchanged.')
        return [*self._resume_prefix, 'resume' if self.host == 'codex' else '--resume', identifier]

    def require_switch_support(self):
        require_version(self.host, self.version)
        # Neither SessionStart nor UserPromptSubmit proves that background tools
        # and permission requests have quiesced. Do not replace that evidence
        # with a delay or a caller-supplied "idle" boolean.
        raise IntegrationError(
            'Safe native turn/tool/approval boundary is not verified for this adapter; '
            'billing unchanged. Finish or cancel work in the native host and restart '
            'explicitly; no prompt was replayed.')

    def require_exit_resume(self, identifier, env):
        """A direct native restart, never a transfer of policy to a remote server."""
        require_version(self.host, self.version)
        args = self.resume_arguments(identifier)
        if self.host != 'claude':
            raise IntegrationError('Codex wallet resume uses a separate app-server; the original effective '
                                   'permissions and server-hook ownership cannot be verified for this launch. '
                                   'Stay on host billing or start a fresh ov codex --wallet session.')
        if self.cwd != os.getcwd():
            raise IntegrationError('Native working directory changed; resume directly in the original directory.')
        # Only syntax we can replay without reinterpreting options or changing
        # the loaded policy/configuration sources. Native still enforces its rules.
        parts = iter(self._resume_prefix)
        tools = False
        for part in parts:
            key, equal, value = part.partition('=')
            if key == '--tools' and not tools:
                value = value if equal else next(parts, '')
                if value and set(value.split(',')) <= {'Read', 'Glob', 'Grep', 'Edit', 'Write'}:
                    tools = True
                    continue
            if key in {'--restricted', '--strict-mcp-config', '--no-chrome'} and not equal:
                continue
            if key in {'--permission-mode', '--disallowedTools', '--disallowed-tools', '--effort'}:
                value = value if equal else next(parts, '')
                if value and (key != '--permission-mode' or value in {'manual', 'plan', 'dontAsk'}):
                    continue
            raise IntegrationError('This invocation has an unverified permission or launch option. '
                                   'Keep using host billing; no option was removed.')
        if not tools or '--strict-mcp-config' not in self._resume_prefix:
            raise IntegrationError('OpenVegas cannot inventory arbitrary background work or verify pending '
                                   'approvals and permissions. Keep this session on its current billing route. '
                                   'For a separate personal file-tools session, start '
                                   'claude --tools Read,Glob,Grep,Edit,Write --strict-mcp-config. '
                                   'These restrictions must be present from the original launch.')
        if (not env.get('HOME') or any(k in env for k in (
                'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_SIMPLE', 'OPENVEGAS_NATIVE_TOKEN'))
                or any(k.startswith(('CLAUDE_CODE_USE_', 'CLAUDE_CODE_MANAGED_', 'CLAUDE_CODE_POLICY_'))
                       for k in env)):
            raise IntegrationError('Custom routing or policy configuration is not supported for exit handoff. '
                                   'Keep using the original native host.')
        profile = Path(env.get('CLAUDE_CONFIG_DIR', str(Path(env['HOME']) / '.claude')))
        policy = (profile / 'remote-settings.json', profile / 'managed-settings.json',
                  Path('/Library/Application Support/ClaudeCode/managed-settings.json'),
                  Path('/Library/Application Support/ClaudeCode/managed-settings.d'),
                  Path('/Library/Managed Preferences/com.anthropic.claudecode.plist'),
                  Path('/etc/claude-code/managed-settings.json'), Path('/etc/claude-code/managed-settings.d'))
        for path in policy:
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise IntegrationError('Cannot inspect native policy sources. Keep using original host billing.') from None
            raise IntegrationError('Managed settings detected; account-dependent policy cannot be transferred. '
                                   'Resume directly on the original host billing route.')
        return args

    def verify_direct_environment(self, original, target):
        # Check actual launcher output, not just its requested argv. Never copy
        # a subscription credential into the wallet environment to fix a mismatch.
        routing = {'CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_MAX_OUTPUT_TOKENS',
                   'ANTHROPIC_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
                   'ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_MODEL',
                   *('ANTHROPIC_DEFAULT_' + family + '_MODEL' for family in ('HAIKU', 'SONNET', 'OPUS', 'FABLE'))}
        for key in original.keys() | target.keys():
            if not key.startswith('OPENVEGAS_') and key not in routing and original.get(key) != target.get(key):
                raise IntegrationError('Native profile or policy environment differs. '
                                       'No wallet child started; resume on original host billing.')


def classify_original(host, args):
    """Snapshot syntax only; policy-name equality never asserts equivalence."""
    from .native import _CODEX_REMOTE_PERMISSION_OPTIONS, _VALUE_OPTIONS
    values = _VALUE_OPTIONS[host] | {'--model', '-m', '--effort'}
    policy = _CODEX_REMOTE_PERMISSION_OPTIONS | {
        '--permission-mode', '--allowedTools', '--allowed-tools', '--disallowedTools',
        '--disallowed-tools', '--tools', '--dangerously-skip-permissions', '--restricted',
        '--allow-dangerously-skip-permissions', '--mcp-config', '--strict-mcp-config',
    }
    options = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '--':
            options.append(LaunchOption('literal', tuple(args[index:])))
            break
        key = arg.partition('=')[0]
        parts = [arg]
        if key in values and '=' not in arg and index + 1 < len(args):
            index += 1
            parts.append(args[index])
        category = ('policy' if key in policy else
                    'model' if key in {'--model', '-m', '--effort'} else
                    'working-directory' if key in {'--cd', '-C'} else
                    'local-transport' if key == '--no-daemon' else
                    'presentation' if key in {'--no-alt-screen', '--color'} else 'opaque')
        options.append(LaunchOption(category, tuple(parts)))
        index += 1
    return tuple(options)


@dataclass(frozen=True)
class LocalServerLaunch:
    executable: str = field(repr=False)
    endpoint: str = field(repr=False)
    backend: str = field(repr=False)
    model: str
    local_tools_only: bool
    catalog_path: str | None = field(default=None, repr=False)

    def argv(self):
        config = {
            'model_provider': 'openai',
            'openai_base_url': self.backend + '/integrations/native/v1',
            'cli_auth_credentials_store': 'ephemeral',
            'model': self.model,
            'review_model': self.model,
        }
        if self.local_tools_only:
            config['web_search'] = 'disabled'
        if self.catalog_path is not None:
            config['model_catalog_json'] = self.catalog_path
        controls = [part for key, value in config.items() for part in ('-c', key + '=' + json.dumps(value))]
        return [self.executable, '--no-daemon', *controls, 'app-server', '--listen', self.endpoint]


def codex_remote_options(args):
    """Positive classification for the pinned remote TUI interface.

    Policy and routing overrides are rejected by native's existing guards.
    Unknown fields must not become remote flags merely because a denylist has
    not caught up with a new CLI version.
    """
    values = {'--cd', '-C', '--image', '-i', '--color'}
    switches = {'--no-alt-screen', '--last', '--all'}
    options, positional, literal = [], [], []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '--':
            literal = list(args[index:])
            break
        key, equal, value = arg.partition('=')
        if key in values:
            parts = [arg]
            if not equal:
                index += 1
                if index >= len(args):
                    raise IntegrationError('Incomplete native client option.')
                parts.append(args[index])
            elif not value:
                raise IntegrationError('Incomplete native client option.')
            category = 'working-directory' if key in {'--cd', '-C'} else 'presentation'
            options.append(LaunchOption(category, tuple(parts)))
        elif arg == '--no-daemon':
            options.append(LaunchOption('local-transport', (arg,)))
        elif arg in switches:
            options.append(LaunchOption('presentation', (arg,)))
        elif arg.startswith('-'):
            raise IntegrationError('Unsupported native remote option; original host arguments unchanged.')
        else:
            positional.append(arg)
            options.append(LaunchOption('client', (arg,)))
        index += 1
    if positional and positional[0] != 'resume':
        raise IntegrationError('Only native resume is supported for remote attach.')
    return tuple(options), tuple(literal)


def remote_attach(executable, endpoint, model, args, *, controls=()):
    options, literal = codex_remote_options(tuple(args))
    client = [part for option in options if option.category != 'local-transport'
              for part in option.argv]
    return [executable, *controls, *client, '--remote', endpoint, '--model', model, *literal]
