"""The two AGENT-LIVENESS enforcers, which only make sense side by side.

Split out of :mod:`._jobs_plugin` (at the per-file cap). They are one concern
divided in two, and each one's scope is defined by what the other handles:

* ``fleet-reconcile`` restarts CORPSES — no tmux session, so no context to lose.
* ``restart-login-expired-agents`` restarts the LIVE-BUT-WEDGED half — tmux is
  up and the pane is frozen behind an auth banner — which fleet-reconcile
  deliberately will not touch.

Keeping them in one file is what makes either readable: they share a rate-limit
vocabulary, run on the same beat by design, and a reader checking "who covers a
wedged agent?" must see both answers at once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.jobs import JobSpec

__all__ = ["liveness_jobs"]


def liveness_jobs() -> "list[JobSpec]":
    """The fleet-liveness JobSpecs, in their historical order."""
    from scitex_dev.jobs import JobSpec

    from ._sac_bin import sac_bin

    # ABSOLUTE, resolved per host -- see :mod:`._sac_bin`. A bare `sac`
    # after the absolute `timeout` head is invisible to resolve_execstart
    # and is looked up on the supervisor's PATH, which is how every sac
    # job on scitex-compute-01 sat at exit 127 (measured 2026-08-20).
    sac = sac_bin()

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
    ]
