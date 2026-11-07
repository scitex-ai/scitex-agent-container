"""Canonical read verbs remain discoverable while old invocations still parse."""

import click
import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import main


@pytest.mark.parametrize(
    "group_name, canonical, legacy",
    [
        ("agents", "list-roles", "roles"),
        ("a2a", "list-grants", "grants"),
    ],
)
def test_canonical_read_command_and_legacy_help_remain_available(
    group_name, canonical, legacy
):
    # Arrange — help inspection must never call a live agent or database.
    runner = CliRunner()
    context = click.Context(main)
    group = main.get_command(context, group_name)
    child_context = click.Context(group, info_name=group_name, parent=context)
    # Act
    current = runner.invoke(main, [group_name, canonical, "--help"])
    old = runner.invoke(main, [group_name, legacy, "--help"])
    canonical_command = group.get_command(child_context, canonical)
    legacy_command = group.get_command(child_context, legacy)
    # Assert
    assert (
        current.exit_code,
        old.exit_code,
        canonical_command.hidden,
        legacy_command.hidden,
        canonical_command.callback is legacy_command.callback,
    ) == (
        0,
        0,
        False,
        True,
        True,
    )


def test_role_json_is_identical_through_canonical_and_legacy_commands(tmp_path):
    # Arrange — a real empty definition directory, not the operator's fleet.
    import json

    runner = CliRunner()
    arguments = ["--dir", str(tmp_path), "--json"]
    # Act
    current = runner.invoke(main, ["agents", "list-roles", *arguments])
    legacy = runner.invoke(main, ["agents", "roles", *arguments])
    current_data = json.loads(current.stdout)
    legacy_data = json.loads(legacy.stdout)
    # Assert
    assert (
        current.exit_code == legacy.exit_code == 0
        and current_data == legacy_data
        and current_data["total"] == 0
    )
