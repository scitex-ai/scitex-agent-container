"""``sac accounts --help`` renders its verbs under named sections.

Fifteen verbs in one flat alphabetical column made ``list`` / ``status`` /
``quota`` look interchangeable and hid which of them WRITE. The interface spec
(general/03_interface_02_cli §6) asks for sections and ``sac agents`` already
renders them.

The two population guards matter more than the rendering test: a category that
names a verb which no longer exists renders nothing and fails silently, and a
verb nobody categorised disappears into ``Other`` unnoticed.
"""

from __future__ import annotations

import click
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.account_group import _AccountsGroup, account


# --- the sections ----------------------------------------------------------


def test_accounts_help_renders_named_sections():
    # Arrange
    runner = CliRunner()

    # Act
    result = runner.invoke(account, ["--help"])

    # Assert
    assert "Distribute to peers:" in result.stdout


def test_every_categorised_name_is_a_real_command():
    # Arrange — a category naming a verb that does not exist would silently
    # render nothing, which is how a section quietly goes empty after a rename
    ctx = click.Context(account)
    listed = {n for _, names in _AccountsGroup.COMMAND_CATEGORIES for n in names}

    # Act
    missing = sorted(n for n in listed if account.get_command(ctx, n) is None)

    # Assert
    assert missing == []


def test_no_visible_command_falls_through_to_other():
    # Arrange — `Other` is the catch-all; a VISIBLE verb landing there is one
    # somebody forgot to categorise. Hidden verbs (the `keepalive` alias) are
    # excluded on the same predicate the formatter itself uses, so this tracks
    # what a reader actually sees rather than what the group happens to hold.
    ctx = click.Context(account)
    categorised = {n for _, names in _AccountsGroup.COMMAND_CATEGORIES for n in names}
    visible = {
        n
        for n in account.list_commands(ctx)
        if not getattr(account.get_command(ctx, n), "hidden", False)
    }

    # Act
    uncategorised = sorted(visible - categorised)

    # Assert
    assert uncategorised == []
