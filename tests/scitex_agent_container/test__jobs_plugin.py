"""Tests for the ``scitex_dev.jobs`` provider (``_jobs_plugin``).

Verifies the JobSpecs sac registers under the ``scitex_dev.jobs``
entry-point group match the federated contract:

* ``sac.accounts-refresh`` — a periodic systemd timer job that runs
  ``--all --include-active --sync-active-login`` every 2h (the SOLE
  refresher; see the ``--skip-active`` note below).
* ``sac.listen`` — a long-running systemd service job (the host
  control-plane daemon), auto-started on boot and auto-restarted on
  ANY exit (``restart_policy="always"``), registered so the host no
  longer needs a cron-based watchdog
  (clew incident ``clew-incident-sac-host-listen-down``, 2026-07-05).

Skipped cleanly if the installed scitex-dev predates ``scitex_dev.jobs``
(PyPI lag) — the entry-point registration is install-time metadata and
the provider import is lazy, so an old scitex-dev must not fail CI here.
"""

from __future__ import annotations

import pytest

jobs_mod = pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)

from scitex_agent_container._jobs_plugin import provide_jobs  # noqa: E402


def _job(name: str):
    (match,) = [j for j in provide_jobs() if j.name == name]
    return match


def test_provider_returns_two_jobs() -> None:
    # Arrange — call the registered provider.
    # Act
    jobs = provide_jobs()
    # Assert
    assert len(jobs) == 2


def test_provider_jobs_are_real_jobspecs() -> None:
    # Arrange — call the registered provider.
    # Act
    jobs = provide_jobs()
    # Assert — every entry is the canonical contract type, not a look-alike.
    assert all(isinstance(job, jobs_mod.JobSpec) for job in jobs)


def test_provider_job_name_is_package_prefixed() -> None:
    # Arrange — call the registered provider.
    # Act
    job = _job("sac.accounts-refresh")
    # Assert
    assert job.name == "sac.accounts-refresh"


def test_provider_job_command_includes_active_account() -> None:
    # Arrange — call the registered provider. This assertion previously
    # pinned ``--skip-active``, which was correct only under the
    # pre-2026-07-08 TWO-refresher model (host timer + in-container CLI
    # racing on one single-use refresh_token). Agents now bind the
    # credential ``:ro`` and never refresh, so this timer is the SOLE
    # refresher: skipping the active account starved the one account the
    # whole fleet uses until its ~8h access_token expired (2026-07-09/10
    # total stall). Do NOT revert to --skip-active.
    # Act — by NAME, not by index: this provider now returns two jobs, so
    # provide_jobs()[0] would silently start asserting against the wrong
    # JobSpec the day the list order changes.
    job = _job("sac.accounts-refresh")
    # Assert
    assert job.command == (
        "sac accounts refresh --all --include-active --sync-active-login"
    )


def test_provider_job_command_never_skips_active() -> None:
    # Arrange — a belt-and-braces guard: --skip-active must never
    # reappear in the sole-refresher timer, however the command is spelled.
    # Act
    job = _job("sac.accounts-refresh")
    # Assert
    assert "--skip-active" not in job.command


def test_provider_job_kind_is_timer() -> None:
    # Arrange — call the registered provider. The legacy ``kind=
    # "systemd"`` is no longer accepted by JobSpec.validate() since
    # scitex-dev #153; ``sac.accounts-refresh`` is a periodic
    # systemd --user timer (token TTL ~7h, refresh every 2h) so the
    # canonical kind is ``"timer"`` (lead msg c5212862, 2026-06-11).
    # Act
    job = _job("sac.accounts-refresh")
    # Assert
    assert job.kind == "timer"


def test_every_provided_job_uses_an_allowed_kind() -> None:
    # Arrange — defensive: even when a new entry is added without a
    # paired pinning test, the taxonomy gate still fires here so the
    # whole provider is never silently dropped by ``ecosystem up``.
    # Act
    kinds = {j.kind for j in provide_jobs()}
    # Assert — JobSpec.ALLOWED_KINDS is the canonical taxonomy.
    assert kinds <= jobs_mod.ALLOWED_KINDS


def test_provider_job_cadence_is_two_hours() -> None:
    # Arrange — call the registered provider.
    # Act
    job = _job("sac.accounts-refresh")
    # Assert
    assert job.on_unit_active_sec == "2h"


def test_provider_listen_job_is_a_service() -> None:
    # Arrange — call the registered provider.
    # Act
    job = _job("sac.listen")
    # Assert — long-running control-plane daemon, not scheduled.
    assert job.kind == "service"


def test_provider_listen_job_has_no_schedule() -> None:
    # Arrange — call the registered provider. kind="service" requires
    # schedule=="" (services aren't scheduled — they run continuously).
    # Act
    job = _job("sac.listen")
    # Assert
    assert job.schedule == ""


def test_provider_listen_job_command() -> None:
    # Arrange — call the registered provider. Confirmed (task brief,
    # 2026-07-05): SAC_LISTEN_BEARER self-resolves from the on-disk
    # token file when unset (PR #470), so a bare command suffices.
    # Act
    job = _job("sac.listen")
    # Assert
    assert job.command == "sac listen"


def test_provider_listen_job_restarts_always() -> None:
    # Arrange — call the registered provider. "always" (not
    # "on-failure") matches the incident-hardened hand-maintained unit
    # (scripts/systemd/sac-listen.service, incident 2026-06-26): a
    # clean 0-exit must still trigger a restart.
    # Act
    job = _job("sac.listen")
    # Assert
    assert job.restart_policy == "always"


def test_provider_listen_job_has_no_watchdog() -> None:
    # Arrange — call the registered provider. sac listen never calls
    # sd_notify(WATCHDOG=1), so requesting a watchdog would cause
    # systemd to kill-and-restart it every interval.
    # Act
    job = _job("sac.listen")
    # Assert
    assert job.watchdog_sec is None
