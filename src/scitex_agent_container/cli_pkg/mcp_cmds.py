"""MCP command group.

`scitex-agent-container` does not bundle MCP servers — each agent spawns
its own via the agent's ``src_mcp.json``. The ``mcp list-tools`` command
exists to satisfy the audit-cli §1a contract (every package CLI must
expose an ``mcp`` group with ``list-tools``) and reports an empty
inventory in JSON.
"""

from __future__ import annotations

import json as json_mod

import click


@click.group(name="mcp")
def mcp() -> None:
    """MCP introspection (no servers bundled — see per-agent src_mcp.json)."""


@mcp.command(name="list-tools")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
def list_tools(as_json: bool) -> None:
    """List MCP tools exposed by this package (none — agents bring their own).

    \b
    Example:
      $ sac mcp list-tools
      $ sac mcp list-tools --json
    """
    payload: list[dict] = []
    if as_json:
        click.echo(json_mod.dumps(payload))
        return
    click.echo("scitex-agent-container does not bundle MCP servers.")
    click.echo("Each agent spawns its own MCP servers via its src_mcp.json.")


__all__ = ["mcp"]
