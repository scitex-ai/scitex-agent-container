#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_state/state_db_gc.py
"""Garbage-collection sweep for ``instances`` records.

On the shared PostgreSQL store since 2026-08-28 (:mod:`.state_db_instances`);
``db_path`` is gone from the signature because there is no file. Re-exported
by :mod:`state_db` so external callers keep the same import path.

AN ``exit_reason`` NAMES THE CHECK, NOT THE FATE
------------------------------------------------
This sweep does not witness anything die. It runs ``os.kill(pid, 0)`` at some
arbitrary later moment and writes a value for every pid that is no longer
there. That is a **single observation of absence**, and it supports exactly
one claim: *this pid was not present when we looked.*

It used to write that as ``exit_reason='crashed'``, which asserts a cause the
check never established, and it paired it with ``ended_at=now()``, which
reads as a time of death the check never measured. Both were believed.

MEASURED 2026-08-12: eleven agents on the fleet host carried
``ended_at=2026-08-11T17:54:26Z, exit_reason='crashed'``. Three separate
readers — including the author of this docstring — took the identical second
across eleven rows as proof of a simultaneous kill, and reasoned about what
could kill eleven processes at once. Nothing did. They had died **10h46m
earlier**, over a two-second window when the host's tmux server went away, and
``17:54:26Z`` was simply :func:`now_iso` evaluated ONCE (see ``now_ts`` below)
and stamped onto every record the loop reaped. An identical timestamp across
N records is the EXPECTED output of one sweep; it is not evidence of anything
about the agents.

So the value is now :data:`EXIT_REASON_PID_ABSENT_AT_SWEEP` — a name that
states the check that ran and, by saying *at sweep*, warns that the
accompanying ``ended_at`` is the moment we LOOKED, not the moment it died. The
old value is still accepted on read, because records written before this
change exist and mean the same thing.

The general rule, which outlives this module: **a field whose name asserts
more than its check performed will be believed at its name.**

THE SHARED STORE MADE ONE BRANCH FLEET-WIDE. IT IS SCOPED NOW.
==============================================================
Under per-host SQLite the heartbeat-staleness branch scoped ONLY by
``remote=0``, and that was sufficient by accident: each host owned its own
file, so "not a cross-host mirror row" also meant "written here". On the
SHARED store it means neither — a peer's own local record is ``remote=0``
too, and every host can see it. One host running ``sac db tick`` would then
have tombstoned LIVE agents fleet-wide, using a heartbeat freshness rule
against agents whose heartbeats it has no business judging, and nothing would
have raised: the victims' rows simply stop being returned by
``list_active_instances``, which every reader takes as "not running".

So the branch now carries ``host == canonical_host`` as well. All three
branches are host-scoped, which makes "a host only ever sweeps its own
observations" true BY CONSTRUCTION rather than by which file was open.

WHAT THIS SWEEP DOES NOT DO: ``hide()``
=======================================
It writes ``ended_at``/``exit_reason`` — the FIRST fill of two IMMUTABLE
fields, which the store permits because immutability starts once there is a
value. It never hides. Hiding is reserved for a deliberate operator
withdrawal, so "this agent existed and its process was found gone" and "this
agent was never recorded here" stay different answers. A tombstoned record is
still returned by :func:`.state_db_instances.last_known_instance`, which is
what ``_reconcile/_rule`` and ``_restart_verify`` read as evidence.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from .state_db_instances import end_instance, list_active_instances

#: What the pid-liveness branch writes: ``os.kill(pid, 0)`` raised ESRCH at
#: sweep time. It says nothing about WHEN the process left, WHY it left, or
#: whether anything was wrong — a deliberately-killed agent, an OOM victim and
#: a clean exit that failed to record itself are indistinguishable here.
EXIT_REASON_PID_ABSENT_AT_SWEEP = "pid_absent_at_sweep"

#: The value this branch wrote before 2026-08-12. Retained as a READ alias
#: only: live databases hold these records, they mean exactly what the new
#: name says, and silently reclassifying them would strand real corpses.
#: Never write it.
LEGACY_EXIT_REASON_CRASHED = "crashed"


def _proc_btime() -> str | None:
    """Return Linux boot time as ISO-8601 UTC, or None on non-Linux.

    Used by ``gc_dead_instances`` to mark every instance whose
    ``started_at`` predates the current boot as ``reboot-swept``.
    No /proc/stat → no boot detection (we silently skip the sweep).
    """
    # stx-allow: fallback (reason: /proc/stat is Linux-specific; macOS
    # has no equivalent and the reboot-sweep degrades gracefully)
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    btime = int(line.split()[1])
                    return datetime.fromtimestamp(btime, timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
    except OSError:  # stx-allow: fallback (reason: see inline comment)
        pass
    return None


def _pid_is_gone(pid: object) -> bool:
    """True when ``os.kill(pid, 0)`` says the process is not there.

    ``PermissionError`` is ALIVE. The process exists but is owned by another
    uid, so we may not signal it — that is proof of life, NOT death. Reaping
    on EPERM would END a live agent's record, and a missing record is exactly
    what makes ``send_to_agent`` report "agent not running"
    (``cli_pkg/_send.py``). This branch had never actually run before pids
    were recorded (0 'crashed' rows in 1229), so the hazard was dormant; it is
    live now. Matches every other pid probe in the codebase
    (``_lifecycle/_stale_lease``, ``cli_pkg/_send_diagnosis``,
    ``runtimes/_tui_liveness``) — all treat EPERM as alive.
    """
    if pid is None:
        return False
    try:
        value = int(pid)
    except (TypeError, ValueError):  # stx-allow: fallback (reason: a malformed pid is not evidence of death)
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except PermissionError:
        return False
    except (
        OSError,
        ProcessLookupError,
    ):  # stx-allow: fallback (reason: ESRCH/other kernel error — the process is genuinely gone from our POV)
        return True
    return False


def _heartbeat_seconds(value: object) -> float | None:
    """Parse an ISO-8601 ``last_heartbeat_at`` into POSIX seconds, or None."""
    if not isinstance(value, str):
        return None
    try:
        return (
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except (
        ValueError,
        TypeError,
    ):  # stx-allow: fallback (reason: malformed timestamp tolerated)
        return None


def gc_dead_instances(
    *,
    heartbeat_stale_seconds: int = 300,
    dry_run: bool = False,
) -> dict[str, int]:
    """Sweep instances whose runner is gone. Returns counters.

    Three heuristics, applied in order, and ALL THREE scoped to this host:

    1. **Boot-epoch check** — every active record of THIS host whose
       ``started_at`` precedes the current ``/proc/stat btime`` is marked
       ``exit_reason='reboot-swept'``.
    2. **PID liveness** — for this host's active records, ``kill -0 pid``
       failures mark ``exit_reason='pid_absent_at_sweep'``. Note what that
       value does and does not claim: the pid was absent WHEN WE LOOKED. The
       ``ended_at`` written beside it is this sweep's clock, not a time of
       death, and the two can be hours apart (measured: 10h46m).
    3. **Heartbeat staleness** — for this host's own (``remote`` false)
       active records, a ``last_heartbeat_at`` older than
       ``heartbeat_stale_seconds`` marks ``exit_reason='gc-stale'``.

    Cross-host (``remote``) instances are NEVER swept, by construction in all
    three branches. We have no local liveness signal for a remote agent —
    ``sac agents list`` ssh-probes the peer live instead of tombstoning it
    here. Since the move to the SHARED store the ``host`` scope carries that
    guarantee, and the third branch keeps ``remote`` as well: they are
    different claims (``remote`` is written by the dispatcher, ``host`` by
    the observer) and losing either would be a silent widening.

    ``dry_run=True`` runs all three checks but writes nothing — counters
    reflect what *would* be swept. As under SQLite, a dry run can report MORE
    than the real sweep would: branch 1 does not actually end anything, so a
    record it names can be counted again by branches 2 and 3.
    """
    from .state_db import _resolve_host, now_iso

    # ``crashed`` is a DEPRECATED ALIAS carrying the same number as
    # ``pid_absent_at_sweep``, so a consumer reading the old key keeps working
    # across this release rather than silently seeing zero swept rows — which
    # is exactly the "success value that is also the didn't-check value" this
    # change exists to stop producing.
    counters = {
        "reboot_swept": 0,
        "crashed": 0,
        EXIT_REASON_PID_ABSENT_AT_SWEEP: 0,
        "gc_stale": 0,
    }
    boot = _proc_btime()
    canonical_host = _resolve_host(None)
    # ONE clock reading for the whole sweep. This is deliberate and correct —
    # it is the moment we LOOKED — but it is why every record this pass reaps
    # shares a second, and why that shared second must never be read as a
    # simultaneous death. The value written beside it says so by name now.
    now_ts = now_iso()
    stale_cutoff = datetime.now(timezone.utc).timestamp() - heartbeat_stale_seconds

    rows = list_active_instances(host=canonical_host)
    swept: set[str] = set()

    def _sweep(row: dict, *, reason: str, ended_at: str) -> bool:
        """Record one tombstone. Returns whether it counted."""
        instance_id = str(row.get("id") or "")
        if not instance_id or instance_id in swept:
            return False
        if dry_run:
            # Deliberately NOT added to ``swept``: the SQLite version re-read
            # the table between branches, so a dry run (which updates nothing)
            # let a later branch see a record an earlier one named. Preserved
            # rather than quietly improved — the counters are a preview of
            # three independent checks, not of one serialised sweep.
            return True
        if not end_instance(instance_id, exit_reason=reason, ended_at=ended_at):
            return False
        swept.add(instance_id)
        return True

    if boot is not None:
        for row in rows:
            started_at = row.get("started_at")
            if isinstance(started_at, str) and started_at < boot:
                # ``ended_at=boot``: the reboot IS the moment every one of
                # these processes stopped existing, which is the one branch
                # here that can honestly name a time of death.
                if _sweep(row, reason="reboot-swept", ended_at=boot):
                    counters["reboot_swept"] += 1

    for row in rows:
        if _pid_is_gone(row.get("pid")):
            if _sweep(
                row, reason=EXIT_REASON_PID_ABSENT_AT_SWEEP, ended_at=now_ts
            ):
                counters["crashed"] += 1
                counters[EXIT_REASON_PID_ABSENT_AT_SWEEP] += 1

    for row in rows:
        if row.get("remote"):
            continue
        heartbeat = _heartbeat_seconds(row.get("last_heartbeat_at"))
        if heartbeat is None or heartbeat >= stale_cutoff:
            continue
        if _sweep(row, reason="gc-stale", ended_at=now_ts):
            counters["gc_stale"] += 1

    return counters


__all__ = [
    "EXIT_REASON_PID_ABSENT_AT_SWEEP",
    "LEGACY_EXIT_REASON_CRASHED",
    "_proc_btime",
    "gc_dead_instances",
]

# EOF
