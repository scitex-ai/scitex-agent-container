"""Delegated policy help preserves protocol and performs no generation."""

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli import main


@pytest.mark.parametrize("operation", [[], ["generate-projections"]])
def test_policy_help_uses_full_path_without_reading_missing_policy(tmp_path, operation):
    # Arrange
    missing = tmp_path / "missing.json"
    args = ["worktree", "policy", "--policy", str(missing), *operation, "--help"]
    # Act
    result = CliRunner().invoke(main, args, prog_name="fixture-sac")
    # Assert
    assert (
        result.exit_code,
        "usage: fixture-sac worktree policy" in result.stdout,
        missing.exists() or (tmp_path / "generated").exists(),
        "generated policy projections" in result.stderr,
    ) == (
        0,
        True,
        False,
        False,
    )


def test_policy_argument_error_still_has_typed_json_and_status_three(tmp_path):
    # Arrange
    args = ["worktree", "policy", "inspect"]
    # Act
    result = CliRunner().invoke(main, args, prog_name="fixture-sac")
    payload = json.loads(result.stdout)
    # Assert
    assert (
        result.exit_code,
        payload["allowed"],
        payload["decision"],
        "--repo" in payload["error"],
    ) == (3, False, "error", True)
