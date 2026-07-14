"""The four conditions, driven by REAL recorded evidence (PA-306: no mocks).

``StaticSources`` is a genuine implementation of the ``Sources`` protocol
whose backing store is a dict rather than a network — the checks run their
real code path against the real bytes PyPI, git and gh actually returned.
No monkeypatching, no network.

The test that earns this file its place is
``test_would_have_caught_the_incident``: it replays the fleet exactly as it
stood at 2026-07-13 23:30 and asserts the alarm fires. Everything else here
is scaffolding around that one question.
"""

from __future__ import annotations

from scitex_agent_container._freshness._checks import (
    build_report,
    check_ghost_tags,
    check_host_behind_pypi,
    check_release_runs,
    check_running_vs_installed,
)
from scitex_agent_container._freshness._model import Freshness
from scitex_agent_container._freshness._sources import StaticSources

from ._real_data import (
    ALL_GHOSTS,
    AT_INCIDENT_GIT_TAGS,
    AT_INCIDENT_PYPI_LATEST,
    AT_INCIDENT_PYPI_RELEASES,
    AT_INCIDENT_RELEASE_RUNS,
    GIT_TAGS,
    HOST_INSTALLED,
    KNOWN_GHOSTS,
    PYPI_LATEST,
    PYPI_RELEASES,
    RELEASE_RUNS,
)

UNIT = "sac-listen.service"


# ---------------------------------------------------------------------------
# THE REGRESSION TEST. This is the whole point of the feature.
# ---------------------------------------------------------------------------


def test_would_have_caught_the_incident_as_stale():
    """Replaying 2026-07-13 23:30, the alarm must fire.

    Tag v0.21.16 exists, its release run FAILED, PyPI is still on 0.21.14
    and so is the host. The fleet then spent a day re-diagnosing an
    already-fixed bug because nothing said a word. If this assertion ever
    goes green-as-FRESH, the alarm is worthless.
    """
    # Arrange — the fleet exactly as it stood, from real recorded data.
    sources = StaticSources(
        pypi_versions=AT_INCIDENT_PYPI_RELEASES,
        pypi_latest=AT_INCIDENT_PYPI_LATEST,
        installed_version=HOST_INSTALLED,
        git_tags=AT_INCIDENT_GIT_TAGS,
        release_runs=AT_INCIDENT_RELEASE_RUNS,
        installed_at=1_000.0,
        daemon_started_at=900.0,
    )

    # Act
    report = build_report(sources, unit=UNIT, now=1.0)

    # Assert
    assert report.state is Freshness.STALE


def test_incident_names_the_ghost_tag():
    """The alarm must name v0.21.16 — the tag that looked shipped and wasn't."""
    # Arrange
    # Act
    finding = check_ghost_tags(
        AT_INCIDENT_GIT_TAGS, AT_INCIDENT_PYPI_RELEASES
    )

    # Assert
    assert "v0.21.16" in finding.summary


def test_incident_head_ghost_is_stale():
    """A head tag with no PyPI release is positive evidence, not a shrug."""
    # Arrange
    # Act
    finding = check_ghost_tags(AT_INCIDENT_GIT_TAGS, AT_INCIDENT_PYPI_RELEASES)

    # Assert
    assert finding.state is Freshness.STALE


# ---------------------------------------------------------------------------
# ghost-tag
# ---------------------------------------------------------------------------


def test_ghost_tags_lists_both_known_ghosts():
    """v0.21.15 and v0.21.16 are ghosts today and must be reported as such."""
    # Arrange
    # Act
    finding = check_ghost_tags(GIT_TAGS, PYPI_RELEASES)

    # Assert
    assert set(KNOWN_GHOSTS).issubset(set(finding.data["ghosts"]))


