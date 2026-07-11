"""`sac agents auth-status` command core — the pure `evaluate_agents` mapper
plus command registration/help.

`evaluate_agents` turns each agent's two captured panes into an OK /
LOGIN-REQUIRED verdict via the near-prompt + distance-frozen matcher. These
tests use compact captured-pane fixtures (the matcher itself is exercised in
depth in ``_runners/_tmux/test_auth_status.py``); here we confirm the
command-level real-vs-prose separation and the wiring. No mocks: pure calls +
a real Click invocation.
"""

from __future__ import annotations

from click.testing import CliRunner

from scitex_agent_container.cli_pkg._auth_status import auth_status, evaluate_agents
from scitex_agent_container.cli_pkg.agent_group import agent_group

# Wedged: banner directly above the prompt, identical on both reads → frozen.
_STUCK = "● Login expired · Please run /login\n────────\n❯\n────────\n  ctx:1%\n"
# Healthy: no banner.
_OK = "  continuing the task now\n────────\n❯\n────────\n  ctx:1%\n"


def test_evaluate_agents_flags_frozen_banner_as_login_required():
    # Arrange
    captures = {"scitex-hpc": (_STUCK, _STUCK)}
    # Act
    row = evaluate_agents(captures)[0]
    # Assert
    assert row["verdict"] == "login_required"


def test_evaluate_agents_marks_clean_pane_ok():
    # Arrange
    captures = {"figrecipe": (_OK, _OK)}
    # Act
    row = evaluate_agents(captures)[0]
    # Assert
    assert row["verdict"] == "ok"


def test_evaluate_agents_uncapturable_agent_is_ok_and_uncaptured():
    # Arrange
    captures = {"gone": (None, None)}
    # Act
    row = evaluate_agents(captures)[0]
    # Assert
    assert (row["verdict"], row["captured"]) == ("ok", False)


def test_evaluate_agents_sorts_rows_by_agent_name():
    # Arrange
    captures = {"zeta": (_OK, _OK), "alpha": (_OK, _OK)}
    # Act
    names = [r["agent"] for r in evaluate_agents(captures)]
    # Assert
    assert names == ["alpha", "zeta"]


def test_auth_status_command_registered_under_agents_group():
    # Arrange
    group = agent_group
    # Act
    registered = "auth-status" in group.commands
    # Assert
    assert registered is True


def test_auth_status_help_renders_interval_option():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["auth-status", "--help"])
    # Assert
    assert result.exit_code == 0 and "--interval" in result.output
