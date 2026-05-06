#!/usr/bin/env python3
# File: src/scitex_agent_container/__init__.py

"""SciTeX Agent Container -- Declarative agent management.

Provides a YAML-based framework for defining, managing, and orchestrating
AI coding agent instances across container runtimes.

Public surface — CLI-tree-shaped noun submodules::

    import scitex_agent_container as sac

    sac.agent.list()                  # `sac agent list`
    sac.agent.start("head-nas")       # `sac agent start head-nas`
    sac.db.query(table="instances")   # `sac db query --table=instances`
    sac.host.show()                   # `sac host show`
    sac.skills.get("02_quick-start")  # `sac skills get 02_quick-start`

Each noun submodule (`agent`, `db`, `host`, `image`, `template`,
`account`, `skills`, `mcp`) re-exports its verbs under bare names
that mirror the CLI subcommand tree. The same function objects power
both the Python API and the MCP server (per scitex MCP §6 parity).

Lifecycle helpers that take a shared ``Registry`` instance live at
``scitex_agent_container._lifecycle.lifecycle`` for callers that
need them. The submodule verbs go through the CLI for JSON-friendly
input/output.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("scitex-agent-container")
except (
    _PackageNotFoundError
):  # stx-allow: fallback (reason: expected failure — see inline comment)
    from pathlib import Path as _Path

    _pyproject = _Path(__file__).parent.parent.parent / "pyproject.toml"
    __version__ = "0.0.0+local"
    if _pyproject.exists():
        with open(_pyproject) as _f:
            for _line in _f:
                if _line.startswith("version"):
                    __version__ = _line.split("=")[1].strip().strip('"')
                    break

from scitex_agent_container._api import (
    account,
    agent,
    db,
    host,
    image,
    mcp,
    skills,
    template,
)
from scitex_agent_container._network import peer
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import AgentConfig, load_config, validate_config

__all__ = [
    "__version__",
    # Config
    "AgentConfig",
    "load_config",
    "validate_config",
    # Registry
    "Registry",
    # CLI-tree-shaped noun submodules — primary public API surface.
    # Each verb is the same function object the MCP server registers
    # (e.g. `sac.agent.list is _mcp._tools._agent.agent_list`).
    "agent",
    "db",
    "host",
    "image",
    "template",
    "account",
    "skills",
    "mcp",
    # Networking submodule (own surface — see _network/peer.py).
    "peer",
]

# EOF
