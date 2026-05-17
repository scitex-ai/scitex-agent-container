"""``sac host ...`` tools (F-CS15) — Python API + MCP wrappers."""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_json, invoke_cli_text


def host_list() -> dict[str, Any]:
    """List configured peers (~/.scitex/agent-container/config.yaml).
    Mirrors ``sac host list --json``."""
    return invoke_cli_json(["host", "list", "--json"])


def host_validate() -> dict[str, Any]:
    """Validate the config.yaml peer config (chain integrity,
    unknown via, schema). Mirrors ``sac host validate --json``."""
    return invoke_cli_json(["host", "validate", "--json"])


def host_probe(peer: str, timeout: int = 5) -> dict[str, Any]:
    """SSH-probe a peer for liveness + sac availability. Mirrors
    ``sac host probe <peer> --json``."""
    return invoke_cli_json(["host", "probe", peer, "--timeout", str(timeout), "--json"])


def host_exec(peer: str, command: list[str]) -> dict[str, Any]:
    """Run a sac sub-command on ``peer`` over multi-hop SSH.
    Mirrors ``sac host exec <peer> -- <command...>``."""
    return invoke_cli_text(["host", "exec", peer, "--", *command])


def register_host_tools(mcp) -> None:
    for fn in (host_list, host_validate, host_probe, host_exec):
        mcp.tool()(fn)


__all__ = [
    "host_list",
    "host_validate",
    "host_probe",
    "host_exec",
    "register_host_tools",
]
