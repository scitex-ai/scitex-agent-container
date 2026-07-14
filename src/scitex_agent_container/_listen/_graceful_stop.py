"""SIGTERM-only stop primitive — the automatic path may ASK, never destroy.

Why this exists alongside ``_restart._terminate_then_kill``
==========================================================
``_terminate_then_kill`` escalates SIGTERM → SIGKILL after a grace
window. That is correct for ``sac listen restart``: an operator typed
the command, so escalating is *sanctioned destruction*.

The hot-standby loop (:mod:`._standby`) is a different animal. It acts
on a **probe-based inference** that the holder is wedged — and a probe
can be wrong. A 2-second health timeout under load, a momentary stall,
a GC pause: any of these can read as "not answering". The remedy for a
wrong verdict there is to SIGKILL a perfectly healthy control plane and
cut the entire fleet off from the host.

    A false-RED is strictly worse than a false-green: the false-green
    merely fails to act, while the false-RED actively destroys the
    working thing it misdiagnosed.

So the automatic take-over may only ever ASK the holder to leave. If the
holder ignores SIGTERM, the standby loop FAILS LOUD and names the one
destructive command a HUMAN can authorise (``sac listen restart
--force``). No SIGKILL is ever sent on the strength of a probe.

Seams (``_kill`` / ``_sleep`` / ``_alive``) are module-level swappable
callables per PA-306 / STX-NM001-003 — no MagicMock.
"""

from __future__ import annotations

import os
import signal
import time
from typing import Callable

from ._restart import pid_alive

__all__ = ["DEFAULT_POLL_INTERVAL_SECS", "terminate_gracefully"]

DEFAULT_POLL_INTERVAL_SECS: float = 0.2

_kill: Callable[[int, int], None] = os.kill
_sleep: Callable[[float], None] = time.sleep
_alive: Callable[[int], bool] = pid_alive


def terminate_gracefully(
    pid: int,
    *,
    grace_secs: float,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECS,
) -> bool:
    """SIGTERM ``pid``, poll for its exit, and **NEVER escalate**.

    Returns ``True`` iff the process is GONE by the time we stop waiting
    (including the case where it was already gone — that is the goal
    state, not a failure). Returns ``False`` when it ignored SIGTERM and
    is still alive; the caller must then surface a loud, actionable
    failure rather than reach for SIGKILL.

    Bounded by ``grace_secs`` — this must never become an unbounded wait.
    """
    try:
        _kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True  # already gone — the goal state
    except PermissionError:
        # Someone else's process. We cannot stop it and we must not
        # pretend we did; the caller fails loud with the pid.
        return False

    remaining = grace_secs
    step = poll_interval if poll_interval > 0 else DEFAULT_POLL_INTERVAL_SECS
    while remaining > 0:
        _sleep(step)
        remaining -= step
        if not _alive(pid):
            return True
    return not _alive(pid)
