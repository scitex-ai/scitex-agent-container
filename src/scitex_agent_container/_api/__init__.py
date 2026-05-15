"""Noun-grouped Python API shims.

Each submodule re-exports the verb functions for one CLI noun
group, so users can write::

    sac.agent.list()
    sac.db.query(table="instances")
    sac.host.show()

…instead of the flat ``sac.agent_list()`` form. Both shapes work;
the flat names are kept for §6 MCP parity and ecosystem
consistency. Each verb in a submodule is the **same function
object** as the flat top-level one — no aliasing layer that could
drift.
"""

from . import account, agent, db, host, image, mcp, skills, template

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
