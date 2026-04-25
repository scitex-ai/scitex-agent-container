"""``sac a2a serve`` CLI subcommand — standalone A2A protocol surface.

Boots the stdlib HTTP A2A server defined in
:mod:`scitex_agent_container.a2a._server` for one or more agent
YAMLs, with a configurable JSON-RPC ``tasks/send`` handler.

Examples::

    # Echo handler (default), single agent YAML
    sac a2a serve mock-echo.yaml --port 8888

    # Real Claude CLI dispatch
    sac a2a serve mock-echo.yaml --handler claude_cli --port 8888

    # Multi-agent: glob-expanded YAMLs
    sac a2a serve agents/*/*.yaml --port 9000
"""

from __future__ import annotations

import logging
from pathlib import Path

import click

from scitex_agent_container.a2a import HANDLERS, serve


@click.group(name="a2a")
def a2a() -> None:
    """A2A protocol — generic agent-to-agent surface (no fleet deps)."""


@a2a.command("serve")
@click.argument(
    "agent_yamls",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Interface to bind. Use 0.0.0.0 to expose externally.",
)
@click.option(
    "--port",
    type=int,
    default=8888,
    show_default=True,
    help="TCP port for the A2A HTTP server.",
)
@click.option(
    "--handler",
    type=click.Choice(sorted(HANDLERS), case_sensitive=False),
    default="echo",
    show_default=True,
    help=(
        "JSON-RPC tasks/send dispatcher. 'echo' = canned reply, "
        "'claude_cli' = `claude --print`, 'exec' = $SAC_A2A_EXEC_COMMAND."
    ),
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable INFO-level logging on the server.",
)
def a2a_serve(
    agent_yamls: tuple[Path, ...],
    host: str,
    port: int,
    handler: str,
    verbose: bool,
) -> None:
    """Serve A2A endpoints for the given agent YAMLs (foreground)."""
    if verbose:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
    serve(list(agent_yamls), host=host, port=port, handler=handler)
