"""Pure, process-local native routing policy. Never reads profiles or credentials.

The caller authenticates/removes the local capability before planning and
owns request admission, retry identity, raw-body forwarding and response policy.
It MUST disable redirects and environment proxies on its upstream transports.
"""
from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

from .credentials import IntegrationError

VERSIONS = {"codex": "0.166.0", "claude": "2.1.322"}
LOCAL_AUTH_HEADER = "x-openvegas-local-auth"
LOCAL_AUTH_ENV = "OPENVEGAS_LOCAL_ROUTER_AUTH"
_HOST_BASES = {
    "codex": {"https://chatgpt.com/backend-api/codex", "https://api.openai.com/v1"},
    "claude": {"https://api.anthropic.com"},
}
_SAFE_HEADERS = frozenset({
    "accept", "accept-encoding", "content-type", "content-encoding", "user-agent",
    "anthropic-version", "anthropic-beta", "openai-beta", "originator",
    "x-codex-turn-state", "x-codex-turn-metadata", "x-client-request-id",
    "thread-id", "session-id", "x-codex-window-id", "x-codex-beta-features", "version",
    "x-claude-code-session-id", "x-claude-code-agent-id", "x-claude-code-parent-agent-id",
    "x-claude-code-request-class", "x-claude-code-agent-type", "x-claude-code-compaction",
    "x-claude-code-context-compacted", "x-stainless-retry-count", "x-stainless-timeout",
    "x-stainless-lang", "x-stainless-package-version", "x-stainless-os", "x-stainless-arch",
    "x-stainless-runtime", "x-stainless-runtime-version", "x-app",
})
_SINGLE_HEADERS = frozenset({
    "authorization", "x-api-key", "chatgpt-account-id", "cookie", "host",
    "content-length", "transfer-encoding", "upgrade", "sec-websocket-key",
    "sec-websocket-version", "sec-websocket-protocol",
})
_SECRET_HEADER = re.compile(r"auth|cookie|token|credential|secret|account|workspace|"
                            r"key|password|bearer|signature")
_CODEX_ROUTING = frozenset({
    "openai_base_url", "chatgpt_base_url", "model_provider", "model_providers", "profile",
    "profiles", "oss_provider", "model_catalog_json", "experimental_realtime_ws_base_url",
})


class AdapterError(IntegrationError):
    """Safe diagnostic: never include rejected headers, tokens, URLs or argv."""


def _require(condition, message="Unsupported native routing input; nothing was sent."):
    if not condition:
        raise AdapterError(message)


def _ascii(value, *, maximum=8192):
    return (isinstance(value, str) and 0 < len(value) <= maximum and value.isascii()
            and all(32 <= ord(char) < 127 for char in value))


def _base(value):
    _require(_ascii(value, maximum=2048) and not any(c in value for c in " \\%?#"))
    try:
        parsed = urlsplit(value)
        _require(parsed.scheme in {"https", "http"} and parsed.hostname
                 and not parsed.username and not parsed.password
                 and parsed.netloc == parsed.netloc.lower())
        _require(parsed.port is None or 1 <= parsed.port <= 65535)
        _require(parsed.scheme == "https" or parsed.hostname in {"127.0.0.1", "::1"})
        _require(not any(part in {".", ".."} for part in parsed.path.split("/")))
        _require("//" not in parsed.path)
        return parsed
    except ValueError:
        raise AdapterError("Unsupported native routing URL; nothing was sent.") from None


def _path(value):
    _require(_ascii(value) and value.startswith("/") and not value.startswith("//")
             and not any(c in value for c in " \\#"))
    path, separator, query = value.partition("?")
    _require(re.fullmatch(r"/[A-Za-z0-9_./-]+", path) is not None
             and "//" not in path and not any(p in {".", ".."} for p in path.split("/")))
    _require(not separator or query)
    try:
        params = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=16)
    except ValueError:
        raise AdapterError("Unsupported native query; nothing was sent.") from None
    _require(not re.search(r"%(?![0-9A-Fa-f]{2})", query))
    _require(all(_ascii(k, maximum=80) and _ascii(v, maximum=512) for k, v in params))
    _require(len({k for k, _ in params}) == len(params))
    return path, ("?" + query if separator else ""), dict(params)


