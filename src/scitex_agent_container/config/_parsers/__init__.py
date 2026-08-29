"""Shared spec parsers used by both v1 and v2 config loaders.

Thin re-export shim so ``from ..config._parsers import parse_*`` keeps
working unchanged. One parser per file under this package; cross-cutting
constants and helpers (HOOK_KEYS, MODEL_DISPLAY_NAMES, get_nested,
interpolate_metadata, _parse_command_list) live in ``_helpers.py``.
"""

from __future__ import annotations

from ._a2a import parse_a2a
from ._apptainer import parse_apptainer
from ._autonomous import parse_autonomous
from ._claude import parse_claude
from ._comms import parse_comms, parse_lineage
from ._container import parse_container
from ._declarations import parse_required_claude_hooks, parse_to_home_layers
from ._extensions import parse_extensions
from ._health import parse_health
from ._helpers import (
    HOOK_KEYS,
    MODEL_DISPLAY_NAMES,
    _parse_command_list,
    get_nested,
    interpolate_metadata,
)
from ._hooks import parse_hooks
from ._hosts import _VALID_SCHEDULING_MODES, parse_hosts_spec, parse_scheduling
from ._listen import parse_listen
from ._mcp import interpolate_mcp_servers
from ._proxy import parse_proxy
from ._restart import parse_restart
from ._skills import parse_skills
from ._startup import parse_startup_commands
from ._watchdog import parse_watchdog

__all__ = [
    "HOOK_KEYS",
    "MODEL_DISPLAY_NAMES",
    "get_nested",
    "interpolate_mcp_servers",
    "interpolate_metadata",
    "parse_a2a",
    "parse_apptainer",
    "parse_autonomous",
    "parse_claude",
    "parse_comms",
    "parse_container",
    "parse_extensions",
    "parse_health",
    "parse_hooks",
    "parse_hosts_spec",
    "parse_lineage",
    "parse_listen",
    "parse_proxy",
    "parse_required_claude_hooks",
    "parse_restart",
    "parse_scheduling",
    "parse_skills",
    "parse_startup_commands",
    "parse_watchdog",
]
