"""Per-agent advisory fcntl lock for snapshot write/roll.

POSIX fcntl advisory lock; not supported on Windows but container
targets unix. The lock file persists between calls (reusable); the
advisory lock is released when the fd is closed via the ``with`` block.
"""

from __future__ import annotations

import contextlib
import fcntl
from typing import Iterator

from ._paths import _lock_path


@contextlib.contextmanager
def _snapshot_lock(agent: str) -> Iterator[None]:
    """Per-agent advisory lock around the latest->prev roll + write.

    POSIX fcntl advisory lock; not supported on Windows but container
    targets unix. The lock file persists between calls (reusable); the
    advisory lock is released when the fd is closed via the ``with`` block.
    """
    lock_p = _lock_path(agent)
    with open(lock_p, "w") as fd:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            # Released implicitly on close(), but be explicit for clarity.
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
