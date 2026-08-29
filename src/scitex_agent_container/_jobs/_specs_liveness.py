"""The three AGENT-LIVENESS enforcers, which only make sense side by side.

Split out of :mod:`._jobs_plugin` (at the per-file cap). They are one concern
divided three ways, and each one's scope is defined by what the others handle:

* ``fleet-reconcile`` restarts CORPSES — no tmux session, so no context to lose.
* ``restart-login-expired-agents`` restarts the LIVE-BUT-WEDGED half — tmux is
  up and the pane is frozen behind an auth banner — which fleet-reconcile
  deliberately will not touch.
* ``resume-rate-limited-agents`` RESUMES the LIVE-BUT-PAUSED half — tmux is up
  and the pane is frozen behind a provider rate wall — which the other two
  BOTH decline, each for a good reason, which is how the gap stayed invisible
  until it cost 1h46m of fleet downtime on 2026-08-28.

Keeping them in one file is what makes any of them readable: they share a
rate-limit vocabulary, run on the same beat by design, and a reader checking
"who covers a stopped agent?" must see all three answers at once. The third
one is also the proof that the division was previously INCOMPLETE — two
enforcers that hand off to each other look exhaustive right up until an agent
lands between them.

WHERE THESE ACTUALLY RUN, because the obvious instrument gives a WRONG answer
----------------------------------------------------------------------------
``kind="timer"`` no longer means a per-leaf ``systemd --user`` timer, and it
no longer means a crontab line either. Both lowerings are RETIRED. Every
periodic JobSpec is executed in-process by the ecosystem supervisor's
``PeriodicRunner`` (:mod:`scitex_dev._supervisor._periodic`), which owns the
clock and writes one record per start, finish and skip to
``~/.scitex/dev/runtime/periodic-executions.jsonl``.

THAT LOG IS THE INSTRUMENT. ``systemctl --user list-timers`` is NOT, and
reading it produces a confidently wrong answer: hosts still carry ORPHAN
``<job>.timer`` / ``<job>.service`` units from the retired model, and those
orphans report their own long-dead state. Measured 2026-08-28 on
scitex-compute-04, ``fleet-reconcile.timer`` read ``ActiveState=active``,
``UnitFileState=enabled``, ``SubState=elapsed``, ``LastTriggerUSec=Wed
2026-08-19 17:51:10 UTC`` — nine days silent — WHILE the supervisor was
running the same job every five minutes and had logged 3,764 executions of
it. An investigation that stopped at ``list-timers`` would have concluded the
fleet's liveness enforcement had been dead for over a week. It was not.

(The orphans are dead for a real reason, and it is worth knowing so nobody
"fixes" one by re-arming it: a timer rendered from ``OnBootSec`` +
``OnUnitActiveSec`` alone never re-arms if the unit is started later than
``OnBootSec`` after boot, and ``Persistent=true`` does not help because
systemd applies it only to ``OnCalendar=`` timers. Re-arming an orphan does
not repair anything — it puts a SECOND scheduler on a job the supervisor
already runs, with independent debounce state. That is the double-supervisor
hazard, and it was created and reverted while writing this file. The orphans
want removing, not reviving: ``dev-timer-monotonic-dead-end-20260828``.)

WHICH CADENCE FIELD IS LIVE. ``PeriodicRunner`` reads
``on_unit_active_sec`` first and falls back to ``schedule`` (the cron
expression); it never reads ``on_calendar``. So both fields below are
load-bearing and ``on_calendar`` would be silently ignored — quite apart
from breaking sac's strict cron-lowering guard, which is where an attempt to
add one was caught.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.jobs import JobSpec

__all__ = ["liveness_jobs"]


def liveness_jobs(*, executable: str | None = None) -> "list[JobSpec]":
    """The fleet-liveness JobSpecs, in their historical order.

    ``executable`` is the same test seam :func:`._sac_bin.sac_bin` exposes,
    threaded through so a test can resolve the payload against a venv-shaped
    tree it built on disk. Without it the rendered command depends on whether
    the RUNNING environment happens to have a ``sac`` console script beside
    its interpreter — true in production, false under a PYTHONPATH-only CI
    run — and a population guard over these specs would then assert an
    environmental fact rather than a property of the specs.
    """
    from scitex_dev.jobs import JobSpec

    from ._sac_bin import sac_bin

    # ABSOLUTE, resolved per host -- see :mod:`._sac_bin`. A bare `sac`
    # after the absolute `timeout` head is invisible to resolve_execstart
    # and is looked up on the supervisor's PATH, which is how every sac
    # job on scitex-compute-01 sat at exit 127 (measured 2026-08-20).
    sac = sac_bin(executable=executable)

    return [
        JobSpec(
            name="scitex-agent-container-fleet-reconcile",
            schedule="*/5 * * * *",  # every 5min (cron form; timer cadence below)
            # SELF-BOUNDING (300s) — the bound lives in the command because
            # this job lands on CRON, where `timeout_sec` cannot follow it.
            # A no-op pass takes ~seconds; this bounds the pathological one
            # (`--limit` restarts, each a stop+settle+start). A pass killed at
            # this timeout is SAFE: the restart history is persisted per
            # restart, not at the end, so the next tick still honours the
            # debounce for anything already bounced.
            #
            # This is the job whose UNBOUNDED cron line was measured piling
            # up fourteen concurrent instances, the oldest 45 minutes old
            # (2026-07-18) — the incident that motivated the guard.
            command=f"/usr/bin/timeout 300 {sac} agents reconcile --apply",
            description=(
                "The enforcer of 'should be running => is running'. Restarts "
                "agents whose tmux session is GONE while their spec asks to be "
                "kept running AND nothing recorded a deliberate stop. Only ever "
                "touches a CORPSE (no session => no context to lose); never a "
                "live-but-wedged agent (auth-heal owns those) and never a "
                "deliberately-stopped one. REFUSES a mass restart: several "
                "corpses AND no live tmux session anywhere is the SERVER dying, "
                "not N agents dying, so the pass withholds every restart and "
                "alarms (exit 2) instead of acting on a reading it cannot "
                "disambiguate. Rate-limited per agent (30min debounce, "
                "<=2/agent/hour) and per pass (<=10). NOTE that hourly cap is a "
                "ROLLING window, NOT a give-up: an agent it can never recover "
                "is RECORDED as degraded and retried at 2/hour indefinitely. "
                "Bounded RATE, not eventual surrender — do not read it as one."
            ),
            kind="timer",
            # THIS JOB IS THE MECHANISM, not an optimisation. `restart.policy`
            # in ~93 specs is dead code — `_lifecycle/_start.py` launches the
            # loop that reads it on a `daemon=True` thread and then returns,
            # and `sac agents start` is a short-lived CLI, so the supervisor
            # dies with the process that promised it. Nothing else owns fleet
            # liveness: `sac listen`'s reconciler only alarms on stuck CARDS.
            # An OAuth rotation killed 33 agents and they stayed dead until the
            # operator noticed by chance. Unschedule this and that returns.
            #
            # 5min: the window an agent stays dead. A pass that finds nothing
            # (the normal case) is one batched `tmux list-sessions` plus a spec
            # read each — cheap enough to run often.
            on_boot_sec="5min",
            on_unit_active_sec="5min",
        ),
        JobSpec(
            name="scitex-agent-container-restart-login-expired-agents",
            schedule="*/5 * * * *",  # every 5min (cron form; timer cadence below)
            # SELF-BOUNDING (300s), mirroring fleet-reconcile: bounds the
            # pathological pass (`--limit` restarts, each a stop+settle+start)
            # plus the ~4s capture interval. A pass killed here is SAFE —
            # history is persisted per restart, so the next tick still honours
            # the debounce for anything bounced.
            command=(
                f"/usr/bin/timeout 300 {sac} agents restart-login-expired --apply"
            ),
            description=(
                "Restarts LIVE agents wedged behind a frozen 'Login expired' "
                "banner (auth-dead but tmux-alive) — the half fleet-reconcile "
                "leaves alone. Detection is READ-ONLY + 2-run-corroborated (a "
                "banner that moved between the two captures = working, never "
                "restarted); the restart runs through the pool-loading start "
                "path (cannot strip CCT tokens) and is rate-limited (30min/agent "
                "debounce, <=2/agent/hour, <=10/pass). As with fleet-reconcile "
                "that hourly cap is a ROLLING window: an agent still wedged "
                "after it is RECORDED as degraded and retried at 2/hour, not "
                "given up on. DEPLOY GATE: do NOT enable until the host's "
                "auth-heal.py scan_tui cron is retired (double-supervisor risk)."
            ),
            # Same taxonomy note as the jobs above: kind must be one of
            # {"service","timer","cron"} (scitex-dev #153); a periodic
            # systemd --user timer is ``kind="timer"`` with the cadence in
            # ``on_unit_active_sec``. A wrong kind raises at construction and
            # ``ecosystem up`` then silently drops sac's WHOLE provider.
            kind="timer",
            # 5min matched to fleet-reconcile so the two enforcers sweep on the
            # same beat rather than harmonising into one; it is the window a
            # wedged agent stays wedged. A no-op pass is one `tmux list-sessions`
            # plus two pane captures ~4s apart.
            on_boot_sec="5min",
            on_unit_active_sec="5min",
        ),
        JobSpec(
            name="scitex-agent-container-resume-rate-limited-agents",
            schedule="*/5 * * * *",  # every 5min (cron form; timer cadence below)
            # SELF-BOUNDING (600s), and DELIBERATELY larger than its siblings'
            # 300s. Their remedy is a restart; this one's is a VERIFIED
            # delivery, which waits for the agent to be idle, submits, and then
            # proves the payload left the compose box — tens of seconds per
            # agent by design, because an unverified nudge is the failure this
            # remedy exists to avoid. A pass killed at this timeout is SAFE:
            # the resume history is persisted per resume, not at the end, so
            # the next tick still honours the debounce for anything already
            # woken.
            command=(
                f"/usr/bin/timeout 600 {sac} agents resume-rate-limited --apply"
            ),
            description=(
                "Resumes LIVE agents parked behind a provider rate wall whose "
                "published reset has PASSED — the shape the other two liveness "
                "enforcers divide the fleet around without covering. "
                "fleet-reconcile sees a live tmux session and correctly hands "
                "off; the auth healer's matcher excludes 429 by design ('a "
                "restart does not fix a rate wall'). INCIDENT 2026-08-28: a "
                "session limit stopped agents at ~17:25 UTC, lifted at 19:10 "
                "UTC, and NOTHING resumed until the operator asked at 20:56 "
                "UTC. It CONTINUES the agent (verified delivery, proven to "
                "leave the compose box) and never restarts one — the session, "
                "context and conversation all survived the wall, and a restart "
                "would destroy what makes resuming worth doing. It reads the "
                "reset time from the provider's own banner and HOLDS until it "
                "passes, so it cannot spend a token against a limit that is "
                "still standing; a wall whose reset it cannot READ is held and "
                "reported, never guessed at. Rate-limited per agent (30min "
                "debounce, <=2/agent/hour) and per pass, on its OWN ledger so "
                "the three enforcers' debounces cannot consume each other's. "
                "An agent it cannot wake is RECORDED as degraded, not nudged "
                "forever."
            ),
            kind="timer",
            on_boot_sec="5min",
            on_unit_active_sec="5min",
        ),
    ]
