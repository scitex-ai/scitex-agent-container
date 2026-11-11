"""Projection previews are plans, never edit authorization or generation."""

import json

from click.testing import CliRunner

from scitex_agent_container.cli import main


def test_projection_preview_leaves_defaults_unresolved(tmp_path):
    # Arrange
    home = tmp_path / "owned-state"
    runner = CliRunner(env={"SCITEX_AGENT_CONTAINER_HOME": str(home)})
    args = ["worktree", "policy", "generate-projections", "--dry-run"]

    # Act
    result = runner.invoke(main, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        plan["allowed"],
        plan["decision"],
        plan["policy"],
        plan["output_dir"],
        home.exists(),
    ) == (
        0,
        False,
        "preview",
        None,
        None,
        False,
    )


def test_projection_preview_accepts_unread_invalid_policy(tmp_path):
    # Arrange
    policy = tmp_path / "invalid policy.json"
    policy.write_text("owned invalid JSON must remain untouched")
    output = tmp_path / "unresolved output"
    args = [
        "worktree",
        "policy",
        "--policy",
        str(policy),
        "generate-projections",
        "--output-dir",
        str(output),
        "--dry-run",
    ]

    # Act
    result = CliRunner().invoke(main, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        plan["scope"],
        plan["policy"],
        plan["output_dir"],
        output.exists(),
        policy.read_text(),
        "policy_sha256" in plan,
    ) == (
        0,
        "arguments_only",
        str(policy),
        str(output),
        False,
        "owned invalid JSON must remain untouched",
        False,
    )


def test_projection_preview_does_not_select_existing_host_policy(tmp_path):
    # Arrange
    state = tmp_path / "owned-state"
    policy = state / "worktree-policy" / "worktree-policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text("invalid JSON")
    runner = CliRunner(env={"SCITEX_AGENT_CONTAINER_HOME": str(state)})
    args = ["worktree", "policy", "generate-projections", "--dry-run"]

    # Act
    result = runner.invoke(main, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        plan["policy"],
        plan["allowed"],
        policy.read_text(),
        (policy.parent / "generated").exists(),
    ) == (
        0,
        None,
        False,
        "invalid JSON",
        False,
    )


def test_projection_preview_preserves_typed_unknown_argument_error(tmp_path):
    # Arrange
    args = ["worktree", "policy", "generate-projections", "--dry-run", "--unknown"]

    # Act
    result = CliRunner().invoke(main, args)
    payload = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        payload["allowed"],
        payload["decision"],
        "--unknown" in payload["error"],
    ) == (
        3,
        False,
        "error",
        True,
    )


def test_projection_help_describes_preview_without_generating(tmp_path):
    # Arrange
    state = tmp_path / "owned-state"
    runner = CliRunner(env={"SCITEX_AGENT_CONTAINER_HOME": str(state)})
    args = ["worktree", "policy", "generate-projections", "--help"]

    # Act
    result = runner.invoke(main, args, prog_name="owned-sac")

    # Assert
    assert (
        result.exit_code,
        "--dry-run" in result.stdout,
        "owned-sac worktree policy" in result.stdout,
        state.exists(),
    ) == (
        0,
        True,
        True,
        False,
    )
