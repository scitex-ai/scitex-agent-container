"""MCP server entry-point for scitex-agent-container (F-CS15).

Tool definitions live under :mod:`._tools`. This module is a thin
shim: FastMCP init, tool registration, transport selection.

Usage::

    sac mcp start                  # stdio
    sac mcp start --http --port 8970
    fastmcp run scitex_agent_container._mcp.server:mcp
"""

from __future__ import annotations

import scitex_logging as slogging

# NOTE: ``register_all_tools`` is deliberately NOT imported at module top
# level. Importing it here would pull in all nine tool modules (and their
# ``click.testing`` helper) at ``import scitex_agent_container._mcp`` time —
# i.e. on the `sac mcp start` cold-start path, BEFORE the stdio handshake.
# The heavy tool implementations (host_exec / agent-spawn / db / image /
# template clients) are already lazy-imported inside each tool body; keeping
# the registration import lazy too means a bare ``import _mcp`` (doctor,
# list-tools, the package _api surface) stays cheap, and the tool modules are
# only imported when the server is actually built in ``_build_server``. See
# ``docs/mcp-cold-start.md`` and the F-CS15 cold-start-race fix.


# Importing an MCP helper must not install a second console handler or
# override the caller's level/format. SciTeX owns stderr diagnostics.
log = slogging.getLogger(__name__)

_INSTRUCTIONS = """\
scitex-agent-container (sac) manages agents in container environments.
Discover actual names and schemas through tools/list. Standalone tools use
noun_action names such as agent_list and agent_send, without a sac_ prefix.
CLI commands use the corresponding noun group followed by the action, for
example `sac agents list`; individual capabilities and options can differ.
Use skills_list and skills_get to load package guidance when needed.
"""


def _build_server():
    """Construct + register the FastMCP server. Lazy-imported so that
    `import scitex_agent_container._mcp.server` succeeds even when
    `fastmcp` isn't installed (the CLI's `mcp doctor` then prints an
    actionable message instead of crashing on import)."""
    try:
        from fastmcp import FastMCP
    except Exception as exc:
        raise ImportError(
            "fastmcp is required for the sac MCP server — "
            "install with `pip install scitex-agent-container[mcp]`"
        ) from exc

    # Lazy tool-module import (cold-start race fix): the nine tool modules are
    # imported HERE, at build time, rather than at ``import _mcp`` module load.
    from ._tools import register_all_tools

    server = FastMCP(name="scitex-agent-container", instructions=_INSTRUCTIONS)
    register_all_tools(server)
    return server


# Module-level singleton — built lazily on first attribute access so
# the import never raises when fastmcp is absent (doctor / list-tools
# check for it gracefully).
mcp = None  # type: ignore[assignment]


def get_server():
    """Return the lazily-constructed FastMCP server instance."""
    global mcp
    if mcp is None:
        mcp = _build_server()
    return mcp


def run_server(
    transport: str = "stdio", host: str = "127.0.0.1", port: int = 8970
) -> None:
    """Launch the MCP server on the requested transport.

    ``transport`` is one of ``"stdio"`` (default) or ``"http"``. The
    HTTP variant binds to ``host:port`` (loopback by default — agents
    on the same host share the docker network for peer-to-peer calls).
    """
    server = get_server()
    if transport == "http":
        server.run(transport="http", host=host, port=port)
    else:
        server.run()


__all__ = ["get_server", "mcp", "run_server"]
