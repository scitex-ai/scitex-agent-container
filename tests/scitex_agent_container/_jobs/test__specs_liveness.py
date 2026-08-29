"""The two AGENT-LIVENESS enforcers, tested side by side.

Split out of ``test__jobs_plugin.py`` (at the per-file cap), mirroring the
split of :mod:`scitex_agent_container._jobs._specs_liveness` on the source
side — a file that already argued these two are unreadable apart, because each
one's scope is defined by what the other handles:

* ``fleet-reconcile`` restarts CORPSES — no tmux session, so no context to lose.
* ``restart-login-expired-agents`` restarts the LIVE-BUT-WEDGED half, which
  fleet-reconcile deliberately will not touch.

``fleet-reconcile``'s pins matter more than most: ``restart.policy`` in ~93
specs is DEAD CODE without that timer, so a wrong ``kind`` or a command that
lost ``--apply`` would put the fleet back to dying unnoticed.
"""

from __future__ import annotations


import pytest

jobs_mod = pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)


from ._jobspec_helpers import _job, _split_command  # noqa: E402


def test_fleet_reconcile_job_name_is_package_prefixed() -> None:
    # Arrange — the enforcer of "should be running => is running".
    # Act
    job = _job("scitex-agent-container-fleet-reconcile")
    # Assert
    assert job.name == "scitex-agent-container-fleet-reconcile"

def test_fleet_reconcile_job_kind_is_timer() -> None:
    # Arrange — a periodic systemd --user timer, so kind="timer". A wrong
    # kind raises at construction and `ecosystem up` then silently DROPS
    # sac's whole provider (provider-isolated, WARN-only) — taking the OAuth
    # refresh, the drift check and the worktree GC down with it.
    # Act
    job = _job("scitex-agent-container-fleet-reconcile")
    # Assert
    assert job.kind == "timer"

def test_fleet_reconcile_command_is_the_applying_form() -> None:
    # Arrange — THIS JOB IS THE MECHANISM. `restart.policy` in ~93 specs is
    # dead code without it: `_lifecycle/_start.py` runs the loop that reads
    # it on a daemon thread inside the short-lived `sac agents start` CLI, so
    # the supervisor dies with the process that promised it. A scheduled
    # DRY-RUN would restore nothing — the whole point is `--apply`.
    # Act
    job = _job("scitex-agent-container-fleet-reconcile")
    # Assert
    bound, _payload, rest = _split_command(job.command)
    assert (bound, rest) == ("/usr/bin/timeout 300", "agents reconcile --apply")

def test_fleet_reconcile_cadence_is_five_minutes() -> None:
    # Arrange — the cadence IS the window a dead agent stays dead. A no-op
    # pass is one batched `tmux list-sessions` plus a spec read each, so it
    # is cheap enough to run often.
    # Act
    job = _job("scitex-agent-container-fleet-reconcile")
    # Assert
    assert job.on_unit_active_sec == "5min"

def test_fleet_reconcile_timeout_outlives_a_capped_pass() -> None:
    # Arrange — the pathological pass restarts `--limit` agents, each a
    # stop+settle+start. A pass killed at this timeout is SAFE (the restart
    # history is persisted per restart, not at the end), but the timeout must
    # still comfortably exceed a normal pass or the enforcer never finishes.
    #
    # THIS is the job whose unbounded cron line was measured accumulating
    # fourteen concurrent instances, the oldest 45 minutes old (2026-07-18).
    # A `timeout_sec` assertion would have passed throughout that incident,
    # because the field was set and the deployed cron line was still
    # unbounded — so the bound is pinned where it actually runs.
    # Act
    job = _job("scitex-agent-container-fleet-reconcile")
    # Assert
    assert job.command.startswith("/usr/bin/timeout 300 ")

def test_restart_login_expired_job_name_is_package_prefixed() -> None:
    # Arrange — the auto-restarter for auth-dead-but-live agents.
    # Act
    job = _job("scitex-agent-container-restart-login-expired-agents")
    # Assert
    assert job.name == "scitex-agent-container-restart-login-expired-agents"

def test_restart_login_expired_job_kind_is_timer() -> None:
    # Arrange — a periodic systemd --user timer, so kind="timer". A wrong kind
    # raises at construction and `ecosystem up` then silently DROPS sac's whole
    # provider (provider-isolated, WARN-only) — taking the OAuth refresh, the
    # drift check, the worktree GC AND the fleet-reconcile enforcer down too.
    # Act
    job = _job("scitex-agent-container-restart-login-expired-agents")
    # Assert
    assert job.kind == "timer"

def test_restart_login_expired_command_is_the_applying_form() -> None:
    # Arrange — a scheduled DRY-RUN would detect wedged agents and heal none.
    # The whole point is `--apply`. Detection stays read-only; the restart is
    # the only mutation.
    # Act
    job = _job("scitex-agent-container-restart-login-expired-agents")
    # Assert
    bound, _payload, rest = _split_command(job.command)
    assert (bound, rest) == ("/usr/bin/timeout 300", "agents restart-login-expired --apply")

def test_restart_login_expired_cadence_is_five_minutes() -> None:
    # Arrange — the cadence IS the window a login-expired agent stays wedged,
    # matched to fleet-reconcile so the two enforcers sweep on the same beat.
    # Act
    job = _job("scitex-agent-container-restart-login-expired-agents")
    # Assert
    assert job.on_unit_active_sec == "5min"

