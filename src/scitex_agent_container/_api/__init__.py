"""Noun-grouped Python API shims.

Each submodule re-exports the verb functions for one CLI noun
group, so users can write::

    sac.agent.list()
    sac.db.query(table="instances")
    sac.host.list()

…instead of the flat ``sac.agent_list()`` form. Both shapes work;
the flat names are kept for §6 MCP parity and ecosystem
consistency. Each verb in a submodule is the **same function
object** as the flat top-level one — no aliasing layer that could
drift.

Submodule imports are lazy (PEP 562 ``__getattr__``). Importing this
package would otherwise transitively load every noun's verbs — and
``template`` in particular pulls in ``_mcp._tools._template`` (plus its
fastmcp / a2a transitive deps), which is wasted work for callers that
only want ``sac.agent.list``. Each submodule loads on first attribute
access; ``sac.template.render_contributor_spec`` etc. keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import (  # noqa: F401
        account,
        agent,
        db,
        host,
        image,
        mcp,
        skills,
        template,
    )

__all__ = [
    "agent",
    "db",
    "host",
    "image",
    "account",
    "skills",
    "mcp",
    "template",
]


_SUBMODULES = frozenset(__all__)


def __getattr__(name: str):
    if name in _SUBMODULES:
        import importlib

        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))
