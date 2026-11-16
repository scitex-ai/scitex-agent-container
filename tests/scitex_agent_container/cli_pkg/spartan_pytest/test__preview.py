"""Argument plans do not resolve a remote filesystem or submit jobs."""

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.spartan_pytest import pytest_group


def test_preview_leaves_remote_paths_and_job_identity_unresolved():
    # Arrange
    args = ["spartan", "submit-run", "owned/example@main", "--dry-run"]

    # Act
    result = CliRunner().invoke(pytest_group, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        plan["scope"],
        plan["repo"],
        plan["branch"],
        plan["scratch_root"],
        plan["artifact_directory"],
        plan["slurm_job_id"],
        "status" in plan,
    ) == (
        0,
        "arguments_only",
        "owned/example",
        "main",
        "$SCRATCH/sac-pytest",
        None,
        None,
        False,
    )


def test_preview_reports_supplied_options_without_remote_expansion():
    # Arrange
    args = [
        "spartan",
        "submit-run",
        "owned/example@feature/topic",
        "--dry-run",
        "--ssh-host",
        "owned-peer",
        "--reservation",
        "owned-pool",
        "--timeout",
        "9",
        "--poll-interval",
        "2",
        "--scratch-root",
        "$SCRATCH/owned path",
    ]

    # Act
    result = CliRunner().invoke(pytest_group, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        plan["ssh_host"],
        plan["reservation"],
        plan["timeout_s"],
        plan["poll_interval_s"],
        plan["scratch_root"],
        plan["branch"],
    ) == (0, "owned-peer", "owned-pool", 9, 2, "$SCRATCH/owned path", "feature/topic")


@pytest.mark.parametrize("flag", ["--timeout", "--poll-interval"])
def test_preview_preserves_positive_deadline_admission(flag):
    # Arrange
    args = ["spartan", "submit-run", "owned/example@main", "--dry-run", flag, "0"]

    # Act
    result = CliRunner().invoke(pytest_group, args)

    # Assert
    assert (result.exit_code, "x>=1" in result.stderr, result.stdout) == (2, True, "")


@pytest.mark.parametrize("control", ["\r", "\n", "\x00"])
def test_preview_preserves_scheduler_header_admission(control):
    # Arrange
    args = [
        "spartan",
        "submit-run",
        "owned/example@main",
        "--dry-run",
        "--reservation",
        "owned" + control,
    ]

    # Act
    result = CliRunner().invoke(pytest_group, args)

    # Assert
    assert (result.exit_code, "reservation" in result.stderr, result.stdout) == (
        2,
        True,
        "",
    )


def test_preview_preserves_target_format_admission():
    # Arrange
    args = ["spartan", "submit-run", "bare-repo", "--dry-run"]

    # Act
    result = CliRunner().invoke(pytest_group, args)

    # Assert
    assert (result.exit_code, "REPO@BRANCH" in result.stderr, result.stdout) == (
        2,
        True,
        "",
    )