def test_superseded_ghosts_raise_no_alarm():
    """Once a later version ships, an old ghost has no remedy — so no alarm.

    It stays visible in the report; it just stops shouting. An alarm with
    no action attached is noise, and noise is how a check gets muted.
    """
    # Arrange
    # Act
    finding = check_ghost_tags(GIT_TAGS, PYPI_RELEASES)

    # Assert
    assert finding.state is Freshness.FRESH


def test_ghost_tags_finds_every_real_ghost():
    """SEVEN tags never shipped, not the two anyone remembered.

    v0.17.1, v0.21.6, v0.21.8, v0.21.10, v0.21.12, v0.21.15, v0.21.16.
    The drift is systemic, not a one-off — which is the real answer to
    "how many times is this?"
    """
    # Arrange
    # Act
    finding = check_ghost_tags(GIT_TAGS, PYPI_RELEASES)

    # Assert
    assert set(finding.data["ghosts"]) == ALL_GHOSTS


def test_ghost_tags_unknown_without_pypi():
    """No PyPI answer means UNKNOWN — never 'no ghosts'."""
    # Arrange
    # Act
    finding = check_ghost_tags(GIT_TAGS, None)

    # Assert
    assert finding.state is Freshness.UNKNOWN


def test_ghost_tags_unknown_without_checkout():
    """A wheel-only host cannot read tags. That is UNKNOWN, not clean."""
    # Arrange
    # Act
    finding = check_ghost_tags(None, PYPI_RELEASES)

    # Assert
    assert finding.state is Freshness.UNKNOWN


# ---------------------------------------------------------------------------
# host-behind-pypi
# ---------------------------------------------------------------------------


def test_host_behind_pypi_is_stale():
    """The live case: host on 0.21.14, PyPI on 0.21.17."""
    # Arrange
    # Act
    finding = check_host_behind_pypi(HOST_INSTALLED, PYPI_LATEST)

    # Assert
    assert finding.state is Freshness.STALE


def test_host_behind_pypi_names_the_remedy():
    """An alarm without a fix command is one people learn to skip."""
    # Arrange
    # Act
    finding = check_host_behind_pypi(HOST_INSTALLED, PYPI_LATEST, python="/v/bin/python")

    # Assert
    assert finding.remedy == "/v/bin/python -m pip install -U 'scitex-agent-container==0.21.17'"


def test_host_current_with_pypi_is_fresh():
    # Arrange
    # Act
    finding = check_host_behind_pypi(PYPI_LATEST, PYPI_LATEST)

    # Assert
    assert finding.state is Freshness.FRESH


def test_host_ahead_of_pypi_is_not_stale():
    """A dev build ahead of PyPI is not behind it.

    Warning a developer that their own unpublished build is 'stale' is
    exactly the false positive that gets a version check disabled.
    """
    # Arrange
    # Act
    finding = check_host_behind_pypi("0.21.18", PYPI_LATEST)

    # Assert
    assert finding.state is Freshness.FRESH


def test_host_behind_unknown_when_offline():
    """Offline is UNKNOWN. It is emphatically not 'up to date'."""
    # Arrange
    # Act
    finding = check_host_behind_pypi(HOST_INSTALLED, None)

    # Assert
    assert finding.state is Freshness.UNKNOWN


def test_host_behind_unknown_on_unparseable_version():
    """We refuse to order what we cannot parse."""
    # Arrange
    # Act
    finding = check_host_behind_pypi("dev", PYPI_LATEST)

    # Assert
    assert finding.state is Freshness.UNKNOWN


# ---------------------------------------------------------------------------
# running-vs-installed
# ---------------------------------------------------------------------------


def test_daemon_older_than_install_is_stale():
    """Installed is not running: the upgrade landed after the daemon booted."""
    # Arrange — daemon up at t=1000, package written at t=5000.
    # Act
    finding = check_running_vs_installed(1_000.0, 5_000.0, unit=UNIT)

    # Assert
    assert finding.state is Freshness.STALE


