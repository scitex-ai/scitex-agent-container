"""``sac.db`` — instance-registry verbs as bare names.

``show`` / ``query`` / ``export`` / ``import_`` were deleted on 2026-08-29
with the SQLite read surface they wrapped; the CLI commands behind them are
gone, so re-exporting the names would only have promised verbs that raise.
"""

from .._mcp._tools._db import (
    db_clean as clean,
)
from .._mcp._tools._db import (
    db_migrate as migrate,
)
from .._mcp._tools._db import (
    db_tick as tick,
)

__all__ = ["clean", "tick", "migrate"]
