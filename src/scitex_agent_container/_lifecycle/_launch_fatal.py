#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Did apptainer refuse to create the container for THIS launch?

Extracted from :mod:`._launch_verify` (512-line cap) because it answers a
different question from the rest of that module. ``_launch_verify`` weighs
evidence about whether an agent came up; this reads a boot log and decides
whether apptainer said it never started. The rules here — which files to
look at, how to tell this launch's log from the previous launch's, what
filesystem mtime granularity does to that comparison — are self-contained
and have nothing to do with heartbeats or poll windows.

WHY THIS EXISTS AT ALL
======================
Operator instruction, 2026-08-20:

    「アップテーナーのフェイタルを握り潰さないで、再テックスロギングで
      つないでください。で、うるさく失敗させてください」

Do not swallow apptainer's FATAL; route it through scitex-logging; fail
loudly. The reporting half already did that — ``cli_pkg.lifecycle.
_start_verify_report`` sends every negative verdict through ``system_msg``
and returns False so the caller exits non-zero. What was missing was any
path that REACHED it: the verdict loop returned ``verified-up`` the moment
it saw a fresh heartbeat, and never looked at the boot log.

WHY A FATAL OUTRANKS A HEARTBEAT
================================
A FATAL is positive evidence that the container was never created. A beat
is weaker evidence of success than a FATAL is of failure, and measurably
so: of ~118 heartbeats across the fleet on 2026-08-19, all but one were
written by ``listen-tui-observer`` rather than by the runner itself. So a
beat can be an observer's write about a container that does not exist,
while a FATAL can only have been produced by apptainer refusing to make
one.

BLAST RADIUS, measured before shipping
======================================
50 boot logs across four hosts on 2026-08-20: ZERO contain a FATAL. So
this check changes no start that happens today; it fires only when
apptainer actually refuses. That measurement is why this fix shipped when
three earlier candidates did not — they would have refused 83 of 121 bind
sources, 29 of 29 heartbeats, and 117 of 118 launches respectively.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["FATAL_PREFIX", "MTIME_SLACK_S", "apptainer_fatal"]

#: apptainer prefixes its fatal diagnostics with this, at line start.
FATAL_PREFIX = "FATAL"

#: Slack on the "was this log written by THIS launch" comparison.
#:
#: MEASURED, not guessed. The first version compared ``st_mtime`` against
#: ``launched_at`` directly and the tests went red with the verdict still
#: ``verified-up`` — a FATAL written milliseconds after launch was being
#: skipped as stale. ``launched_at`` carries sub-second precision from
#: ``time.time()`` while a filesystem mtime can be coarser, so the log of
#: the very launch being verified can stamp BELOW its own launch instant.
#:
#: Two seconds absorbs that granularity and nothing more: a genuinely
#: stale FATAL comes from a previous start, which is minutes or hours old,
#: so widening the guard by two seconds does not weaken it.
MTIME_SLACK_S = 2.0


def apptainer_fatal(
    state_dir: Path,
    names: tuple[str, ...],
    *,
    launched_at: float,
) -> tuple[Path, str] | None:
    """The apptainer FATAL line from THIS launch, or ``None``.

    Returns the log file and the first ``FATAL...`` line in it.

    THE FRESHNESS CHECK IS NOT OPTIONAL
    ===================================
    ``boot.stderr.log`` is not truncated per launch, so a FATAL from a
    start that failed yesterday is still on disk today. Without it this
    function would fail every subsequent start of an agent that once
    failed — converting silent success into permanent silent failure,
    which is worse than the defect it fixes. Only a log modified at or
    after ``launched_at`` (less :data:`MTIME_SLACK_S`) testifies about
    this launch.

    A file that cannot be read is treated as "no FATAL found", never as a
    failure. This is one input to a verdict, and an unreadable log must
    not manufacture a failure on its own — the caller's DEAD-process probe
    and window expiry still decide.
    """
    floor = launched_at - MTIME_SLACK_S
    for name in names:
        candidate = state_dir / name
        if not candidate.is_file():
            continue
        try:
            if candidate.stat().st_mtime < floor:
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:  # stx-allow: fallback (reason: an unreadable log is absence of evidence, never evidence of failure; the caller's DEAD probe and window expiry still decide)
            continue
        for line in text.splitlines():
            if line.startswith(FATAL_PREFIX):
                return candidate, line.strip()
    return None
