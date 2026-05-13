"""Shared CLI helpers: rich console, recursive help group, agent-list formatting.

Thin re-export shim preserving the historical ``cli_pkg._helpers`` public
surface. Every consumer's ``from ._helpers import X`` (or
``from scitex_agent_container.cli_pkg._helpers import X``) keeps working
unchanged.
"""

from __future__ import annotations

from ._agent_list import (
    _discover_defined_agents,
    _extract_damaged_fields,
    _probe_local,
    get_agent_list_data,
    print_agent_list,
    print_agent_list_json,
)
from ._completion import agent_name_complete
from ._console import console, system_msg
from ._groups import CategorizedGroup, HelpRecursiveGroup, renamed_redirect
from ._json_flag import _json_flag

__all__ = [
    "CategorizedGroup",
    "HelpRecursiveGroup",
    "_json_flag",
    "agent_name_complete",
    "console",
    "get_agent_list_data",
    "print_agent_list",
    "print_agent_list_json",
    "renamed_redirect",
    "system_msg",
]
