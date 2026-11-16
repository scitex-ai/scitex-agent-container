"""Wait for a killed tmux session to actually disappear.

``tmux kill-session`` returns before the server has torn the session down.
A fixed sleep before re-checking made :meth:`TmuxManager.stop` report a
failed stop on a loaded host (seen on busy CI runners) although the kill
had worked. Poll ``exists`` until the session is gone or a deadline passes.
"""

from __future__ import annotations

import time
from typing import Callable

SESSION_GONE_TIMEOUT_S = 5.0
SESSION_GONE_INTERVAL_S = 0.1


def wait_session_gone(
    session_name: str,
    *,
    exists: Callable[[str], bool],
    timeout_s: float = SESSION_GONE_TIMEOUT_S,
    interval_s: float = SESSION_GONE_INTERVAL_S,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Return True once ``exists(session_name)`` is False, else False at timeout."""
    deadline = clock() + timeout_s
    while exists(session_name):
        if clock() >= deadline:
            return False
        sleep_fn(interval_s)
    return True
