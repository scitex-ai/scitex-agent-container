"""Garbage-collection sweep for state.db ``instances`` rows.

Extracted from :mod:`state_db` for line-budget. Re-exported by
:mod:`state_db` so external callers keep the same import path.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


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


def gc_dead_instances(
    *,
    db_path: Path | None = None,
    heartbeat_stale_seconds: int = 300,
    dry_run: bool = False,
) -> dict[str, int]:
    """Sweep instances whose runner is gone. Returns counters.

    Three heuristics, applied in order:

    1. **Boot-epoch check** — every active row whose ``started_at``
       precedes the current ``/proc/stat btime`` is marked
       ``exit_reason='reboot-swept'``.
    2. **PID liveness** — for the host's own active rows, ``kill -0
       pid`` failures mark the row ``exit_reason='crashed'``.
    3. **Heartbeat staleness** — if ``last_heartbeat_at`` exists and
       is older than ``heartbeat_stale_seconds``, mark
       ``exit_reason='gc-stale'``.

    Cross-host (``remote=1``) instances are NEVER swept, by construction
    in all three branches: reboot + pid are ``host=<self>``-scoped (a
    peer's row has a different host), and the heartbeat sweep carries an
    explicit ``AND remote=0``. We have no local liveness signal for a
    remote agent — ``sac agents list`` ssh-probes the peer live instead of
    tombstoning it here.

    ``dry_run=True`` runs all three checks but emits zero UPDATE
    statements — counters reflect what *would* be swept.
    """
    from .state_db import _resolve_host, now_iso, open_db

    counters = {"reboot_swept": 0, "crashed": 0, "gc_stale": 0}
    boot = _proc_btime()
    canonical_host = _resolve_host(None)
    now_ts = now_iso()
    stale_cutoff = datetime.now(timezone.utc).timestamp() - heartbeat_stale_seconds

    with open_db(db_path) as conn:
        if boot is not None:
            if dry_run:
                cur = conn.execute(
                    "SELECT COUNT(*) AS n FROM instances "
                    "WHERE ended_at IS NULL AND host=? AND started_at < ?",
                    (canonical_host, boot),
                ).fetchone()
                counters["reboot_swept"] = int(cur["n"]) if cur else 0
            else:
                cur = conn.execute(
                    "UPDATE instances SET ended_at=?, exit_reason='reboot-swept' "
                    "WHERE ended_at IS NULL AND host=? AND started_at < ?",
                    (boot, canonical_host, boot),
                )
                counters["reboot_swept"] = cur.rowcount

        rows = conn.execute(
            "SELECT id, pid FROM instances WHERE ended_at IS NULL AND host=?",
            (canonical_host,),
        ).fetchall()
        for row in rows:
            pid = row["pid"]
            if pid is None or pid <= 0:
                continue
            try:
                os.kill(pid, 0)
            except PermissionError:
                # ALIVE. The process exists but is owned by another uid, so
                # we may not signal it — that is proof of life, NOT death.
                # Reaping here would END a live agent's row, and a missing
                # row is exactly what makes ``send_to_agent`` report "agent
                # not running" (cli_pkg/_send.py). This branch had never
                # actually run before pids were recorded (0 'crashed' rows
                # in 1229), so the hazard was dormant; it is live now.
                # Matches every other pid probe in the codebase
                # (_lifecycle/_stale_lease, cli_pkg/_send_diagnosis,
                # runtimes/_tui_liveness) — all treat EPERM as alive.
                continue
            except (
                OSError,
                ProcessLookupError,
            ):  # stx-allow: fallback (reason: ESRCH/other kernel error — the process is genuinely gone from our POV)
                if not dry_run:
                    conn.execute(
                        "UPDATE instances SET ended_at=?, exit_reason='crashed' WHERE id=?",
                        (now_ts, row["id"]),
                    )
                counters["crashed"] += 1

        # ``AND remote=0`` — the heartbeat sweep is the ONE branch without a
        # host filter, so without this a cross-host (``remote=1``) row could be
        # reaped from the master the instant a stale ``last_heartbeat_at`` were
        # ever written to it. A master remote row is safe TODAY only because its
        # heartbeat is NULL; this makes "cross-host instances are never swept"
        # true BY CONSTRUCTION across all three branches (reboot + pid are
        # already ``host=<self>``-scoped), not merely true by accident.
        cur = conn.execute(
            "SELECT id, last_heartbeat_at FROM instances "
            "WHERE ended_at IS NULL AND last_heartbeat_at IS NOT NULL AND remote=0"
        ).fetchall()
        for row in cur:
            try:
                hb = (
                    datetime.strptime(row["last_heartbeat_at"], "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                )
            except (
                ValueError,
                TypeError,
            ):  # stx-allow: fallback (reason: malformed timestamp tolerated)
                continue
            if hb < stale_cutoff:
                if not dry_run:
                    conn.execute(
                        "UPDATE instances SET ended_at=?, exit_reason='gc-stale' "
                        "WHERE id=?",
                        (now_ts, row["id"]),
                    )
                counters["gc_stale"] += 1

    return counters


__all__ = ["_proc_btime", "gc_dead_instances"]
