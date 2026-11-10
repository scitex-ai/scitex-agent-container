"""Projection writes need explicit approval; previews never authorize edits."""

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli import main


@pytest.mark.parametrize("assume_yes", ["", "1"])
def test_unapproved_generation_refuses_before_defaults(tmp_path, assume_yes):
    # Arrange
    home = tmp_path / "owned-state"
    output = tmp_path / "owned-output"
    runner = CliRunner(
        env={"SCITEX_AGENT_CONTAINER_HOME": str(home), "SAC_ASSUME_YES": assume_yes}
    )
    args = ["worktree", "policy", "generate-projections", "--output-dir", str(output)]

    # Act
    result = runner.invoke(main, args)
    payload = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        payload["allowed"],
        payload["decision"],
        "--yes" in payload["error"],
        home.exists(),
        output.exists(),
    ) == (3, False, "error", True, False, False)


def test_unapproved_generation_does_not_read_invalid_policy(tmp_path):
    # Arrange
    policy = tmp_path / "invalid.json"
    policy.write_text("owned invalid JSON")
    args = ["worktree", "policy", "--policy", str(policy), "generate-projections"]

    # Act
    result = CliRunner().invoke(main, args)
    payload = json.loads(result.stdout)

    # Assert
    assert (result.exit_code, "--yes" in payload["error"], policy.read_text()) == (
        3,
        True,
        "owned invalid JSON",
    )


def test_approved_preview_still_does_not_generate(tmp_path):
    # Arrange
    home = tmp_path / "owned-state"
    runner = CliRunner(env={"SCITEX_AGENT_CONTAINER_HOME": str(home)})
    args = ["worktree", "policy", "generate-projections", "--dry-run", "--yes"]

    # Act
    result = runner.invoke(main, args)
    payload = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        payload["decision"],
        payload["allowed"],
        home.exists(),
    ) == (
        0,
        "preview",
        False,
        False,
    )
