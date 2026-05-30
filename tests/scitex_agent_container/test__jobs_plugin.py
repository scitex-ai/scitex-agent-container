"""Tests for the ``scitex_dev.jobs`` provider (``_jobs_plugin``).

Verifies the JobSpec sac registers under the ``scitex_dev.jobs``
entry-point group matches the federated contract: a single
``sac.accounts-refresh`` systemd job that runs ``--all --skip-active``
every 2h.

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

from scitex_agent_container._jobs_plugin import provide_jobs


def test_provider_returns_single_job() -> None:
    # Arrange — call the registered provider.
    # Act
    jobs = provide_jobs()
    # Assert
    assert len(jobs) == 1


def test_provider_job_is_real_jobspec() -> None:
    # Arrange — call the registered provider.
    # Act
    job = provide_jobs()[0]
    # Assert — it is the canonical contract type, not a look-alike.
    assert isinstance(job, jobs_mod.JobSpec)


def test_provider_job_name_is_package_prefixed() -> None:
    # Arrange — call the registered provider.
    # Act
    job = provide_jobs()[0]
    # Assert
    assert job.name == "sac.accounts-refresh"


def test_provider_job_command_skips_active() -> None:
    # Arrange — call the registered provider.
    # Act
    job = provide_jobs()[0]
    # Assert — the federated job uses --skip-active to avoid the rotation race.
    assert job.command == "sac accounts refresh --all --skip-active"


def test_provider_job_kind_is_systemd() -> None:
    # Arrange — call the registered provider.
    # Act
    job = provide_jobs()[0]
    # Assert
    assert job.kind == "systemd"


def test_provider_job_cadence_is_two_hours() -> None:
    # Arrange — call the registered provider.
    # Act
    job = provide_jobs()[0]
    # Assert
    assert job.on_unit_active_sec == "2h"
