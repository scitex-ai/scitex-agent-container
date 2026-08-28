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

A KNOWN DEFECT IN HOW THESE ARE SCHEDULED, which is NOT fixed here
------------------------------------------------------------------
A systemd timer rendered from ``OnBootSec`` + ``OnUnitActiveSec`` alone —
which is what :func:`scitex_dev.jobs._systemd.build_timer_unit` emits for
every one of these — dies PERMANENTLY if the timer unit is started later
than ``OnBootSec`` after boot. Both monotonic elapse points are then already
in the past, systemd marks the unit ``elapsed``, and it never re-arms.
``Persistent=true`` cannot save it: systemd documents that setting as
applying ONLY to ``OnCalendar=`` timers.

MEASURED on scitex-compute-04, 2026-08-28, on ``fleet-reconcile.timer``::

    TimersMonotonic={ OnUnitActiveUSec=5min ; next_elapse=0 }
    TimersMonotonic={ OnBootUSec=5min ; next_elapse=0 }
    NextElapseUSecMonotonic=infinity
    LastTriggerUSec=Wed 2026-08-19 17:51:10 UTC
    ActiveState=active   SubState=elapsed   UnitFileState=enabled

Boot was 2026-08-27 08:15:24 UTC and the timer unit became active
2026-08-28 03:17:08 — nineteen hours later. It had not fired in NINE DAYS
while reporting ``active`` and ``enabled``.
``restart-login-expired-agents.timer`` was in the same state, last triggered
2026-08-20 03:36:01 UTC.

The control that isolates the cause is ``accounts-keepalive.timer`` on the
SAME host with the SAME monotonic-only shape, still firing every minute —
because it happened to have been active continuously since boot. The
discriminating variable is WHEN the unit started relative to boot.

WHY THE OBVIOUS FIX IS NOT APPLIED HERE. Adding ``on_calendar`` would give
these timers a schedule that always has a next elapse, and it was tried. It
BREAKS sac's strict cron lowering: ``_up_timer_losses`` treats ANY
``on_calendar`` as a lossy field, unconditionally, on the grounds that a
crontab line carries no timezone. That reasoning is right for a
zone-bearing calendar and wrong for a zone-free interval like
``*-*-* *:0/5:00``, which lowers to ``*/5 * * * *`` exactly — but the guard
does not distinguish them, and sac's own ``test_no_job_degrades_when_lowered
_onto_cron`` correctly fails. Cron lowering is the real deployment path for
a host without ``systemd --user`` (scitex-nas-03 is one), so it is not
something to weaken from this side.

The fix belongs in ``scitex_dev.jobs._systemd.build_timer_unit``: emit
``OnActiveSec=`` beside ``OnUnitActiveSec=``. ``OnActiveSec`` is relative to
the TIMER's own activation, so a unit started at any point after boot always
has a base for its first fire, and the ``OnUnitActiveSec`` chain sustains
itself from there. It names no timezone and adds no cron loss. Tracked as
``dev-timer-monotonic-dead-end-20260828``.

UNTIL THAT LANDS, arming is an operational step with a REQUIRED check: after
enabling a timer, trigger its service ONCE (``systemctl --user start
<name>.service``) so ``OnUnitActiveSec`` has a base for this boot, then
confirm the timer reads ``SubState=waiting`` with a real ``NextElapse``.
``enabled`` and ``active`` are BOTH true of a timer that will never fire
again, so neither is the check.
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