def _headers(values):
    if isinstance(values, Mapping):
        values = list(values.items())
    _require(isinstance(values, Sequence) and not isinstance(values, (str, bytes))
             and len(values) <= 128)
    result, seen = [], set()
    for pair in values:
        _require(isinstance(pair, (tuple, list)) and len(pair) == 2)
        key, value = pair
        _require(isinstance(key, str) and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                 and len(key) <= 128 and _ascii(value, maximum=16384))
        key = key.lower()
        _require(key not in seen or key not in _SINGLE_HEADERS,
                 "Ambiguous native headers; nothing was sent.")
        seen.add(key)
        if key == "connection":
            _require({part.strip().lower() for part in value.split(",")}
                     <= {"keep-alive", "close", "upgrade"})
        result.append((key, value))
    return result


@dataclass(frozen=True, repr=False)
class RequestPlan:
    endpoint: str
    method: str
    transport: str
    response_kind: str = "passthrough"
    _headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    follow_redirects: bool = field(default=False, init=False)

    @property
    def headers(self) -> list[tuple[str, str]]:
        return list(self._headers)

    def __repr__(self):
        return f"RequestPlan(method={self.method!r}, transport={self.transport!r})"


@dataclass(frozen=True, repr=False)
class ProviderAdapter:
    host: str
    host_base: str
    wallet_base: str

    def __post_init__(self):
        _require(self.host in _HOST_BASES and self.host_base in _HOST_BASES[self.host],
                 "The original native upstream must be an explicit supported official endpoint.")
        parsed = _base(self.wallet_base)
        _require(parsed.path == "/integrations/native" and not self.wallet_base.endswith("/"),
                 "Wallet base must name the configured native backend prefix.")

    def request_plan(self, method, path, headers, mode, wallet_token=None) -> RequestPlan:
        """Plan one request, not its retry/charge identity; never touches its body.

        Codex wallet model discovery needs caller-side validation of the catalog
        envelope, then projection of models_response (response_kind advertises it).
        Token counts and host compactions advertise distinct completion schemas;
        they are not Messages/Responses inference completion envelopes.
        Alpha search is native passthrough, not token-priced inference. Its wallet
        route must enforce the grant's explicit standalone_search tariff before
        dispatch. The router supplies its admitted lease's operation key, never
        the native body/session ID or an unverified incoming idempotency header.
        WebSocket upgrade/key/version headers belong to the two transport legs,
        not to this end-to-end header list. Subprotocols are unverified/refused.
        """
        _require(mode in {"host", "v"} and method in {"GET", "POST"})
        path, query, params = _path(path)
        incoming = _headers(headers)
        by_name = dict(incoming)
        websocket = by_name.get("upgrade", "").lower() == "websocket"
        _require("upgrade" not in by_name or websocket)
        _require(not websocket or (self.host == "codex" and method == "GET"
                                   and path == "/responses" and not params
                                   and "sec-websocket-protocol" not in by_name))
        _require(websocket or not any(k.startswith("sec-websocket-") for k, _ in incoming))
        response_kind = "passthrough"
        if self.host == "codex":
            if path == "/responses":
                _require((method == "POST" or websocket) and not params)
                suffix = "/v1/responses"
            elif path == "/responses/compact":
                _require(method == "POST" and not params)
                suffix = "/v1/responses/compact"
                response_kind = "codex_compaction"
            elif path == "/models":
                _require(method == "GET" and set(params) <= {"client_version"})
                if mode == "v":
                    _require(params == {"client_version": VERSIONS["codex"]})
                    response_kind = "codex_catalog_envelope"
                suffix = "/codex/catalog"
            elif path in {"/alpha/search", "/v1/alpha/search"}:
                _require(method == "POST" and not params)
                # Codex joins alpha/search to its base: the managed bare
                # loopback origin emits /alpha/search, unlike an API /v1 base.
                suffix = "/v1/alpha/search"
                response_kind = "codex_alpha_search"
            else:
                raise AdapterError("This native endpoint has no reviewed wallet dispatch and billing contract; nothing was sent.")
        else:
            if path in {"/v1/messages", "/v1/messages/count_tokens"}:
                _require(method == "POST" and (not params or params == {"beta": "true"}))
                if path == "/v1/messages/count_tokens":
                    response_kind = "claude_token_count"
            elif path == "/v1/models":
                _require(method == "GET" and set(params) <= {"limit", "before_id", "after_id"}
                         and not {"before_id", "after_id"} <= params.keys())
                if "limit" in params:
                    _require(re.fullmatch(r"[0-9]{1,4}", params["limit"])
                             and 1 <= int(params["limit"]) <= 1000)
            else:
                raise AdapterError("This native endpoint is not certified for the selected payer.")
            suffix = path
        safe = [(k, v) for k, v in incoming if k in _SAFE_HEADERS
                or (k.startswith("anthropic-") and not _SECRET_HEADER.search(k))]
        if mode == "v":
            _require(isinstance(wallet_token, str)
                     and re.fullmatch(r"ov_native_[A-Za-z0-9_-]{16,128}", wallet_token),
                     "A valid wallet capability is required; no native credential fallback.")
            safe.append(("authorization", "Bearer " + wallet_token))
            endpoint = self.wallet_base + suffix + query
        else:
            _require(wallet_token is None, "Host requests cannot carry a wallet capability.")
            auth_names = ({"authorization", "chatgpt-account-id"} if self.host == "codex"
                          else {"authorization", "x-api-key"})
            if self.host_base == "https://api.openai.com/v1":
                auth_names = {"authorization", "openai-organization", "openai-project"}
            safe.extend((k, v) for k, v in incoming if k in auth_names)
            host_path = path
            if self.host_base == "https://api.openai.com/v1" and path == "/v1/alpha/search":
                # The retained Codex 0.166.0 API-key probe uses a /v1 base but
                # emits this already-versioned path, unlike /responses.
                host_path = "/alpha/search"
            endpoint = self.host_base + host_path + query
        transport = "websocket" if websocket else "http"
        if websocket:
            endpoint = ("wss:" if endpoint.startswith("https:") else "ws:") + endpoint.split(":", 1)[1]
        return RequestPlan(endpoint, method, transport, response_kind, tuple(safe))


