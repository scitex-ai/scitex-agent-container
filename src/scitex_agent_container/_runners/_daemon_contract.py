"""The residency-contract helpers for :mod:`.session_daemon` (v4 step 5).

Three focused factories/deciders, extracted so ``session_daemon`` stays
under the line cap while remaining the single orchestrator:

* :func:`make_daemon_state_fn` — the per-beat resident state machine
  (``busy | ready | stopping``) the heartbeat loop consults;
* :func:`make_convo_done_callback` — THE ZOMBIE FIX (card
  sac-sdk-runner-stop-never-set-zombie-resident-20260814): folds the
  conversation task's completion into ``stop`` and records the honest
  exit cause, so a driver that returns or dies on its own ends the
  daemon instead of leaving a resident zombie with green heartbeats;
* :func:`resolve_exit` — the (reason, code) the terminal ExitRecord
  carries, first recorded cause winning, with honest fallbacks.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable

from ._incarnation import (
    EXIT_CRASHED,
    EXIT_HARNESS_RETURNED,
    EXIT_ONESHOT_COMPLETE,
    EXIT_STOPPED_BY_SIGNAL,
    WRITER_TURN_DRIVER,
    ExitReasonHolder,
    try_bind_incarnation,
)

logger = logging.getLogger(__name__)

__all__ = [
    "make_convo_done_callback",
    "make_daemon_state_fn",
    "resolve_exit",
]


def make_daemon_state_fn(
    state_dir: Path,
    *,
    stop: asyncio.Event,
    convo_ref: dict,
) -> Callable[[], str]:
    """Build the per-beat resident-state decider: busy | ready | stopping.

    READY asserts the residency contract — an inbox consumer is attached
    (or none is required) — which is what the old blanket IDLE could not
    say. BUSY is preserved when the turn driver's own busy beat is the
    latest self-testimony (a turn is in flight; the periodic loop must
    not overwrite it). A done conversation task reads STOPPING: its
    done-callback is already folding that into ``stop``, and this beat
    must not vouch READY over a consumer that no longer exists.

    Each tick also re-attempts the incarnation bind (see
    :mod:`._incarnation`) — the start path publishes the ``instance_id``
    marker a moment after the daemon boots, and the bind-once cache
    makes the retries free after adoption.
    """
    from ._session_beat import STATE_BUSY, STATE_READY, STATE_STOPPING, read_heartbeat

    def _daemon_state() -> str:
        try_bind_incarnation(state_dir)
        if stop.is_set():
            return STATE_STOPPING
        task = convo_ref.get("task")
        if task is not None and task.done():
            return STATE_STOPPING
        if task is not None:
            prev = read_heartbeat(state_dir)
            if (
                prev is not None
                and prev.get("state") == STATE_BUSY
                and prev.get("writer") == WRITER_TURN_DRIVER
            ):
                return STATE_BUSY
        return STATE_READY

    return _daemon_state


def make_convo_done_callback(
    *,
    name: str,
    stop: asyncio.Event,
    exit_cause: ExitReasonHolder,
    oneshot_mode: bool,
) -> Callable[[asyncio.Task], None]:
    """THE ZOMBIE FIX — the conversation ending is a daemon event.

    Nothing used to set ``stop`` when the turn driver returned or died
    on its own, so the daemon stayed parked on ``stop.wait()`` as a
    resident zombie: green heartbeats, a bound a2a port, and no inbox
    consumer — every incoming turn died at the 120s timeout while every
    liveness proxy read alive. Completion now folds into ``stop`` and
    records the honest cause. A completion that arrives DURING a planned
    shutdown (``stop`` already set — the ShutdownEnvelope drain, a
    signal) is exactly that plan, not a violation; ``oneshot_mode``
    (a ``--print-stream`` foreground mission) makes a clean return the
    PLANNED end rather than a residency breach.
    """

    def _on_convo_done(task: asyncio.Task) -> None:
        if stop.is_set():
            return
        if task.cancelled():
            logger.error(
                "runner %s: conversation task was CANCELLED outside shutdown — "
                "stopping the daemon (no inbox consumer remains)",
                name,
            )
            exit_cause.set_once(EXIT_CRASHED, 1)
        else:
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "runner %s: conversation task DIED (%r) — stopping the "
                    "daemon (no inbox consumer remains)",
                    name,
                    exc,
                )
                exit_cause.set_once(EXIT_CRASHED, 1)
            elif oneshot_mode:
                exit_cause.set_once(EXIT_ONESHOT_COMPLETE, 0)
            else:
                logger.error(
                    "runner %s: conversation task RETURNED while the daemon "
                    "was resident — stopping (reason=harness-returned); a "
                    "resident daemon's driver must only return on shutdown",
                    name,
                )
                exit_cause.set_once(EXIT_HARNESS_RETURNED, 1)
        stop.set()

    return _on_convo_done


def resolve_exit(
    exit_cause: ExitReasonHolder,
    convo_ref: dict,
) -> tuple[str, int]:
    """The (reason, code) this daemon ends with — first cause wins.

    Falls back honestly when no path recorded a cause: a cleanly
    finished driver means the driver itself initiated the end (an
    ``exit_after`` turn that set ``stop`` before returning) → one-shot;
    a dead driver means crashed; otherwise something signal-shaped
    stopped us.
    """
    if exit_cause.reason is not None:
        return exit_cause.reason, exit_cause.code
    task = convo_ref.get("task")
    if task is not None and task.done() and not task.cancelled():
        if task.exception() is not None:
            return EXIT_CRASHED, 1
        return EXIT_ONESHOT_COMPLETE, 0
    return EXIT_STOPPED_BY_SIGNAL, 0
