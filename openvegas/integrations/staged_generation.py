"""One-use process identity for a wallet server and its attached native UI."""
import secrets
from contextlib import contextmanager

from .credentials import IntegrationError
from .session_ownership import GENERATION_ENV, OWNER_ENV, fence_fields
from .use import SOCKET, TOKEN


def validate_control_environment(env):
    basic = {SOCKET, TOKEN}
    fenced = basic | {OWNER_ENV, GENERATION_ENV}
    if (not isinstance(env, dict) or not (set(env) <= basic or set(env) == fenced)
            or any(not isinstance(v, str) or '\x00' in v for v in env.values())):
        raise IntegrationError('Invalid local billing control connection.')
    if OWNER_ENV in env:
        if not all(env.values()) or len(env[GENERATION_ENV]) > 19:
            raise IntegrationError('Invalid fenced billing control connection.')
        fence_fields(env)


class _Stage:
    def __init__(self, supervisor):
        self.generation = supervisor.generation + 1
        self._env = {SOCKET: supervisor.env[SOCKET], TOKEN: secrets.token_urlsafe(32),
                     OWNER_ENV: supervisor.relay_owner, GENERATION_ENV: str(self.generation)}
        validate_control_environment(self._env)
        self._used = False

    def environment(self):
        if self._used:
            raise IntegrationError('Native launch generation is no longer available.')
        return dict(self._env)

    def consume(self, supervisor, env):
        if self._used or getattr(supervisor, '_wallet_stage', None) is not self:
            raise IntegrationError('Native launch generation is no longer available.')
        self._used = True
        if (supervisor.child is None or supervisor.child.returncode != 0
                or supervisor.generation + 1 != self.generation
                or self._env[SOCKET] != supervisor.env.get(SOCKET)
                or self._env[OWNER_ENV] != supervisor.relay_owner
                or any(env.get(k) != v for k, v in self._env.items())):
            raise IntegrationError('Native launch generation is stale or mismatched.')
        return self._env[TOKEN]


@contextmanager
def stage_after_exit(supervisor):
    if (supervisor.child is None or supervisor.child.returncode != 0
            or supervisor.runtime_lock.locked() or getattr(supervisor, '_wallet_stage', None) is not None):
        raise IntegrationError('A cleanly exited native generation is required before staging.')
    stage = supervisor._wallet_stage = _Stage(supervisor)
    try:
        yield stage
    finally:
        # A server may already have observed this generation even if attach failed.
        supervisor.generation = max(supervisor.generation, stage.generation)
        if supervisor.token == stage._env[TOKEN]:
            supervisor.env[TOKEN] = supervisor.token = secrets.token_urlsafe(32)
        if supervisor.env.get(GENERATION_ENV) == str(stage.generation):
            supervisor.env.pop(OWNER_ENV, None)
            supervisor.env.pop(GENERATION_ENV, None)
        stage._used = True
        stage._env.clear()
        supervisor._wallet_stage = None
