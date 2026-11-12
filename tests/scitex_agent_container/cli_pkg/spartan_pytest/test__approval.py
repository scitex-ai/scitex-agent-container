"""Unapproved submissions refuse before any remote action."""

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.spartan_pytest import pytest_group
from scitex_agent_container.cli_pkg.spartan_pytest._approval import require_run_approval


@pytest.mark.parametrize("assume_yes", ["0", "1"])
def test_submission_requires_explicit_approval(assume_yes):
    # Arrange
    runner = CliRunner(env={"SAC_ASSUME_YES": assume_yes})
    args = ["spartan", "submit-run", "owned/example@main"]

    # Act
    result = runner.invoke(pytest_group, args)

    # Assert
    assert (result.exit_code, result.stdout, "requires --yes/-y" in result.stderr) == (
        2,
        "",
        True,
    )


@pytest.mark.parametrize("flag", ["--yes", "-y"])
def test_preview_precedes_explicit_approval(flag):
    # Arrange
    args = ["spartan", "submit-run", "owned/example@main", flag, "--dry-run"]

    # Act
    result = CliRunner().invoke(pytest_group, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (result.exit_code, plan["scope"], plan["slurm_job_id"]) == (
        0,
        "arguments_only",
        None,
    )


def test_explicit_flag_passes_the_admission_helper():
    # Arrange
    approval = True

    # Act
    result = require_run_approval(approval)

    # Assert
    assert result is None
