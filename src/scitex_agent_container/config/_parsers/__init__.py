"""Shared spec parsers used by both v1 and v2 config loaders.

Thin re-export shim for the section parsers. Cross-cutting constants and
helpers live in ``_helpers.py``.
"""

from __future__ import annotations

from ._a2a import parse_a2a
from ._apptainer import parse_apptainer
from ._autonomous import parse_autonomous
from ._claude import parse_claude
from ._comms import parse_comms, parse_lineage
from ._container import parse_container
from ._delegation import parse_delegation
from ._extensions import parse_extensions
from ._health import parse_health
from ._helpers import (
    DEFAULT_MODEL,
    HOOK_KEYS,
    MODEL_DISPLAY_NAMES,
    MODEL_ENV_KEY,
    get_nested,
    interpolate_metadata,
    resolve_model_surface,
)
from ._hooks import parse_hooks
from ._hosts import parse_hosts_spec, parse_scheduling
from ._listen import parse_listen
from ._mcp import interpolate_mcp_servers
from ._proxy import parse_proxy
from ._restart import parse_restart
from ._skills import parse_skills
from ._watchdog import parse_watchdog

__all__ = [
    "DEFAULT_MODEL",
    "HOOK_KEYS",
    "MODEL_DISPLAY_NAMES",
    "MODEL_ENV_KEY",
    "get_nested",
    "interpolate_mcp_servers",
    "interpolate_metadata",
    "parse_a2a",
    "parse_apptainer",
    "parse_autonomous",
    "parse_claude",
    "parse_comms",
    "parse_container",
    "parse_delegation",
    "parse_extensions",
    "parse_health",
    "parse_hooks",
    "parse_hosts_spec",
    "parse_lineage",
    "parse_listen",
    "parse_proxy",
    "parse_restart",
    "parse_scheduling",
    "parse_skills",
    "parse_watchdog",
    "resolve_model_surface",
]
