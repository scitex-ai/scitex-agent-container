"""Reject invalid cleanup controls before repository discovery or removal."""

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli import main


@pytest.mark.parametrize("command", ["clean", "gc"])
@pytest.mark.parametrize("approval", [[], ["--yes"]])
@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--min-age-hours", "nan"),
        ("--min-age-hours", "inf"),
        ("--min-age-hours", "-inf"),
        ("--min-age-hours", "-1"),
        ("--cap", "-1"),
    ],
)
def test_invalid_cleanup_options_refuse_before_inspection(
    tmp_path, command, approval, option, value
):
    # Arrange
    repo = tmp_path / "missing-owned-repo"
    args = [
        "worktree",
        command,
        "--repo",
        str(repo),
        option,
        value,
        *approval,
        "--json",
    ]

    # Act
    result = CliRunner().invoke(main, args)

    # Assert
    assert (
        result.exit_code,
        result.stdout,
        option in result.stderr,
        repo.exists(),
    ) == (2, "", True, False)
