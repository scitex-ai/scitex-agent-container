"""Keep a deadline-exceeded spawn alive, and record its outcome anyway.

When ``POST /agents`` hits its declared answer-by deadline
(:mod:`._handler_deadline`) the handler answers ``202 Accepted`` — but the
launch it started is STILL RUNNING and must not be disturbed. Three things then
have to be true, and none of them is automatic:

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

3. rc == 0 IS NOT A SUCCESS REPORT — AND THAT HOLE SWALLOWED A WHOLE LAUNCH.
   Measured 2026-09-06 on scitex-compute-04: ``sac agents start dotfiles``
   brokered from a container answered 202 at 07:23:13 and then NOTHING
   happened — no tmux session, no marker, no log line, not one file written
   under the agent's state dir, for six minutes. Host forensics showed the
   boot gate was taken at 07:22:43 and released normally, so the launch RAN
   TO COMPLETION and this module's done callback DID fire; it simply took the
   branch below that writes nothing, because the child exited 0 having started
   nothing. The same command run on the host self-verified first try.

   The asymmetry is the defect. On the SYNCHRONOUS path an rc=0 launch is not
   believed on its word: ``_agent_exec`` runs
   :func:`._agent_exec_liveness._probe_post_ack_liveness`, which for the
   fleet's default ``tui`` runtime asks the agent's OWN runtime whether a
   session exists and, on a POSITIVELY OBSERVED absence, writes
   ``post_ack_session_absent`` + answers 502. That probe is unreachable once
   the handler has answered 202, so the 202 path trusted an exit code that the
   200 path explicitly refuses to trust. This module now runs the same probe on
   the same budget after a detached launch, which is what makes the module
   docstring's own claim below — "the 202 must not be a degraded mode" — true
   rather than aspirational.

There is a fourth, quieter reason: an abandoned ``asyncio.Task`` whose exception
is never retrieved is collected with a "Task exception was never retrieved"
warning and the error is gone. Holding a strong reference until the done
callback runs is what makes the failure reportable at all.

WHY EVERY LINE HERE IS ``WARNING`` AND NOT ``INFO``
---------------------------------------------------
Measured on the interpreter this fleet runs: uvicorn's ``LOGGING_CONFIG``
defines handlers for ``uvicorn`` / ``uvicorn.error`` / ``uvicorn.access`` and NO
root logger, with ``disable_existing_loggers`` false. A library ``INFO`` record
from sac therefore reaches the journal only through ``logging.lastResort``,
whose level is ``WARNING`` — i.e. it goes nowhere at all. Logging the detached
path at ``INFO`` would look like fixing the silence while reproducing it. A
brokered launch that outran its deadline is an exceptional event by
construction (it blew a 30 s budget), so ``WARNING`` is also the honest level
for it, not merely the reachable one.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

__all__ = ["detach_launch", "inflight_count"]

logger = logging.getLogger(__name__)

# Headroom added to the probe's own budget when dispatching it off the loop.
# ``run_blocking`` needs a ceiling STRICTLY GREATER than the work it bounds, or
# it would abandon a probe that was about to answer and report UNKNOWN — a
# self-inflicted blind spot in the very check that exists to remove one.
_PROBE_DISPATCH_MARGIN_S = 5.0

# Strong references to launches that outlived their handler's deadline, plus the
# follow-up verification tasks they spawn. Without this, the event loop holds
# only a weak reference and a still-running spawn can be garbage collected
# mid-flight — taking its exception (and any chance of writing the failure
# marker) with it. Entries are removed by the done callbacks.
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
    phase: str = "container_creation",
    kind_override: str | None = None,
) -> None:
    """Best-effort ``STARTUP_FAILED`` write for a launch that failed AFTER 202.

    Mirrors the synchronous path's marker exactly (same ``phase``, same
    ``kind_override``, same fields) so a caller polling
    ``GET /agents/<name>/status`` cannot tell whether the failure was reported
    inside the deadline or after it — the diagnostic is identical either way.
    That equivalence is the point: the 202 must not be a degraded mode.

    A marker that could not be written (or that ``write_marker`` deliberately
    declines to write, e.g. an operator's ``--yes``-less refusal) is LOGGED
    rather than dropped: the whole failure mode this module now guards against
    is an outcome that left no trace anywhere.
    """
    try:
        from .._lifecycle._startup_failed import write_marker
        from .._runners._session_state import state_dir_for

        target = write_marker(
            state_dir_for(name),
            started_at=started_at,
            phase=phase,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            kind_override=kind_override,
        )
    except Exception as exc:  # stx-allow: fallback (reason: the marker is observability for an already-answered request; a write failure must not raise inside a done callback, where the loop would swallow it. Not dropped: the line below goes to the daemon's stderr, which systemd captures into `journalctl -u sac-listen` at WARNING, which is the sink the whole module exists to reach.)
        logger.warning(
            "spawn_detach: could NOT write the STARTUP_FAILED marker for %r "
            "(phase=%s kind=%s): %s: %s. The failure below is the only record.",
            name,
            phase,
            kind_override,
            type(exc).__name__,
            exc,
        )
        return
    if target is None:
        logger.warning(
            "spawn_detach: no STARTUP_FAILED marker written for %r (phase=%s) "
            "— write_marker declined it (start was refused without --yes). "
            "Recording it here so the outcome is not silent.",
            name,
            phase,
        )


def _tail(text: str, limit: int = 400) -> str:
    """Last ``limit`` characters of ``text``, single-line, for a log record."""
    flattened = " ".join((text or "").split())
    if len(flattened) <= limit:
        return flattened
    return "..." + flattened[-limit:]


async def _verify_post_ack(name: str, *, started_at: str, proc: Any) -> None:
    """Run the SAME post-ack liveness probe the synchronous path runs.

    Reached only for a detached launch that exited 0. ``rc == 0`` from
    ``sac agents start`` means "the wrapper returned without error", NOT "an
    agent is running" — the background branch is a ``Popen`` that reports
    success the instant it forks, and a child can also return 0 having decided
    to start nothing at all. Believing it is precisely the "Popen + rc=0
    immediately" lie that Layer-3 fail-loud exists to catch, and the 202 path
    had re-opened it.

    UNKNOWN CONVICTS NOTHING. The probe returns a failure only on a POSITIVELY
    OBSERVED absence; an unresolvable spec, a wedged tmux or a probe that
    outran its dispatch ceiling are all "we could not tell", and stamping
    ``startup_failed`` on those would hand an operator a death verdict whose
    remedy (``--force --fresh``) destroys a healthy agent. Those cases are
    LOGGED instead, which is the whole difference from before: a launch whose
    outcome we cannot determine now says so, out loud, in the journal.
    """
    try:
        from .._lifecycle._off_loop import run_blocking
        from .._runners._session_state import state_dir_for
        from ._agent_exec_liveness import (
            _probe_post_ack_liveness,
            post_ack_timeout_from_env,
        )

        budget = post_ack_timeout_from_env()
        if budget <= 0.0:
            # Operator/suite disabled the probe. Honour it on BOTH paths, or
            # the two would disagree about what a missing marker means.
            return
        runtime_dir = state_dir_for(name)

        def post_ack_liveness_probe() -> tuple[str, str] | None:
            return _probe_post_ack_liveness(
                runtime_dir, name=name, timeout_s=budget
            )

        try:
            failure = await run_blocking(
                post_ack_liveness_probe,
                timeout_s=budget + _PROBE_DISPATCH_MARGIN_S,
            )
        except asyncio.TimeoutError:
            # The probe itself wedged. It observed nothing, so it convicts
            # nothing — but a launch we cannot verify must not pass in silence.
            logger.warning(
                "spawn_detach: post-ack liveness probe for %r did not return "
                "within %.1fs; its outcome is UNKNOWN and no startup_failed "
                "marker was written. Poll /agents/%s/status and check the "
                "runtime dir by hand.",
                name,
                budget + _PROBE_DISPATCH_MARGIN_S,
                name,
            )
            return
        if failure is None:
            logger.warning(
                "spawn_detach: detached launch of %r verified — its runtime "
                "reports a live session within %.1fs of the rc=0 exit.",
                name,
                budget,
            )
            return
        kind, hint = failure
        logger.warning(
            "spawn_detach: detached launch of %r exited 0 but STARTED NOTHING "
            "(%s). Writing a startup_failed marker so GET /agents/%s/status — "
            "the route the 202 told the caller to poll — can say so. %s",
            name,
            kind,
            name,
            hint,
        )
        _write_launch_marker(
            name,
            started_at=started_at,
            phase="post_ack_liveness",
            exit_code=0,
            stdout=getattr(proc, "stdout", "") or "",
            stderr=(getattr(proc, "stderr", "") or "")
            + f"\n\n[listen post-ack liveness probe] {kind}: {hint}\n",
            kind_override=kind,
        )
    except Exception as exc:  # stx-allow: fallback (reason: this runs in a detached task for an already-answered request; it must never escape as an unretrieved-task exception. The line below reaches the daemon's stderr, which systemd captures into `journalctl -u sac-listen` at WARNING, which is the entire point of the module.)
        logger.warning(
            "spawn_detach: post-ack verification of %r blew up (%s: %s). The "
            "launch's outcome is UNRECORDED; poll /agents/%s/status.",
            name,
            type(exc).__name__,
            exc,
            name,
        )


def _on_verify_done(task: "asyncio.Task") -> None:
    """Drop the strong ref on a finished verification task."""
    _INFLIGHT.discard(task)


def _schedule_post_ack_verification(
    name: str, *, started_at: str, proc: Any
) -> None:
    """Kick the blocking liveness probe OFF the done-callback's thread.

    A done callback runs ON the event loop, and the probe blocks for up to the
    full grace window. Running it inline would stall uvicorn for twenty seconds
    per detached launch — the exact class of loop-blocking bug
    :mod:`.._lifecycle._off_loop` was written to make impossible — so the work
    is handed to a task that dispatches it through that bounded, dedicated-
    thread helper.
    """
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            _verify_post_ack(name, started_at=started_at, proc=proc)
        )
    except RuntimeError as exc:  # stx-allow: fallback (reason: the loop is gone or closing — e.g. the daemon is shutting down; nothing can be scheduled, and RAISING here would surface as an opaque "Exception in callback". The line below reaches the daemon's stderr, which systemd captures into `journalctl -u sac-listen` at WARNING instead.)
        logger.warning(
            "spawn_detach: no usable event loop to verify the detached launch "
            "of %r (%s); its rc=0 exit is UNVERIFIED.",
            name,
            exc,
        )
        return
    _INFLIGHT.add(task)
    task.add_done_callback(_on_verify_done)


def _on_launch_done(task: "asyncio.Task", *, name: str, started_at: str) -> None:
    """Done callback: drop the strong ref, then RECORD the outcome.

    Every terminal outcome now leaves a trace. A failing rc writes the marker as
    before; an rc of 0 is handed to :func:`_verify_post_ack`, because "the
    wrapper returned 0" and "an agent is running" are different claims and this
    callback used to treat them as the same one — which is how a launch
    disappeared without a single byte written anywhere (see the module
    docstring).
    """
    _INFLIGHT.discard(task)
    if task.cancelled():
        # Should not happen (the launch is shielded), but a cancelled task has
        # no result to inspect and calling .result() would raise here.
        logger.warning(
            "spawn_detach: the detached launch of %r was CANCELLED — it is "
            "shielded, so this should be unreachable. Outcome unknown.",
            name,
        )
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "spawn_detach: the detached launch of %r raised %s: %s",
            name,
            type(exc).__name__,
            exc,
        )
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
    logger.warning(
        "spawn_detach: detached launch of %r finished rc=%s (stderr tail: %s)",
        name,
        returncode,
        _tail(getattr(proc, "stderr", "") or "") or "<empty>",
    )
    if returncode not in (0, None):
        _write_launch_marker(
            name,
            started_at=started_at,
            exit_code=int(returncode),
            stdout=getattr(proc, "stdout", "") or "",
            stderr=getattr(proc, "stderr", "") or "",
        )
        return
    _schedule_post_ack_verification(name, started_at=started_at, proc=proc)


def detach_launch(task: "asyncio.Task", *, name: str, started_at: str) -> None:
    """Adopt ``task`` so it survives the handler that started it.

    Called ONLY when the handler has already decided to answer 202. Holds a
    strong reference until completion, then records the outcome via
    :func:`_on_launch_done` so the caller's follow-up
    ``GET /agents/<name>/status`` carries the same diagnostic it would have
    carried on the synchronous path.
    """
    _INFLIGHT.add(task)
    logger.warning(
        "spawn_detach: the launch of %r outran the handler's deadline; "
        "answering 202 and adopting it (started_at=%s, in flight=%d). Its "
        "outcome will be logged here and, on failure, written to the agent's "
        "STARTUP_FAILED marker.",
        name,
        started_at,
        len(_INFLIGHT),
    )
    task.add_done_callback(
        lambda t: _on_launch_done(t, name=name, started_at=started_at)
    )
