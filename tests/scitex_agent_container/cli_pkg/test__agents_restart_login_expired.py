"""``sac agents restart-login-expired`` — command wiring, help, arg validation.

The command is a thin shell over :func:`.._authheal.auth_heal_pass` (whose
behaviour is exercised in depth, with injected panes and a real temp store, in
``tests/scitex_agent_container/_authheal/``). Here we pin only the CLI surface:
it is registered, its help documents the two modes AND the deploy gate, and the
contradictory-flags guard fires BEFORE any pass runs (so these tests never touch
real tmux or the real board).

No mocks: a real Click invocation against the real ``agents`` group.
"""

from __future__ import annotations

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.agent_group import agent_group


def test_command_registered_under_agents_group():
    # Arrange
    group = agent_group
    # Act
    registered = "restart-login-expired" in group.commands
    # Assert
    assert registered is True


def test_help_renders_apply_option():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["restart-login-expired", "--help"])
    # Assert
    assert "--apply" in result.output


def test_help_renders_check_option():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["restart-login-expired", "--help"])
    # Assert
    assert "--check" in result.output


def test_help_warns_about_the_deploy_gate():
    # Arrange — the double-supervisor hazard must be impossible to miss.
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["restart-login-expired", "--help"])
    # Assert
    assert "DEPLOY GATE" in result.output


def test_apply_and_check_are_contradictory():
    # Arrange — the guard must fire before any pass runs, so this never touches
    # tmux or the board.
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["restart-login-expired", "--apply", "--check"])
    # Assert
    assert result.exit_code != 0


def test_apply_and_check_error_explains_why():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["restart-login-expired", "--apply", "--check"])
    # Assert
    assert "contradictory" in result.output