def _local_url(value, *, header_auth=False):
    parsed = _base(value)
    _require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
             and parsed.port is not None
             and (parsed.path == "" if header_auth else
                  re.fullmatch(r"/(?:[0-9a-f]{64}|[A-Za-z0-9_-]{43})", parsed.path)),
             "Native launch requires a verified session-local loopback URL.")


def _claude_routing_env(env):
    return any(k == "ANTHROPIC_BASE_URL" or k.startswith("CLAUDE_CODE_USE_")
               or (k.startswith("ANTHROPIC_") and k.endswith("BASE_URL")) for k in env)


def _config_root(expression):
    _require(isinstance(expression, str) and "=" in expression)
    try:
        parsed = tomllib.loads(expression.split("=", 1)[0] + "=0")
        _require(len(parsed) == 1)
        return next(iter(parsed))
    except tomllib.TOMLDecodeError:
        raise AdapterError("Unsupported configuration override; launch unchanged.") from None


def launch_overlay(host, version, local_url, env, args, *, local_capability=None,
                   peer_authenticated=False):
    """Return copied (env, argv-without-executable); never edit a shared profile.

    Only the provided launch inputs are inspected. The supervisor must verify
    the effective built-in provider/settings before allowing native requests;
    indirect managed/user configuration is deliberately not read here.
    """
    _require(host in VERSIONS and version == VERSIONS[host],
             "This native version is not verified for the local inference adapter.")
    _require(type(peer_authenticated) is bool)
    _require(not peer_authenticated or (host == "codex" and local_capability is None),
             "Peer authentication is reserved for the verified native Codex launcher.")
    _require(local_capability is None or host == "claude",
             "The built-in Codex provider does not accept local authentication headers.")
    _local_url(local_url, header_auth=local_capability is not None or peer_authenticated)
    if local_capability is not None:
        _require(isinstance(local_capability, str)
                 and re.fullmatch(r"(?:[0-9a-f]{64}|[A-Za-z0-9_-]{43})", local_capability),
                 "Invalid session-local authentication capability.")
    _require(isinstance(env, Mapping) and all(isinstance(k, str) and isinstance(v, str)
                                            for k, v in env.items()))
    _require(isinstance(args, (list, tuple)) and all(isinstance(a, str) and "\x00" not in a
                                                  for a in args))
    updated, original = dict(env), list(args)
    _require(LOCAL_AUTH_ENV not in env, "An existing local router cannot be overlaid.")
    _require(not env.get("OPENVEGAS_NATIVE_TOKEN"), "Existing wallet launch cannot be overlaid.")
    _require(not any(value for key, value in env.items()
                     if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}),
             "An environment proxy needs explicit loopback isolation verification.")
    if host == "codex":
        _require(not any(k in env for k in ("OPENAI_BASE_URL", "CODEX_PROVIDER", "CODEX_CONFIG")),
                 "Existing native routing override is not supported; launch unchanged.")
    else:
        _require(not _claude_routing_env(env),
                 "Existing native routing override is not supported; launch unchanged.")
    i = 0
    while i < len(original):
        arg = original[i]
        if arg == "--":
            break
        flag, equal, value = arg.partition("=")
        if host == "codex":
            _require(flag not in {"--oss", "--local-provider", "--profile", "-p"})
            _require(not arg.startswith("-p") or arg.startswith("--"))
            if flag in {"-c", "--config"} or (arg.startswith("-c") and len(arg) > 2):
                if arg.startswith("-c") and flag != "-c":
                    value = arg[2:]
                elif not equal:
                    i += 1
                    _require(i < len(original))
                    value = original[i]
                _require(_config_root(value) not in _CODEX_ROUTING,
                         "Existing native routing override is not supported; launch unchanged.")
        else:
            _require(flag not in {"--base-url", "--api-base-url", "--settings-file", "--agent-url"})
            if flag == "--settings":
                if not equal:
                    i += 1
                    _require(i < len(original))
                    value = original[i]
                try:
                    settings = json.loads(value)
                    _require(isinstance(settings, dict) and isinstance(settings.get("env", {}), dict))
                    _require(not _claude_routing_env(settings.get("env", {})))
                    if local_capability is not None:
                        _require(not {LOCAL_AUTH_ENV, "ANTHROPIC_CUSTOM_HEADERS"}
                                 & settings.get("env", {}).keys())
                except (ValueError, TypeError):
                    raise AdapterError("Opaque settings overrides require prior verification.") from None
        i += 1
    if host == "codex":
        overlay = ["-c", "openai_base_url=" + json.dumps(local_url)]
        return updated, [*overlay, *original]
    updated["ANTHROPIC_BASE_URL"] = local_url
    if local_capability is not None:
        custom = env.get("ANTHROPIC_CUSTOM_HEADERS", "")
        _require(len(custom) <= 16384 and all(
            ":" in line and _ascii(line) and line.split(":", 1)[0].strip().lower()
            != LOCAL_AUTH_HEADER for line in custom.splitlines()),
            "Existing custom headers conflict with local router authentication.")
        updated["ANTHROPIC_CUSTOM_HEADERS"] = (
            (custom + "\n" if custom else "") + LOCAL_AUTH_HEADER + ": " + local_capability)
    return updated, original
