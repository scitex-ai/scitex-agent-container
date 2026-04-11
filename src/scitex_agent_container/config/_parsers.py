"""Shared spec parsers used by both v1 and v2 config loaders."""

from __future__ import annotations

import re
from typing import Any

from ._types import (
    ClaudeSpec,
    ContainerSpec,
    HealthSpec,
    OrochiSpec,
    RemoteSpec,
    RestartSpec,
    SkillsSpec,
    StartupCommand,
    TelegramSpec,
    WatchdogSpec,
)


def parse_container(spec: dict) -> ContainerSpec:
    raw = spec.get("container", {}) or {}
    return ContainerSpec(
        runtime=raw.get("runtime", "none"),
        image=raw.get("image", "scitex-agent-container:latest"),
        volumes=raw.get("volumes", []) or [],
        network=raw.get("network", "host"),
    )


def parse_claude(spec: dict) -> ClaudeSpec:
    raw = spec.get("claude", {}) or {}
    return ClaudeSpec(
        channels=raw.get("channels", []) or [],
        flags=raw.get("flags", []) or [],
        session=raw.get("session", "new"),
        auto_accept=raw.get("auto_accept", True),
    )


def parse_health(spec: dict) -> HealthSpec:
    raw = spec.get("health", {}) or {}
    return HealthSpec(
        enabled=raw.get("enabled", False),
        interval=raw.get("interval", 30),
        timeout=raw.get("timeout", 5),
        method=raw.get("method", "screen-alive"),
    )


def parse_watchdog(spec: dict) -> WatchdogSpec:
    raw = spec.get("watchdog", {}) or {}
    responses = raw.get("responses", {}) or {}
    return WatchdogSpec(
        enabled=raw.get("enabled", False),
        interval=float(raw.get("interval", 1.5)),
        resp_y_n=str(responses.get("y_n", "1")),
        resp_y_y_n=str(responses.get("y_y_n", "2")),
        resp_waiting=str(responses.get("waiting", "/speak-and-call")),
    )


def parse_restart(spec: dict) -> RestartSpec:
    raw = spec.get("restart", {}) or {}
    backoff = raw.get("backoff", {}) or {}
    return RestartSpec(
        policy=raw.get("policy", "never"),
        max_retries=raw.get("max_retries", 3),
        backoff_initial=backoff.get("initial", 30),
        backoff_max=backoff.get("max", 300),
        backoff_multiplier=backoff.get("multiplier", 2),
    )


def parse_telegram(spec: dict) -> TelegramSpec:
    raw = spec.get("telegram", {}) or {}
    return TelegramSpec(
        bot_token_env=raw.get(
            "bot_token_env", "SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN"
        ),
        allowed_users=[str(u) for u in (raw.get("allowed_users", []) or [])],
        auto_connect=raw.get("auto_connect", True),
        greeting=raw.get("greeting", ""),
    )


def parse_orochi(spec: dict) -> OrochiSpec:
    raw = spec.get("orochi", {}) or {}
    hosts = raw.get("hosts", []) or []
    return OrochiSpec(
        enabled=raw.get("enabled", bool(hosts)),
        hosts=hosts,
        port=int(raw.get("port", 8559)),
        token_env=raw.get("token_env", "SCITEX_OROCHI_TOKEN"),
        channels=raw.get("channels", []) or [],
        heartbeat_interval=int(raw.get("heartbeat_interval", 60)),
    )


def parse_skills(spec: dict) -> SkillsSpec:
    raw = spec.get("skills", {}) or {}
    return SkillsSpec(
        required=raw.get("required", []) or [],
        available=raw.get("available", []) or [],
    )


def parse_remote(spec: dict) -> RemoteSpec:
    raw = spec.get("remote", {}) or {}
    return RemoteSpec(
        host=raw.get("host", ""),
        user=raw.get("user", ""),
        key=raw.get("key", ""),
        port=int(raw.get("port", 22)),
        login_shell=raw.get("login_shell", True),
    )


def parse_hooks(spec: dict) -> dict[str, list[str]]:
    raw = spec.get("hooks", {}) or {}
    return {
        key: raw.get(key, []) or []
        for key in ("pre_start", "post_start", "pre_stop", "post_stop")
    }


def parse_startup_commands(spec: dict) -> list[StartupCommand]:
    raw = spec.get("startup_commands", []) or []
    return [
        StartupCommand(
            delay=int(item.get("delay", 0)),
            command=item.get("command", ""),
        )
        for item in raw
        if isinstance(item, dict) and item.get("command")
    ]


def get_nested(data: dict, key: str, default: Any = None) -> Any:
    """Traverse a dot-separated key path in a nested dict."""
    keys = key.split(".")
    current = data
    for k in keys:
        if not isinstance(current, dict) or k not in current:
            return default
        current = current[k]
    return current


# Model name mapping for auto-derived SCITEX_OROCHI_MODEL env var
MODEL_DISPLAY_NAMES: dict[str, str] = {
    "opus": "Claude Opus",
    "opus[1m]": "Claude Opus (1M)",
    "sonnet": "Claude Sonnet",
    "sonnet[1m]": "Claude Sonnet (1M)",
    "haiku": "Claude Haiku",
}


def interpolate_metadata(value: str, metadata: dict) -> str:
    """Replace ${metadata.*} references in a string value."""

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key == "metadata.name":
            return metadata.get("name", m.group(0))
        if key.startswith("metadata.labels."):
            label = key[len("metadata.labels.") :]
            labels = metadata.get("labels", {}) or {}
            return labels.get(label, m.group(0))
        return m.group(0)

    return re.sub(r"\$\{([^}]+)\}", _replace, value)


def interpolate_mcp_servers(mcp_raw: dict, metadata: dict) -> dict[str, dict]:
    """Deep-interpolate ${metadata.*} in mcp_servers env values."""
    result: dict[str, dict] = {}
    for server_name, server_def in (mcp_raw or {}).items():
        entry = dict(server_def)
        if "env" in entry and isinstance(entry["env"], dict):
            entry["env"] = {
                k: interpolate_metadata(str(v), metadata)
                for k, v in entry["env"].items()
            }
        if "args" in entry and isinstance(entry["args"], list):
            entry["args"] = [
                interpolate_metadata(str(a), metadata) for a in entry["args"]
            ]
        result[server_name] = entry
    return result
