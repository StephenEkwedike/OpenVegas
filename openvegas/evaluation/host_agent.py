"""Restricted CLI decisions using existing subscriptions, never API-key fallback."""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from openvegas.arena.dataset import canonical

SYSTEM = (
    "You are one agent in an OpenVegas Arena research poker hand. "
    "All stakes are simulated, non-redeemable $V; no real money or inference credits. "
    "Maximize your expected final chip balance using only your observation. "
    "You may fold, check, call or raise when legal. Do not assume opponents' hidden cards. "
    "Return only the requested JSON action and amount_to (null except raises). "
    "Do not explain your reasoning, use tools, inspect files or contact services."
)
ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["fold", "check", "call", "raise"]},
        "amount_to": {"type": ["integer", "null"]},
    },
    "required": ["type", "amount_to"],
    "additionalProperties": False,
}


def schema_for(observation):
    """Constrain representation, not strategy; retain every legal wager size."""
    legal = observation["legal_actions"]
    branches = []
    ordinary = [a for a in ("fold", "check", "call") if a in legal]
    if ordinary:
        branches.append(
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ordinary},
                    "amount_to": {"type": "null"},
                },
                "required": ["type", "amount_to"],
                "additionalProperties": False,
            }
        )
    if "raise" in legal:
        branches.append(
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["raise"]},
                    "amount_to": {
                        "type": "integer",
                        "minimum": legal["raise"]["min_to"],
                        "maximum": legal["raise"]["max_to"],
                    },
                },
                "required": ["type", "amount_to"],
                "additionalProperties": False,
            }
        )
    if not branches:
        raise ValueError("no_legal_action")
    return {
        "type": "object",
        "properties": {"action": {"anyOf": branches}},
        "required": ["action"],
        "additionalProperties": False,
    }


class HostFailure(RuntimeError):
    def __init__(self, code, usage=None):
        super().__init__(code)
        self.code = code
        self.usage = usage or {}


def environment():
    # Do not inherit provider keys, gateways or the current Codex child settings.
    allowed = {"HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME"}
    return {k: v for k, v in os.environ.items() if k in allowed}


def run_process(command, *, cwd, prompt=None, timeout=90):
    started = time.perf_counter()
    p = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, _ = p.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
        raise HostFailure(
            "host_timeout", {"latency_ms": round((time.perf_counter() - started) * 1000)}
        ) from None
    return p.returncode, stdout, round((time.perf_counter() - started) * 1000)


def preflight(host, work):
    if host not in {"claude", "codex"}:
        raise ValueError("unknown_host")
    code, text, _ = run_process([host, "--version"], cwd=work, timeout=10)
    version = text.strip().splitlines()[-1] if code == 0 and text.strip() else "unknown"
    command = [host, "auth", "status"] if host == "claude" else [host, "login", "status"]
    result = subprocess.run(
        command,
        cwd=work,
        env=environment(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if host == "claude":
        try:
            data = json.loads(result.stdout)
        except ValueError:
            data = {}
        ready = bool(
            data.get("loggedIn") and data.get("authMethod") in {"claude.ai", "oauth_token"}
        )
    else:
        ready = (
            result.returncode == 0 and "Logged in using ChatGPT" in result.stdout + result.stderr
        )
    return {
        "host": host,
        "version": version,
        "subscription_ready": ready,
        "auth_method": data.get("authMethod") if host == "claude" else "chatgpt" if ready else None,
        "reason": None if ready else "existing_subscription_login_required",
    }


def prompt_for(agent_id, observation):
    return SYSTEM + "\n" + canonical({"agent_id": agent_id, "observation": observation})


def parse_result(host, output):
    usage = {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "inference_cost_usd": None,
        "reported_equivalent_cost_usd": None,
        "billing": "existing_host_subscription",
        "observed_model": None,
    }
    try:
        if host == "codex":
            events = [json.loads(line) for line in output.splitlines() if line.strip()]
            if any(e.get("type") in {"turn.failed", "error"} for e in events):
                raise HostFailure("host_turn_failed", usage)
            messages = []
            for event in events:
                item = event.get("item", {})
                if event.get("type") == "item.completed":
                    if item.get("type") == "agent_message":
                        messages.append(item["text"])
                    elif item.get("type") not in {"reasoning"}:
                        raise HostFailure("unexpected_tool_activity", usage)
                if event.get("type") == "turn.completed":
                    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                        usage[key] = event.get("usage", {}).get(key)
            if not any(e.get("type") == "turn.completed" for e in events):
                raise HostFailure("missing_turn_completion", usage)
            action = json.loads(messages[-1])
        else:
            data = json.loads(output)
            u = data.get("usage", {})
            usage.update(
                input_tokens=u.get("input_tokens"),
                output_tokens=u.get("output_tokens"),
                cached_input_tokens=u.get("cache_read_input_tokens"),
                reported_equivalent_cost_usd=data.get("total_cost_usd"),
            )
            models = list(data.get("modelUsage", {}))
            usage["observed_model"] = models[0] if len(models) == 1 else None
            if data.get("is_error") or data.get("subtype") != "success":
                message = str(data.get("result", "")).lower()
                reason = "host_credit_unavailable" if "credit" in message else "host_turn_failed"
                raise HostFailure(reason, usage)
            action = data.get("structured_output")
            if action is None:
                action = json.loads(data["result"])
        if isinstance(action, dict) and set(action) == {"action"}:
            action = action["action"]
        if not isinstance(action, dict) or set(action) != {"type", "amount_to"}:
            raise ValueError("schema")
        if action["type"] not in ACTION_SCHEMA["properties"]["type"]["enum"]:
            raise ValueError("schema")
        if action["amount_to"] is not None and type(action["amount_to"]) is not int:
            raise ValueError("schema")
        if (action["type"] == "raise") != (type(action["amount_to"]) is int):
            raise ValueError("schema")
        return action, usage
    except (ValueError, KeyError, IndexError, TypeError):
        raise HostFailure("invalid_structured_output", usage) from None


class HostAgent:
    def __init__(self, host, model, work):
        if host not in {"claude", "codex"}:
            raise ValueError("unknown_host")
        self.host, self.model, self.work = host, model, Path(work)
        self.work.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.schema = self.work / "action-schema.json"
        self.schema.write_text(json.dumps(ACTION_SCHEMA))

    def command(self):
        if self.host == "codex":
            return [
                "codex",
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--disable",
                "shell_tool",
                "--disable",
                "apps",
                "--disable",
                "plugins",
                "--disable",
                "shell_snapshot",
                "-c",
                'web_search="disabled"',
                "-c",
                "project_doc_max_bytes=0",
                "-c",
                'model_reasoning_effort="low"',
                "-m",
                self.model,
                "--output-schema",
                str(self.schema),
                "--json",
                "-",
            ]
        return [
            "claude",
            "-p",
            "--model",
            self.model,
            "--restricted",
            "--tools",
            "",
            "--disable-slash-commands",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--no-session-persistence",
            "--system-prompt",
            SYSTEM,
            "--output-format",
            "json",
            "--json-schema",
            self.schema.read_text(),
        ]

    def decide(self, agent_id, observation):
        self.schema.write_text(json.dumps(schema_for(observation)))
        code, output, latency = run_process(
            self.command(), cwd=self.work, prompt=prompt_for(agent_id, observation)
        )
        try:
            action, usage = parse_result(self.host, output)
        except HostFailure as exc:
            exc.usage.update(latency_ms=latency, return_code=code)
            raise
        usage.update(latency_ms=latency, return_code=code, requested_model=self.model)
        if code:
            raise HostFailure("host_process_failed", usage)
        return action, usage
