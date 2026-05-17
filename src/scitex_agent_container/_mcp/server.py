"""MCP server entry-point for scitex-agent-container (F-CS15).

Tool definitions live under :mod:`._tools`. This module is a thin
shim: FastMCP init, tool registration, transport selection.

Usage::

    sac mcp start                  # stdio
    sac mcp start --http --port 8970
    fastmcp run scitex_agent_container._mcp.server:mcp
"""

from __future__ import annotations

import logging
import sys

from ._tools import register_all_tools


def _ensure_stderr_logging() -> None:
    """Attach a stderr StreamHandler so INFO-level diagnostic lines appear in
    claude-code's MCP debug log. Idempotent."""
    root = logging.getLogger("scitex_agent_container")
    if any(getattr(h, "_sac_stderr", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    )
    handler._sac_stderr = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(logging.INFO)


_ensure_stderr_logging()

log = logging.getLogger(__name__)

_INSTRUCTIONS = """\
scitex-agent-container (sac) — declarative container wrapper for
agents. Tools mirror the `sac` CLI surface: `agent_*` for lifecycle
(list / status / start / stop / logs), `db_*` for SQLite state queries,
`host_*` for multi-host topology, `image_*` for container image
build, `template_*` for spec rendering, plus `account_*`, `skills_*`,
`mcp_*` and a few introspection helpers. Tool names mirror the CLI
verb-noun shape (e.g. `sac_agent_list`).
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
