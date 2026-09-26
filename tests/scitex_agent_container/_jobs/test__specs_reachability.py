"""The cross-host a2a REACHABILITY JobSpec, pinned.

Mirrors the split of :mod:`scitex_agent_container._jobs._specs_reachability`
on the source side. What these protect: the job is DISCOVERED by the real
``provide_jobs()`` (a job declared in a module nothing splices in is the
inert-feature shape ``_jobs_audit`` exists to catch), its command is exactly
the fleet-wide, recording, JSON form the supervisor's execution log expects,
its kind lowers onto the supervisor at all, and its cadence is the 15 minutes
the design argues for.
"""

from __future__ import annotations

import pytest

jobs_mod = pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)

from scitex_agent_container._jobs._jobs_audit import Verdict, audit_jobs  # noqa: E402

from ._jobspec_helpers import _job, _split_command  # noqa: E402

NAME = "scitex-agent-container-a2a-reachability"


def test_a2a_reachability_job_is_discovered_by_the_provider() -> None:
    # Arrange — `_job` raises unless exactly one declared spec carries NAME.
    # Act
    job = _job(NAME)
    # Assert
    assert job.name == NAME


def test_a2a_reachability_job_kind_is_timer() -> None:
    # Arrange — a wrong kind raises at construction and `ecosystem up` then
    # silently DROPS sac's whole provider.
    # Act
    job = _job(NAME)
    # Assert
    assert job.kind == "timer"


def test_a2a_reachability_command_is_the_fleet_wide_recording_form() -> None:
    # Arrange — EXACT equality on purpose: dropping --all silently probes
    # nothing, dropping --record leaves `--last` reading a stale report,
    # dropping --json leaves the supervisor's log holding a rich table.
    # Act
    job = _job(NAME)
    # Assert
    bound, _payload, rest = _split_command(job.command)
    assert (bound, rest) == (
        "/usr/bin/timeout 180",
        "a2a reachability --all --json --record",
    )


def test_a2a_reachability_cadence_is_every_15_minutes() -> None:
    # Arrange — an alias/token gap is a config fact; 15min bounds how long
    # a peer stays silently unreachable without flooding the log.
    # Act
    job = _job(NAME)
    # Assert
    assert job.on_unit_active_sec == "15min"


def test_a2a_reachability_cron_form_matches_the_timer_cadence() -> None:
    # Arrange — `PeriodicRunner` reads on_unit_active_sec first and falls
    # back to `schedule`; both must say the same thing.
    # Act
    job = _job(NAME)
    # Assert
    assert job.schedule == "*/15 * * * *"


def test_a2a_reachability_job_has_a_live_counterpart_per_the_audit() -> None:
    # Arrange — the inert-feature detector, run against the REAL provider and
    # the REAL entry-point aggregation.
    report = audit_jobs()
    # Act
    inert_for_us = [
        f for f in report.findings if f.subject == NAME and f.verdict is Verdict.INERT
    ]
    # Assert
    assert inert_for_us == []
