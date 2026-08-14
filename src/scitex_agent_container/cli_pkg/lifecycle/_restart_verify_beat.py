#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The BEAT witness for the restart postcondition (v4 step 5).

Sibling of :mod:`._restart_verify` (which owns the ternary rule and the
tmux witness; this split keeps both under the line cap). An SDK agent
has no multiplexer session to ask about — ``instances.screen`` is NULL —
so the two-witness check could only ever abstain for it. The runner's
own heartbeat closes that gap: the ``incarnation_id`` on a beat is the
one the runner process BOUND AT ITS OWN BOOT (bind-once, stale markers
refused, rewrites ignored — see ``_runners._incarnation``), so a fresh
beat naming the new incarnation is a live process's own testimony that
it took up that run, while an untouched old process keeps beating its
OLD incarnation no matter how many ids the ledger mints over it. That
independence from the start path's writes is what makes this a WITNESS
rather than a third echo.
"""

from __future__ import annotations

from typing import Callable

from ._restart_verify import SessionObservation

__all__ = ["read_beat_identity"]


def _exit_record_citation(name: str) -> str:
    """One sentence citing the last incarnation's ExitRecord, or ``""``.

    Rides on a blind beat observation so a caller staring at "cannot
    verify" at least learns HOW the previous run ended — e.g.
    ``reason='harness-returned'`` is the daemon telling you its
    conversation task died out from under it.
    """
    from ..._lifecycle._session_reset import _runtime_state_dir
    from ..._runners._incarnation import read_exit_record

    rec = read_exit_record(_runtime_state_dir(name))
    if not rec:
        return ""
    return (
        f" The last incarnation's ExitRecord says reason={rec.get('reason')!r} "
        f"code={rec.get('code')} (incarnation {rec.get('incarnation_id')}, "
        f"ts {rec.get('ts')})"
    )


def read_beat_identity(
    name: str,
    *,
    min_ts: float | None = None,
    wait_s: float = 0.0,
    poll_interval_s: float = 0.5,
    now_fn: Callable[[], float] = None,  # type: ignore[assignment]
    sleep_fn: Callable[[float], None] = None,  # type: ignore[assignment]
) -> SessionObservation:
    """The RUNNER'S OWN testimony: which incarnation is beating right now?

    Reads ``heartbeat.json`` through the same runtime-dir resolver as
    :func:`._restart_verify.read_run_identity` and reports the beat's
    ``incarnation_id`` + ``pid`` as the observation identity
    (``beat:<incarnation>@pid<pid>``).

    Blindness stays blindness, each with its reason: no beat file (an
    instant abstention — nothing suggests a runner ever lived here, so
    polling would only tax every non-SDK restart), a beat with no
    incarnation (an observer/proxy beat, or a pre-artifact runner), or —
    when ``min_ts`` is given — no beat stamped at/after it (the new
    runner has not spoken yet; the previous ExitRecord is cited when one
    exists). ``wait_s`` bounds an optional poll for the stale case: the
    runner adopts its incarnation on its next tick after the start path
    publishes the marker, so the after-restart witness is worth a short
    wait rather than an instant abstention.

    ``now_fn``/``sleep_fn`` are injection seams (real callables) so the
    suite drives the wait deterministically.
    """
    import time as _time

    now_fn = now_fn or _time.time
    sleep_fn = sleep_fn or _time.sleep
    from ..._lifecycle._session_reset import _runtime_state_dir
    from ..._runners._session_state import read_heartbeat

    state_dir = _runtime_state_dir(name)
    deadline = now_fn() + max(0.0, wait_s)
    blind = ""
    while True:
        beat = read_heartbeat(state_dir)
        if beat is None:
            return SessionObservation(
                False,
                None,
                f"no heartbeat.json exists for {name!r} — either no runner "
                f"ever booted in this runtime dir, or it is not this "
                f"process's to read",
            )
        incarnation = beat.get("incarnation_id")
        ts = beat.get("ts")
        fresh = isinstance(ts, (int, float)) and (
            min_ts is None or float(ts) >= float(min_ts)
        )
        if incarnation and fresh:
            return SessionObservation(
                True, f"beat:{incarnation}@pid{beat.get('pid')}", ""
            )
        if not incarnation:
            blind = (
                f"the latest beat for {name!r} carries no incarnation_id "
                f"(an observer/proxy beat, or a runner predating the v4 "
                f"liveness artifact) — the runner's own testimony is not "
                f"on file"
            )
        else:
            blind = (
                f"no self-testimony beat for {name!r} has landed since "
                f"the restart began — the latest beat predates it, so "
                f"the new runner has not spoken yet."
                f"{_exit_record_citation(name)}"
            )
        if now_fn() >= deadline:
            return SessionObservation(False, None, blind)
        sleep_fn(poll_interval_s)
