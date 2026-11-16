"""ADR-0033: ``sac config`` verbs — preview by default, --yes applies."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.config_group import config_group
from tests.scitex_agent_container._neutral_config._layer_fixture import make_layer


@pytest.fixture
def cli_args(tmp_path):
    root = make_layer(tmp_path / "u")
    return [
        "--scope",
        "user",
        "--source",
        str(root),
        "--target",
        str(tmp_path / "claude"),
    ]


def test_deploy_without_yes_only_previews(cli_args, tmp_path):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(config_group, ["deploy", *cli_args])
    # Assert
    assert not (tmp_path / "claude").exists()


def test_deploy_dry_run_with_yes_still_previews(cli_args, tmp_path):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(config_group, ["deploy", *cli_args, "--dry-run", "--yes"])
    # Assert
    assert not (tmp_path / "claude").exists()


def test_deploy_preview_tells_how_to_apply(cli_args):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(config_group, ["deploy", *cli_args])
    # Assert
    assert "re-run with --yes" in result.output


def test_check_exits_1_before_deploy(cli_args):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(config_group, ["check", *cli_args])
    # Assert
    assert result.exit_code == 1


def test_check_exits_0_after_deploy_yes(cli_args):
    # Arrange
    runner = CliRunner()
    runner.invoke(config_group, ["deploy", *cli_args, "--yes"])
    # Act
    result = runner.invoke(config_group, ["check", *cli_args])
    # Assert
    assert result.exit_code == 0


def test_import_without_yes_writes_nothing(tmp_path):
    # Arrange
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "CLAUDE.md").write_text("x\n")
    runner = CliRunner()
    # Act
    runner.invoke(
        config_group,
        ["import", "--claude-dir", str(claude), "--to", str(tmp_path / "out")],
    )
    # Assert
    assert not (tmp_path / "out").exists()


def test_show_on_missing_layer_is_a_usage_error(tmp_path):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(config_group, ["show", "--source", str(tmp_path / "none")])
    # Assert
    assert result.exit_code == 2
