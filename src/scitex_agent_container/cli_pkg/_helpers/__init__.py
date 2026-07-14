"""Shared CLI helpers: rich console, recursive help group, agent-list formatting.

Thin re-export shim preserving the historical ``cli_pkg._helpers`` public
surface. Every consumer's ``from ._helpers import X`` (or
``from scitex_agent_container.cli_pkg._helpers import X``) keeps working
unchanged.

Submodule imports are lazy (PEP 562 ``__getattr__``). The CLI cold-start
path imports this package as a side-effect of ``from ._helpers._groups
import HelpRecursiveGroup`` in ``_lazy_group.py``; pulling ``_agent_list``
eagerly here transitively loaded ``Registry`` + ``config.load_config`` +
``rich.table`` (~100 ms) on every ``sac --help`` / tab-completion press,
blowing the 500 ms startup budget. Keep this shim lean.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._agent_list import (  # noqa: F401
        _discover_defined_agents,
        _extract_damaged_fields,
        _probe_local,
        get_agent_list_data,
        is_live_status,
        print_agent_list,
        print_agent_list_json,
    )
    from ._completion import agent_name_complete  # noqa: F401
    from ._console import console, system_msg  # noqa: F401
    from ._groups import (  # noqa: F401
        CategorizedGroup,
        HelpRecursiveGroup,
        renamed_redirect,
    )
    from ._json_flag import _json_flag  # noqa: F401

__all__ = [
    "CategorizedGroup",
    "HelpRecursiveGroup",
    "_json_flag",
    "agent_name_complete",
    "console",
    "get_agent_list_data",
    "is_live_status",
    "print_agent_list",
    "print_agent_list_json",
    "renamed_redirect",
    "system_msg",
]

# Lazy attribute → submodule map. PEP 562 ``__getattr__`` resolves each
# name on first access and caches the result in module globals so
# subsequent accesses are plain dict lookups.
_LAZY_ATTR_SOURCES = {
    "_discover_defined_agents": "._agent_list",
    "_extract_damaged_fields": "._agent_list",
    "_probe_local": "._agent_list",
    "get_agent_list_data": "._agent_list",
    "is_live_status": "._agent_list",
    "print_agent_list": "._agent_list",
    "print_agent_list_json": "._agent_list",
    "agent_name_complete": "._completion",
    "console": "._console",
    "system_msg": "._console",
    "CategorizedGroup": "._groups",
    "HelpRecursiveGroup": "._groups",
    "renamed_redirect": "._groups",
    "_json_flag": "._json_flag",
}


def __getattr__(name: str):
    source = _LAZY_ATTR_SOURCES.get(name)
    if source is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(source, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))
