"""Tests for the click command surface in :mod:`spartan_pytest._run_cmd`.

CliRunner drives the actual click group — no mocks. The opt-in live
round-trip is gated behind ``$SAC_SPARTAN_HOST`` so the default suite
never reaches out to a remote SLURM cluster from a laptop.

Style: STX-TQ002 AAA markers, STX-TQ007 one assert per test, no mocks.
"""

from __future__ import annotations

import os

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.spartan_pytest import pytest_group


def test_pytest_group_help_lists_spartan_subgroup():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(pytest_group, ["--help"])
    # Assert
    assert "spartan" in result.output


def test_spartan_run_help_documents_reservation_flag():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(pytest_group, ["spartan", "run", "--help"])
    # Assert
    assert "--reservation" in result.output


def test_spartan_run_rejects_target_without_at_sign():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(pytest_group, ["spartan", "run", "bare-repo"])
    # Assert — UsageError translates to non-zero exit + the format hint.
    assert "REPO@BRANCH" in result.output


@pytest.mark.skipif(
    not os.environ.get("SAC_SPARTAN_HOST"),
    reason="Set SAC_SPARTAN_HOST=<ssh-alias> to run the real Spartan round-trip.",
)
def test_real_spartan_round_trip_against_live_host():
    """End-to-end smoke against a live Spartan reservation.

    Reserved for opt-in CI/local runs; default suite skips this so
    laptop pytest never reaches out to a remote SLURM cluster.
    """
    # Arrange
    target = os.environ.get(
        "SAC_SPARTAN_SMOKE_TARGET", "ywatanabe1989/scitex-agent-container@develop"
    )
    host = os.environ["SAC_SPARTAN_HOST"]
    runner = CliRunner()
    # Act
    result = runner.invoke(
        pytest_group,
        ["spartan", "run", target, "--ssh-host", host, "--timeout", "1800"],
    )
    # Assert — either green or operator-meaningful failure surface.
    assert "Spartan pytest" in result.output
