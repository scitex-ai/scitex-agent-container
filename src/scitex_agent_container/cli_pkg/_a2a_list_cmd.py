"""``sac a2a list`` — every peer registered on the local ``sac listen``.

Extracted from :mod:`.a2a_group` (over the per-file cap) and registered
onto it by :func:`register`, the same way :mod:`._host_sync` attaches to
``sac host``. The fetch itself lives in :mod:`._a2a_list_fetch`.
"""

from __future__ import annotations

import json

import click

__all__ = ["a2a_list", "register"]


@click.command("list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit a JSON array instead of a rich table (scripting-friendly).",
)
@click.option(
    "--url",
    "base_url",
    default=None,
    help=("Listen base URL. Default: $SAC_LISTEN_BASE_URL or http://127.0.0.1:7878."),
)
def a2a_list(as_json: bool, base_url: str | None) -> None:
    """List every peer registered on the local ``sac listen`` (the a2a registry).

    Queries ``GET /agents`` on the local listen server -- the SAME source
    the ``a2a_peers`` MCP tool reads. Shows container agents (Registry)
    AND self-registered comms-nodes: any process that holds the sac MCP
    and self-registers at startup (e.g. ``sac mcp channel --name lead``).

    Fail-loud: aborts with a clear message if no listen bearer token is
    found or the listen server is unreachable -- no silent empty result.

    \b
    Example:
      $ sac a2a list
      $ sac a2a list --json | jq '.[] | select(.kind == "comms-node")'
    """
    import os

    from .._listen.tokens import default_token_path, read_token

    url = base_url or os.environ.get("SAC_LISTEN_BASE_URL", "http://127.0.0.1:7878")
    token = os.environ.get("SAC_LISTEN_BEARER")
    if not token:
        token = read_token(default_token_path())
    if not token:
        raise SystemExit(
            "sac a2a list: no listen bearer token found "
            f"($SAC_LISTEN_BEARER unset and {default_token_path()} absent). "
            "Is `sac listen` running on this host?"
        )

    # Fail-loud, no raw traceback: fetch_agents maps every reach/read/parse
    # failure (URLError, socket timeout, OSError, non-JSON / odd body) to one
    # clean A2aListError. The inline version only caught URLError, so a socket
    # timeout on a loaded runner or a non-JSON body crashed UNHANDLED and broke
    # callers shelling out to `sac a2a list --json` (scitex-dev 2026-06-17:
    # Spartan runner bm159 → todo /fleet/mesh 500'd → CI blocked).
    from ._a2a_list_fetch import A2aListError, fetch_agents

    try:
        agents = fetch_agents(url, token)
    except A2aListError as exc:
        raise SystemExit(f"sac a2a list: {exc}") from exc
    if as_json:
        click.echo(json.dumps(agents, ensure_ascii=False))
        return

    from ._helpers import console

    if not agents:
        console.print("[dim](no a2a peers)[/dim]")
        return

    from rich.table import Table

    table = Table(show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("kind")
    table.add_column("host")
    table.add_column("a2a_port", justify="right")
    table.add_column("turn_url", overflow="fold")
    for a in agents:
        port = a.get("a2a_port")
        table.add_row(
            str(a.get("name", "")),
            str(a.get("kind", "agent")),
            str(a.get("host", "")),
            "" if port is None else str(port),
            str(a.get("turn_url") or ""),
        )
    console.print(table)


def register(a2a_group) -> None:
    """Attach ``list`` to the parent ``a2a`` Click group."""
    a2a_group.add_command(a2a_list)
