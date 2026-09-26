"""Hermes adapter for the selected claude-code-telegrammer channel."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ..config import AgentConfig
from ._cct_token_resolution import TOKEN_RESOLVED, resolve_cct_token
from ._sdk_channels import (
    _TELEGRAMMER_CHANNEL,
    _TELEGRAMMER_MCP_KEY,
    _TELEGRAMMER_TURN_URL_ENV,
    compute_channel_plan,
)

_EXTERNAL_POLLER_ENV = "CLAUDE_CODE_TELEGRAMMER_EXTERNAL_POLLER"


class HermesCctRailError(RuntimeError):
    """A selected Hermes Telegram rail cannot receive and wake this session."""


def cct_requested(config: AgentConfig) -> bool:
    channels = getattr(config.claude, "channels", None) or ()
    return _TELEGRAMMER_CHANNEL in {str(value).strip() for value in channels}


def wire_hermes_cct_rail(
    config: AgentConfig,
    *,
    home: Path,
    servers: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Validate and wire a selected CCT MCP to Hermes' native turn bridge.

    ``deploy_to_home`` has already reused the fleet token-pool resolver and
    materialised the winning token into ``home/.env``. This is therefore a
    validation/wiring step, not a second token derivation.
    """
    if not cct_requested(config):
        return {}

    resolution = resolve_cct_token(config, dest=home)
    if resolution.outcome != TOKEN_RESOLVED:
        raise HermesCctRailError(
            f"Hermes CCT rail requested for {config.name!r}, but no Telegram "
            f"bot token materialised ({resolution.outcome}): {resolution.detail}"
        )

    entry = servers.get(_TELEGRAMMER_MCP_KEY)
    if not isinstance(entry, dict):
        raise HermesCctRailError(
            f"Hermes CCT rail requested for {config.name!r}, but generated MCP "
            f"profile has no {_TELEGRAMMER_MCP_KEY!r} entry. Declare that exact "
            "key in to_home/.mcp.json."
        )

    channels = tuple(getattr(config.claude, "channels", None) or ())
    plan = compute_channel_plan(
        channels, getattr(config.a2a, "port", None), config.name
    )
    turn_url = plan.telegrammer_turn_url
    if not turn_url:
        raise HermesCctRailError(
            f"Hermes CCT rail requested for {config.name!r}, but no /v1/turn URL "
            "can be generated. Set spec.a2a.port to 'auto' or an explicit port."
        )
    env = entry.setdefault("env", {})
    if not isinstance(env, dict):
        raise HermesCctRailError(
            f"Hermes MCP {_TELEGRAMMER_MCP_KEY!r} env must be an object"
        )
    # Hermes resolves `${env:NAME}` from its inherited environment. The
    # shared `.mcp.json` uses Claude's `${NAME}` spelling, so normalize these
    # two CCT references without copying either secret value into config.yaml.
    env["CCT_BOT_TOKEN"] = "${env:CCT_BOT_TOKEN}"
    env["CCT_AGENT_ID"] = "${env:CCT_AGENT_ID}"
    env[_TELEGRAMMER_TURN_URL_ENV] = turn_url
    # The host-side SAC lifecycle starts the one authoritative standalone
    # poller.  This value must live in the independently launched MCP server's
    # environment too: putting it only on SAC's poller child cannot flow
    # backwards into Hermes or its later MCP children.
    env[_EXTERNAL_POLLER_ENV] = "1"
    return {
        _TELEGRAMMER_TURN_URL_ENV: turn_url,
        _EXTERNAL_POLLER_ENV: "1",
    }


def inspect_materialized_hermes_cct(config: AgentConfig, home: Path) -> dict[str, Any]:
    """Inspect the generated Hermes rail without reading token values."""
    expected = compute_channel_plan(
        getattr(config.claude, "channels", None),
        getattr(config.a2a, "port", None),
        config.name,
    ).telegrammer_turn_url
    profile_path = home / ".hermes" / "config.yaml"
    result: dict[str, Any] = {
        "selected_harness": str(getattr(config, "harness", "")),
        "profile": str(profile_path),
        "profile_present": profile_path.is_file(),
        "mcp_present": False,
        "turn_url_present": False,
        "turn_url_matches": False,
        "expected_turn_url": expected or "",
    }
    if not profile_path.is_file():
        return result
    try:
        document = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        result["error"] = f"generated Hermes profile is unreadable: {exc}"
        return result
    servers = document.get("mcp_servers") if isinstance(document, dict) else None
    entry = servers.get(_TELEGRAMMER_MCP_KEY) if isinstance(servers, dict) else None
    result["mcp_present"] = isinstance(entry, dict)
    env = entry.get("env") if isinstance(entry, dict) else None
    actual = env.get(_TELEGRAMMER_TURN_URL_ENV) if isinstance(env, dict) else None
    result["turn_url_present"] = bool(actual)
    declared_port = getattr(config.a2a, "port", None)
    if declared_port == "auto":
        # The generated profile contains the integer claimed at start, while a
        # freshly loaded authority spec still says ``auto``.
        match = re.fullmatch(
            r"http://127\.0\.0\.1:(\d{1,5})/v1/turn", str(actual or "")
        )
        result["turn_url_matches"] = bool(
            match and 0 < int(match.group(1)) < 65536
        )
        result["expected_turn_url"] = "auto (resolved at start)"
    else:
        result["turn_url_matches"] = bool(expected and actual == expected)
    return result


__all__ = [
    "HermesCctRailError",
    "cct_requested",
    "inspect_materialized_hermes_cct",
    "wire_hermes_cct_rail",
]