def test_restart_login_expired_constructs_as_a_real_jobspec() -> None:
    # Arrange — construction must not raise (a bad field would drop the whole
    # provider). Assert it is the canonical contract type, not a look-alike.
    # Act
    job = _job("scitex-agent-container-restart-login-expired-agents")
    # Assert
    assert isinstance(job, jobs_mod.JobSpec)


# --- the SCHEDULE these three share, and the defect around it ----------------------
#
# A timer rendered from OnBootSec + OnUnitActiveSec ALONE dies permanently the
# first time the unit is started later than OnBootSec after boot — both
# monotonic elapse points are then in the past, systemd marks it `elapsed`, and
# it never re-arms. `Persistent=true` cannot save it; systemd documents that
# setting as applying only to OnCalendar= timers. The repair belongs in
# scitex-dev's build_timer_unit (an OnActiveSec= base); see the module
# docstring of _jobs/_specs_liveness for why it is not an on_calendar here.
# What IS pinned below is the cron schedule those jobs must keep, because the
# cron lowering is the real deployment path on a host with no systemd --user.
#
# MEASURED on scitex-compute-04, 2026-08-28, on fleet-reconcile.timer itself:
#     NextElapseUSecMonotonic=infinity
#     LastTriggerUSec=Wed 2026-08-19 17:51:10 UTC
#     ActiveState=active  SubState=elapsed  UnitFileState=enabled
# Boot was 2026-08-27 08:15:24 UTC; the unit became active 2026-08-28 03:17:08,
# 19 hours later. Nine days without firing while reporting healthy. The control
# that isolates the cause is `accounts-keepalive.timer` on the SAME host with
# the SAME monotonic-only shape, still firing every minute — because it had
# been active continuously since boot. The discriminating variable is WHEN the
# unit started relative to boot, nothing else.
#
# These are the guards that keep the wall-clock anchor from being dropped again.

_LIVENESS_JOBS = (
    "scitex-agent-container-fleet-reconcile",
    "scitex-agent-container-restart-login-expired-agents",
    "scitex-agent-container-resume-rate-limited-agents",
)


@pytest.mark.parametrize("name", _LIVENESS_JOBS)
def test_every_liveness_timer_keeps_its_cron_schedule(name: str) -> None:
    # Arrange — the cron LOWERING reads `schedule`, not `on_calendar`. Dropping
    # it while adding the wall-clock anchor would silently unschedule these
    # jobs on any host that lowers to crontab instead of systemd.
    # Act
    job = _job(name)
    # Assert
    assert job.schedule == "*/5 * * * *"


def test_the_three_enforcers_sweep_on_one_beat() -> None:
    # Arrange — one cadence across all three, so a reader reasoning about "how
    # long can an agent stay stopped" gets ONE answer rather than three. They
    # are one concern divided three ways, not three schedules.
    # Act
    cadences = {_job(name).on_unit_active_sec for name in _LIVENESS_JOBS}
    # Assert
    assert cadences == {"5min"}


# --- the THIRD enforcer: the rate-wall reviver -------------------------------


def test_resume_rate_limited_job_name_is_package_prefixed() -> None:
    # Arrange — the shape the other two divide the fleet around without
    # covering: a live session parked behind a provider rate wall. Both
    # correctly declined it, and the agent stayed stopped for 1h46m.
    # Act
    job = _job("scitex-agent-container-resume-rate-limited-agents")
    # Assert
    assert job.name == "scitex-agent-container-resume-rate-limited-agents"


def test_resume_rate_limited_job_kind_is_timer() -> None:
    # Arrange — a periodic systemd --user timer. A wrong kind raises at
    # construction and `ecosystem up` then silently DROPS sac's whole provider,
    # taking the other two liveness enforcers down with it.
    # Act
    job = _job("scitex-agent-container-resume-rate-limited-agents")
    # Assert
    assert job.kind == "timer"


def test_resume_rate_limited_command_is_the_applying_form() -> None:
    # Arrange — a scheduled DRY-RUN would detect parked agents and wake none.
    # The whole point is `--apply`; detection stays read-only and the verified
    # nudge is the only mutation in the flow.
    # Act
    job = _job("scitex-agent-container-resume-rate-limited-agents")
    # Assert
    bound, _payload, rest = _split_command(job.command)
    assert (bound, rest) == (
        "/usr/bin/timeout 600",
        "agents resume-rate-limited --apply",
    )


def test_resume_rate_limited_outlives_a_verified_delivery() -> None:
    # Arrange — DELIBERATELY larger than the siblings' 300s. Their remedy is a
    # restart; this one's is a verified delivery that waits for idle, submits,
    # and proves the payload left the compose box — tens of seconds per agent
    # by design, because an unverified nudge is the failure it exists to avoid.
    # Act
    job = _job("scitex-agent-container-resume-rate-limited-agents")
    # Assert
    assert job.command.startswith("/usr/bin/timeout 600 ")


def test_resume_rate_limited_constructs_as_a_real_jobspec() -> None:
    # Arrange — construction must not raise (a bad field drops the whole
    # provider). Assert it is the canonical contract type, not a look-alike.
    # Act
    job = _job("scitex-agent-container-resume-rate-limited-agents")
    # Assert
    assert isinstance(job, jobs_mod.JobSpec)
