"""``sac.skills`` — skill-introspection verbs as bare names."""

from .._mcp._tools._skills import (
    skills_get as get,
)
from .._mcp._tools._skills import (
    skills_list as list,
)

__all__ = ["list", "get"]
