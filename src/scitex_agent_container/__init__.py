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

from scitex_agent_container._lifecycle.lifecycle import (
    agent_logs,
    agent_restart,
    agent_start,
    agent_status,
    agent_stop,
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
    # Lifecycle
    "agent_start",
    "agent_stop",
    "agent_restart",
    "agent_status",
    "agent_logs",
    # Registry
    "Registry",
    # Submodules with coherent identity (per
    # general/03_interface_01_python-api/08_submodule-exposure.md):
    "peer",
]

# EOF