def test_daemon_restart_is_the_remedy():
    """Upgrading alone changes nothing; only a restart reloads the code."""
    # Arrange
    # Act
    finding = check_running_vs_installed(1_000.0, 5_000.0, unit=UNIT)

    # Assert
    assert finding.remedy == f"systemctl --user restart {UNIT}"


def test_daemon_newer_than_install_is_fresh():
    # Arrange — package written at t=1000, daemon (re)started at t=5000.
    # Act
    finding = check_running_vs_installed(5_000.0, 1_000.0, unit=UNIT)

    # Assert
    assert finding.state is Freshness.FRESH


def test_daemon_not_running_is_unknown():
    """A daemon that is not running is not running STALE code."""
    # Arrange
    # Act
    finding = check_running_vs_installed(None, 5_000.0, unit=UNIT)

    # Assert
    assert finding.state is Freshness.UNKNOWN


# ---------------------------------------------------------------------------
# release-run
# ---------------------------------------------------------------------------


def test_failed_release_run_is_stale():
    """v0.21.16's run FAILED — build/publish never ran, nothing shipped."""
    # Arrange
    # Act
    finding = check_release_runs(AT_INCIDENT_RELEASE_RUNS)

    # Assert
    assert finding.state is Freshness.STALE


def test_cancelled_release_run_is_stale():
    """v0.21.15's run was CANCELLED, not failed. It shipped just as little.

    'Not success' is one class precisely because these were the two real
    cases, and a check that only looked for 'failure' would have missed one.
    """
    # Arrange
    cancelled_only = [
        r for r in RELEASE_RUNS if r["headBranch"] in ("v0.21.15", "v0.21.14")
    ]

    # Act
    finding = check_release_runs(cancelled_only)

    # Assert
    assert finding.state is Freshness.STALE


def test_successful_release_run_is_fresh():
    # Arrange
    # Act
    finding = check_release_runs(RELEASE_RUNS)

    # Assert
    assert finding.state is Freshness.FRESH


def test_release_run_unknown_without_gh():
    # Arrange
    # Act
    finding = check_release_runs(None)

    # Assert
    assert finding.state is Freshness.UNKNOWN


def test_in_flight_run_is_not_judged():
    """A run still going says nothing yet about whether it will ship."""
    # Arrange
    running = [{"status": "in_progress", "conclusion": None, "headBranch": "v9.9.9"}]

    # Act
    finding = check_release_runs(running)

    # Assert
    assert finding.state is Freshness.UNKNOWN


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------


def test_blind_report_is_unknown_not_fresh():
    """Every source dark => UNKNOWN. This is the anti-false-green test.

    A report that can see nothing must never summarise itself as fine.
    """
    # Arrange
    sources = StaticSources()

    # Act
    report = build_report(sources, unit=UNIT, now=1.0)

    # Assert
    assert report.state is not Freshness.FRESH


def test_one_stale_finding_makes_report_stale():
    """Positive evidence of a problem outranks any number of clean checks."""
    # Arrange — everything current except the host, which is behind.
    sources = StaticSources(
        pypi_versions=PYPI_RELEASES,
        pypi_latest=PYPI_LATEST,
        installed_version=HOST_INSTALLED,
        git_tags=GIT_TAGS,
        release_runs=RELEASE_RUNS,
        installed_at=1_000.0,
        daemon_started_at=5_000.0,
    )

    # Act
    report = build_report(sources, unit=UNIT, now=1.0)

    # Assert
    assert report.state is Freshness.STALE


def test_stale_findings_exclude_unknown():
    """Only STALE is actionable. UNKNOWN must never reach the alarm."""
    # Arrange — PyPI dark, so most checks are UNKNOWN.
    sources = StaticSources(installed_version=HOST_INSTALLED, git_tags=GIT_TAGS)

    # Act
    report = build_report(sources, unit=UNIT, now=1.0)

    # Assert
    assert report.stale == ()


# EOF
