#!/usr/bin/env python3
# File: src/scitex_agent_container/__init__.py

"""SciTeX Agent Container -- Declarative agent management.

Provides a YAML-based framework for defining, managing, and orchestrating
AI coding agent instances across container runtimes.

Public surface — CLI-tree-shaped noun submodules::

    import scitex_agent_container as sac

    sac.agent.list()                  # `sac agent list`
    sac.agent.start("head-nas")       # `sac agent start head-nas`
    sac.db.query(table="events")      # `sac db query --table=events`
    sac.host.list()                   # `sac host list`
    sac.skills.get("02_quick-start")  # `sac dev skills get 02_quick-start`

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
from typing import TYPE_CHECKING

# PA-102 §1: names in __all__ must be statically visible.
# These imports are only evaluated by type-checkers / AST auditors,
# never at runtime — so they don't pay the heavy import cost.
if TYPE_CHECKING:
    from scitex_agent_container._api import (  # noqa: F401
        account,
        agent,
        db,
        host,
        image,
        mcp,
        skills,
        template,
    )
    from scitex_agent_container._network import peer  # noqa: F401
    from scitex_agent_container._state.registry import Registry  # noqa: F401
    from scitex_agent_container.config import (  # noqa: F401
        AgentConfig,
        load_config,
        validate_config,
    )

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

# Lazy imports via PEP 562 __getattr__ — keeps CLI startup under the 500 ms
# budget (importing _api → _mcp.server alone costs ~20 ms; config._types costs
# ~16 ms; pulling all of them at import time pushed `sac --help` to ~670 ms).
_API_NAMES = {
    "account",
    "agent",
    "db",
    "host",
    "image",
    "mcp",
    "skills",
    "template",
}
_LAZY: dict = {}


def __getattr__(name: str):
    if name in _API_NAMES:
        if name not in _LAZY:
            from scitex_agent_container import _api as _api_mod

            for _n in _API_NAMES:
                _LAZY[_n] = getattr(_api_mod, _n)
        return _LAZY[name]
    if name == "peer":
        if "peer" not in _LAZY:
            from scitex_agent_container._network import peer as _peer

            _LAZY["peer"] = _peer
        return _LAZY["peer"]
    if name == "Registry":
        if "Registry" not in _LAZY:
            from scitex_agent_container._state.registry import Registry as _Registry

            _LAZY["Registry"] = _Registry
        return _LAZY["Registry"]
    if name in ("AgentConfig", "load_config", "validate_config"):
        if name not in _LAZY:
            from scitex_agent_container.config import (
                AgentConfig as _AC,
            )
            from scitex_agent_container.config import (
                load_config as _lc,
            )
            from scitex_agent_container.config import (
                validate_config as _vc,
            )

            _LAZY["AgentConfig"] = _AC
            _LAZY["load_config"] = _lc
            _LAZY["validate_config"] = _vc
        return _LAZY[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# EOF
