"""CLI tests for the ``sac agents`` noun-group.

Covers the muscle-memory ``status`` alias added for foundation-polish
bug 2: the top-level help and the README example reference
``sac agents status``, but only ``list`` existed. ``status`` is now a
peer alias of ``list`` so both verbs land on the same impl.

TQ cleanup: AAA markers per test (TQ002), behaviour-shaped names
(TQ003), one assertion per test (TQ007). No mocks — real ``CliRunner``
against the real Click group.
"""

from __future__ import annotations

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.agent_group import agent_group


def test_agents_status_subcommand_is_registered() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["status", "--help"])
    # Assert — `status` resolves to a real command, not "no such command".
    assert result.exit_code == 0


def test_agents_status_help_lists_under_inspect_category() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["--help"])
    # Assert — `status` shows up in the group help (visible alongside `list`).
    assert "status" in result.output


def test_agents_status_and_list_share_same_callback() -> None:
    # Arrange — both verbs must dispatch to the same underlying impl so
    # behaviour stays in lock-step.
    status_cmd = agent_group.commands["status"]
    list_cmd = agent_group.commands["list"]
    # Act
    same_callback = status_cmd.callback is list_cmd.callback
    # Assert
    assert same_callback


def test_agents_new_command_name_is_retired() -> None:
    # Arrange — the old, narrower `create` was folded into `new`'s
    # dir-template system (card refactor/consolidate-create-into-new-templates),
    # and `new` was then renamed to `create` for CRUD-consistent naming
    # (the CLI already has `delete`). The `new` name is no longer registered.
    # Act
    is_registered = "new" in agent_group.commands
    # Assert
    assert is_registered is False


def test_agents_create_command_is_registered() -> None:
    # Arrange — `create` now names the unified dir-template-aware command
    # (formerly `new`); the vacated old-`create` name was reused.
    # Act
    is_registered = "create" in agent_group.commands
    # Assert
    assert is_registered is True


def test_agents_help_lists_create_under_lifecycle() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["--help"])
    # Assert — the Lifecycle section names `create` (renamed from `new`).
    assert "create" in result.output


def test_agents_usage_command_is_registered() -> None:
    # Arrange
    # Act
    is_registered = "usage" in agent_group.commands
    # Assert
    assert is_registered is True


def test_agents_help_lists_usage_under_inspect() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["--help"])
    # Assert
    assert "usage" in result.output
