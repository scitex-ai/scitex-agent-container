"""``sac mcp`` Click group — start / doctor / list-tools / install (F-CS15).

The MCP server itself lives in :mod:`scitex_agent_container._mcp.server`.
This module is the CLI face that operators / installers invoke. Shape
mirrors the canonical scitex package convention (`scitex-dataset mcp …`).
"""

from __future__ import annotations

import json as json_mod

import click

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


@click.group(name="mcp", context_settings=CONTEXT_SETTINGS)
def mcp() -> None:
    """MCP (Model Context Protocol) server commands."""


@mcp.command("start")
@click.option(
    "--http",
    "use_http",
    is_flag=True,
    default=False,
    help="Use HTTP transport instead of stdio.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="HTTP bind address (only with --http).",
)
@click.option(
    "--port",
    default=8970,
    show_default=True,
    help="HTTP bind port (only with --http).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print launch plan without starting.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the (currently never-shown) confirm prompt; reserved for parity.",
)
def mcp_start(use_http: bool, host: str, port: int, dry_run: bool, yes: bool) -> None:
    """Start the scitex-agent-container MCP server.

    \b
    Example:
      $ sac mcp start                          # stdio (default)
      $ sac mcp start --http --port 8970       # HTTP transport
      $ sac mcp start --dry-run
    """
    del yes  # reserved
    transport = "http" if use_http else "stdio"
    if dry_run:
        click.echo(
            f"DRY RUN — would start scitex-agent-container MCP server "
            f"(transport={transport}{', host=' + host + ', port=' + str(port) if use_http else ''})"
        )
        return
    try:
        from .._mcp import run_server
    except ImportError as exc:
        raise click.ClickException(
            "fastmcp not installed — install with "
            "`pip install scitex-agent-container[mcp]`"
        ) from exc
    if use_http:
        click.echo(f"Starting MCP server on http://{host}:{port}")
    run_server(transport=transport, host=host, port=port)


@mcp.command("doctor")
def mcp_doctor() -> None:
    """Check MCP server dependencies + tool registration health.

    \b
    Example:
      $ sac mcp doctor
    """
    click.secho("Checking MCP dependencies...", fg="cyan")
    try:
        import fastmcp

        click.secho("  OK ", fg="green", nl=False)
        click.echo(f"fastmcp {fastmcp.__version__}")
    except ImportError:
        click.secho("  NG ", fg="red", nl=False)
        click.echo("fastmcp not installed")
        click.echo("     Install: pip install scitex-agent-container[mcp]")
        raise SystemExit(1)
    try:
        from .._mcp.server import get_server

        server = get_server()
        tools = _list_tool_names(server)
        click.secho("  OK ", fg="green", nl=False)
        click.echo(f"sac MCP server ({len(tools)} tools)")
    except Exception as exc:  # stx-allow: fallback (reason: doctor exits non-zero on any registration failure — surface the reason without crashing)
        click.secho("  NG ", fg="red", nl=False)
        click.echo(f"MCP server error: {exc}")
        raise SystemExit(1)
    click.secho("\nMCP server ready.", fg="green")
    click.echo("Run: sac mcp start")


@mcp.command("list-tools")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON (for programmatic consumers).",
)
def mcp_list_tools(as_json: bool) -> None:
    """List MCP tools exposed by the sac server.

    \b
    Example:
      $ sac mcp list-tools
      $ sac mcp list-tools --json
    """
    try:
        from .._mcp.server import get_server

        server = get_server()
        tools = _list_tools(server)
    except ImportError:
        if as_json:
            click.echo(
                json_mod.dumps(
                    {"count": 0, "tools": [], "error": "fastmcp not installed"}
                )
            )
            return
        click.echo(
            "fastmcp not installed — install with `pip install scitex-agent-container[mcp]`"
        )
        return

    if as_json:
        click.echo(
            json_mod.dumps(
                {
                    "count": len(tools),
                    "tools": [
                        {"name": t["name"], "description": t["description"]}
                        for t in tools
                    ],
                }
            )
        )
        return

    click.secho(f"sac MCP: {len(tools)} tools", fg="cyan", bold=True)
    click.echo()
    for t in tools:
        click.secho(f"  {t['name']}", fg="green", bold=True)
        if t["description"]:
            first = t["description"].split("\n", 1)[0].strip()
            click.echo(f"    {first}")


@mcp.command("install")
@click.option(
    "--claude-code",
    is_flag=True,
    default=False,
    help="Print the Claude Code MCP config snippet.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print install plan without writing anything (no-op today; reserved).",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the (currently never-shown) confirm prompt; reserved for parity.",
)
def mcp_install(claude_code: bool, dry_run: bool, yes: bool) -> None:
    """Show MCP installation instructions.

    \b
    Example:
      $ sac mcp install
      $ sac mcp install --claude-code
      $ sac mcp install --dry-run
    """
    del dry_run, yes  # `install` only prints today; flags reserved
    if claude_code:
        click.secho("Add to your Claude Code MCP config:", fg="cyan")
        click.echo()
        click.echo('  "scitex-agent-container": {')
        click.echo('    "command": "sac",')
        click.echo('    "args": ["mcp", "start"]')
        click.echo("  }")
        return
    click.secho("scitex-agent-container MCP Server Installation", fg="cyan", bold=True)
    click.echo("=" * 50)
    click.echo()
    click.echo("1. Install: pip install scitex-agent-container[mcp]")
    click.echo("2. Config:  sac mcp install --claude-code")
    click.echo("3. Test:    sac mcp doctor")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _list_tools(server) -> list[dict]:
    """Return ``[{name, description}]`` for every tool registered on ``server``."""
    tools = _enumerate_tools(server)
    return [
        {"name": t.name, "description": (getattr(t, "description", "") or "").strip()}
        for t in sorted(tools, key=lambda t: t.name)
    ]


def _list_tool_names(server) -> list[str]:
    return sorted(t.name for t in _enumerate_tools(server))


def _enumerate_tools(server) -> list:
    """FastMCP version-agnostic tool enumeration. Returns a list of
    Tool objects (each with at least ``.name`` and ``.description``)."""
    import asyncio

    # FastMCP 3.x — async list_tools() returns list[Tool].
    if hasattr(server, "list_tools"):
        try:
            result = asyncio.run(server.list_tools())
            if isinstance(result, list):
                return result
        except RuntimeError:
            # Already in a running event loop; FastMCP servers usually
            # call this from sync `mcp_*` commands so this branch is
            # rare. Fall through to the dict-shape fallback.
            pass
    # FastMCP 2.x — server.tools / server._tools is dict-like.
    for attr in ("_tool_manager", "tools", "_tools"):
        obj = getattr(server, attr, None)
        if obj is None:
            continue
        inner = getattr(obj, "_tools", None)
        if isinstance(inner, dict):
            return list(inner.values())
        if isinstance(obj, dict):
            return list(obj.values())
    return []


__all__ = ["mcp"]
