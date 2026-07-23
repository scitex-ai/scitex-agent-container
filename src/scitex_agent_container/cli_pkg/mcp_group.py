"""``sac mcp`` Click group — start / doctor / list-tools / install (F-CS15).

The MCP server itself lives in :mod:`scitex_agent_container._mcp.server`.
This module is the CLI face that operators / installers invoke. Shape
mirrors the canonical scitex package convention (`scitex-dataset mcp …`).
"""

from __future__ import annotations

import json as json_mod

import click

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


# ---------------------------------------------------------------------------
# Backend loaders (public seam for the Python API + tests)
#
# The real MCP server lives in ``scitex_agent_container._mcp``, which is an
# optional dep (requires ``fastmcp``) and isn't always installed in dev /
# CI environments. We surface the backend lookup as callable module
# attributes so:
#
#   * external Python users can install a different MCP server / runner
#     by reassigning these, the same way ``logging.getLogger`` is
#     redirectable,
#   * tests can install hand-rolled real callables (no MagicMock) via
#     the normal save/restore pattern, without fabricating ``sys.modules``
#     entries for ``scitex_agent_container._mcp``.
#
# Each loader returns the resolved callable / object; each raises the real
# ``ImportError`` if the optional ``fastmcp`` extra isn't installed.
# ---------------------------------------------------------------------------
def _default_load_run_server():
    """Default ``run_server`` loader — imports from the optional ``_mcp`` pkg."""
    from .._mcp import run_server

    return run_server


def _default_load_get_server():
    """Default ``get_server`` loader — imports from the optional ``_mcp`` pkg."""
    from .._mcp.server import get_server

    return get_server


def _default_load_fastmcp_version():
    """Default ``fastmcp.__version__`` loader — for ``doctor``."""
    import fastmcp

    return fastmcp.__version__


def _default_load_channel_main():
    """Default ``channel.main`` loader — for ``mcp channel`` subprocess."""
    from .._mcp.channel import main as channel_main

    return channel_main


# Module-level overridable references. Reassign (and restore!) to swap.
_load_run_server = _default_load_run_server
_load_get_server = _default_load_get_server
_load_fastmcp_version = _default_load_fastmcp_version
_load_channel_main = _default_load_channel_main


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
        run_server = _load_run_server()
    except ImportError as exc:
        raise click.ClickException(
            "fastmcp not installed — install with "
            "`pip install scitex-agent-container[mcp]`"
        ) from exc
    if use_http:
        click.echo(f"Starting MCP server on http://{host}:{port}")
    run_server(transport=transport, host=host, port=port)


@mcp.command("channel")
@click.option(
    "--name",
    required=False,
    default=None,
    help=(
        "Agent name whose inbox to subscribe to. When omitted, the "
        "channel walks cwd upward for "
        ".scitex/agent-container/agents/self/spec.yaml and derives the "
        "name from the running session's runtime identity."
    ),
)
@click.option(
    "--listen-url",
    default=None,
    help="sac listen base URL (default: $SAC_LISTEN_BASE_URL or http://127.0.0.1:7878).",
)
@click.option(
    "--turn-url",
    default=None,
    help=(
        "The agent's own colocated /v1/turn endpoint (e.g. "
        "http://127.0.0.1:18888/v1/turn). When set, each received bus event "
        "is POSTed here so a push WAKES an idle session and drives a turn "
        "immediately (push behaves like the lead's Telegram channel). "
        "Without it the adapter only pushes the channel notification, which "
        "does not advance an idle agent's turn."
    ),
)
def mcp_channel(name: str | None, listen_url: str | None, turn_url: str | None) -> None:
    """Run the sac push channel adapter as a stdio MCP subprocess.

    Intended to be spawned by Claude Code via
    ``--dangerously-load-development-channels server:sac`` — see
    ``docs/adr/0008-sac-node-transport-boundary.md``. Streams inbox events from sac listen
    as ``notifications/claude/channel`` so the running session sees
    ``<channel source="..." msg_id="...">`` tags in real time, and (when
    ``--turn-url`` is set) WAKES the session by POSTing each event to the
    agent's own ``/v1/turn`` so an idle agent processes it immediately.

    \b
    Example (manual):
      $ sac mcp channel --name lead
    """
    _channel_main = _load_channel_main()
    _channel_main(name=name, listen_url=listen_url, turn_url=turn_url)


@mcp.command("healthcheck")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the result as JSON.",
)
def mcp_healthcheck(as_json: bool) -> None:
    """Boot self-check + auto-heal for the critical MCP connections.

    Verifies the ``scitex-agent-container`` + ``scitex-todo`` stdio MCP
    servers actually CONNECTED (via ``claude mcp list``), logs the expected
    capability surface so a missing tool reads as "MCP broken → heal" rather
    than "I lack capability", and — if a critical server failed to connect —
    raises a loud alarm and requests a rate-limited ``--fresh`` self-restart
    through the host ``sac listen`` plane. FAIL-OPEN: always exits 0, so a
    ``SessionStart`` hook that runs this never blocks the agent's boot.

    \b
    Example (SessionStart hook):
      $ sac mcp healthcheck
    """
    # Lazy import: keep this out of the cold-start path and out of the CLI's
    # module-import budget. ``_healthcheck`` pulls no heavy deps (no fastmcp).
    from .._mcp._healthcheck import run_healthcheck

    result = run_healthcheck()
    if as_json:
        click.echo(json_mod.dumps(result))
    else:
        action = result.get("action", "ok")
        failed = result.get("failed") or []
        unknown = result.get("unknown") or []
        if failed:
            click.secho(
                f"MCP healthcheck: {', '.join(failed)} FAILED — action={action}",
                fg="red",
            )
        elif action == "unknown" or unknown:
            # HONEST: connectivity could NOT be verified — never render green/OK.
            target = ", ".join(unknown) or "critical MCPs"
            click.secho(
                f"MCP healthcheck: could NOT verify {target} "
                f"(`claude mcp list` unreadable) — action={action}",
                fg="yellow",
            )
        elif action == "ok":
            click.secho("MCP healthcheck: all critical MCPs OK (action=ok)", fg="green")
        else:
            # disabled / error and any future non-ok verdict: state it plainly,
            # never dressed up as OK.
            click.secho(f"MCP healthcheck: action={action}", fg="yellow")
    # Fail-open: NEVER non-zero. A boot hook must not block the session.
    raise SystemExit(0)


@mcp.command("doctor")
def mcp_doctor() -> None:
    """Check MCP server dependencies + tool registration health.

    \b
    Example:
      $ sac mcp doctor
    """
    click.secho("Checking MCP dependencies...", fg="cyan")
    try:
        version = _load_fastmcp_version()
        click.secho("  OK ", fg="green", nl=False)
        click.echo(f"fastmcp {version}")
    except ImportError:
        click.secho("  NG ", fg="red", nl=False)
        click.echo("fastmcp not installed")
        click.echo("     Install: pip install scitex-agent-container[mcp]")
        raise SystemExit(1)
    try:
        get_server = _load_get_server()
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
        get_server = _load_get_server()
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
