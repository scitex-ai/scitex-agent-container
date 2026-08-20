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
