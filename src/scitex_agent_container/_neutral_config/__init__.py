"""Harness-neutral agent config (``.agents/``) and its native renderers — ADR-0033.

One source (``AGENTS.md``, ``skills/``, ``subagents/``, ``commands/``,
``hooks.yaml``, ``mcp.yaml``, ``settings.yaml``) in two layers (user, then
project), rendered per harness by an adapter. Claude Code is the first
adapter; ``sac config deploy|check|show|import`` drive it.
"""

from __future__ import annotations

from ._claude import plan as plan_claude
from ._import_claude import hooks_equivalent, import_claude
from ._layers import find_project_root, project_layer_root, user_layer_root
from ._model import (
    NEUTRAL_EVENTS,
    Hook,
    Layer,
    NeutralConfigError,
    load_layer,
    merge_layers,
)
from ._plan import Link, Result, WriteFile, apply, check

__all__ = [
    "NEUTRAL_EVENTS",
    "Hook",
    "Layer",
    "Link",
    "NeutralConfigError",
    "Result",
    "WriteFile",
    "apply",
    "check",
    "find_project_root",
    "hooks_equivalent",
    "import_claude",
    "load_layer",
    "merge_layers",
    "plan_claude",
    "project_layer_root",
    "user_layer_root",
]
