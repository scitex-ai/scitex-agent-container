"""``sac.host`` — host / multi-peer verbs as bare names."""

from .._mcp._tools._host import (
    host_exec as exec,
)
from .._mcp._tools._host import (
    host_list as list,
)
from .._mcp._tools._host import (
    host_probe as probe,
)
from .._mcp._tools._host import (
    host_validate as validate,
)

__all__ = ["list", "validate", "probe", "exec"]
