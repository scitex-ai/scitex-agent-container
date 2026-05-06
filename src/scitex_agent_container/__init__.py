#!/usr/bin/env python3
# File: src/scitex_agent_container/__init__.py

"""SciTeX Agent Container -- Declarative agent management.

Provides a YAML-based framework for defining, managing, and orchestrating
AI coding agent instances across container runtimes.

Modules:
    - config: YAML config loading and validation
    - lifecycle: Agent start/stop/restart/status
    - registry: File-based agent tracking
    - health: Health check implementation
    - runtimes: Container runtime adapters (docker, apptainer, screen)
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
from scitex_agent_container._mcp._tools._account import account_show, quota_watch

# Lifecycle verbs come from _mcp._tools._agent (JSON-friendly thin
# wrappers around the CLI) rather than _lifecycle.lifecycle (which
# takes a Registry param fastmcp can't introspect). The lifecycle
# functions remain reachable directly via
# ``scitex_agent_container._lifecycle.lifecycle`` for callers that
# need to share a Registry instance.
from scitex_agent_container._mcp._tools._agent import (
    agent_attach,
    agent_check,
    agent_check_priority,
    agent_find,
    agent_health,
    agent_inspect,
    agent_list,
    agent_logs,
    agent_recall,
    agent_restart,
    agent_start,
    agent_status,
    agent_stop,
    agent_take_snapshot,
    agent_validate,
)
from scitex_agent_container._mcp._tools._db import (
    db_clean,
    db_export,
    db_import,
    db_migrate,
    db_query,
    db_show,
    db_tick,
)
from scitex_agent_container._mcp._tools._host import (
    host_exec,
    host_list,
    host_probe,
    host_show,
    host_validate,
)
from scitex_agent_container._mcp._tools._image import image_build
from scitex_agent_container._mcp._tools._info import (
    list_python_apis,
    mcp_doctor,
    mcp_list_tools,
)
from scitex_agent_container._mcp._tools._skills import skills_get, skills_list
from scitex_agent_container._mcp._tools._template import (
    template_render_contributor_spec,
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
    # Agent lifecycle (canonical Python APIs — also re-exported as MCP)
    "agent_start",
    "agent_stop",
    "agent_restart",
    "agent_status",
    "agent_logs",
    # Agent inspection / control
    "agent_list",
    "agent_health",
    "agent_find",
    "agent_check",
    "agent_validate",
    "agent_inspect",
    "agent_recall",
    "agent_check_priority",
    "agent_take_snapshot",
    "agent_attach",
    # State-DB
    "db_show",
    "db_query",
    "db_clean",
    "db_tick",
    "db_migrate",
    "db_export",
    "db_import",
    # Host / multi-peer
    "host_show",
    "host_list",
    "host_validate",
    "host_probe",
    "host_exec",
    # Image build
    "image_build",
    # Templates
    "template_render_contributor_spec",
    # Account / quota
    "account_show",
    "quota_watch",
    # Skills introspection (convention §5)
    "skills_list",
    "skills_get",
    # Self-introspection
    "list_python_apis",
    "mcp_list_tools",
    "mcp_doctor",
    # Registry
    "Registry",
    # Submodules with coherent identity (per
    # general/03_interface_01_python-api/08_submodule-exposure.md):
    "peer",
    # CLI-tree-shaped noun submodules (sac.agent.list(), sac.db.query(), …)
    # — same function objects as the flat names above; provided for
    # ergonomic CLI-mirror access.
    "agent",
    "db",
    "host",
    "image",
    "template",
    "account",
    "skills",
    "mcp",
]

# EOF
