"""Keep a deadline-exceeded spawn alive, and record its outcome anyway.

When ``POST /agents`` hits its declared answer-by deadline
(:mod:`._handler_deadline`) the handler answers ``202 Accepted`` — but the
launch it started is STILL RUNNING and must not be disturbed. Two things then
have to be true, and neither is automatic:

1. THE LAUNCH MUST NOT BE CANCELLED. ``asyncio.wait_for`` cancels its inner
   awaitable on timeout, which is the opposite of what we want here — the whole
   claim of the 202 is "your spawn is in flight". The handler therefore wraps
   the launch in ``asyncio.shield``; this module owns what happens next.

2. THE OUTCOME MUST STILL BE OBSERVABLE. A caller that got a 202 polls
   ``GET /agents/<name>/status``. For a spawn that later FAILS, that route is
   only informative because something wrote a ``STARTUP_FAILED`` marker — and
   on the synchronous path the handler writes it. Detach without this module
   and a post-202 failure would leave the agent merely "not running", with the
   rc and stderr that explain WHY discarded when the task was garbage
   collected. The 202 would then have traded one silence for another.

There is a third, quieter reason: an abandoned ``asyncio.Task`` whose exception
is never retrieved is collected with a "Task exception was never retrieved"
warning and the error is gone. Holding a strong reference until the done
callback runs is what makes the failure reportable at all.
"""

from __future__ import annotations

import asyncio
from typing import Any

__all__ = ["detach_launch", "inflight_count"]


# Strong references to launches that outlived their handler's deadline. Without
# this, the event loop holds only a weak reference and a still-running spawn can
# be garbage collected mid-flight — taking its exception (and any chance of
# writing the failure marker) with it. Entries are removed by the done callback.
_INFLIGHT: set[asyncio.Task] = set()


def inflight_count() -> int:
    """Number of launches currently outliving their handler (observability)."""
    return len(_INFLIGHT)


def _write_launch_marker(
    name: str,
    *,
    started_at: str,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> None:
    """Best-effort ``STARTUP_FAILED`` write for a launch that failed AFTER 202.

    Mirrors the synchronous path's marker exactly (same ``phase``, same
    fields) so a caller polling ``GET /agents/<name>/status`` cannot tell
    whether the failure was reported inside the deadline or after it — the
    diagnostic is identical either way. That equivalence is the point: the
    202 must not be a degraded mode.
    """
    try:
        from .._lifecycle._startup_failed import write_marker
        from .._runners._session_state import state_dir_for

        write_marker(
            state_dir_for(name),
            started_at=started_at,
            phase="container_creation",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )
    except Exception:  # stx-allow: fallback (reason: the marker is observability for an already-answered request; a write failure must not raise inside a done callback, where it would be swallowed by the loop anyway)
        pass


def _on_launch_done(task: "asyncio.Task", *, name: str, started_at: str) -> None:
    """Done callback: drop the strong ref, then record a failing outcome.

    A SUCCESSFUL launch needs nothing written — the agent's own liveness is the
    record, and that is exactly what ``GET /agents/<name>/status`` reports. Only
    failure needs a marker, because a failed spawn is otherwise indistinguishable
    from an agent that was never asked to start.
    """
    _INFLIGHT.discard(task)
    if task.cancelled():
        # Should not happen (the launch is shielded), but a cancelled task has
        # no result to inspect and calling .result() would raise here.
        return
    exc = task.exception()
    if exc is not None:
        _write_launch_marker(
            name,
            started_at=started_at,
            exit_code=-1,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
        )
        return
    proc: Any = task.result()
    returncode = getattr(proc, "returncode", None)
    if returncode not in (0, None):
        _write_launch_marker(
            name,
            started_at=started_at,
            exit_code=int(returncode),
            stdout=getattr(proc, "stdout", "") or "",
            stderr=getattr(proc, "stderr", "") or "",
        )


def detach_launch(task: "asyncio.Task", *, name: str, started_at: str) -> None:
    """Adopt ``task`` so it survives the handler that started it.

    Called ONLY when the handler has already decided to answer 202. Holds a
    strong reference until completion, then records a failing outcome via
    :func:`_write_launch_marker` so the caller's follow-up
    ``GET /agents/<name>/status`` carries the same diagnostic it would have
    carried on the synchronous path.
    """
    _INFLIGHT.add(task)
    task.add_done_callback(
        lambda t: _on_launch_done(t, name=name, started_at=started_at)
    )
