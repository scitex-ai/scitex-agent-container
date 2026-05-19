"""WI-6 — sac channel was a deprecated tombstone; verify it is gone.

The handoff (HANDOFF_AGENT_COMMS_2026-05-19.md §6 "WI-6 — Delete
deprecated tombstones") mandates removal of the ``sac channel`` group:
its lone ``send`` verb duplicated ``sac peer post-turn`` and shipped
with a ``[DEPRECATED]`` banner. No live in-repo caller; the replacement
is the canonical transport.

This module asserts the deletion stays deleted — adding ``channel``
back would have to land *with* a new behaviour test, not as a
back-compat re-introduction.
"""

from __future__ import annotations

import importlib
from importlib.metadata import PackageNotFoundError

import pytest
from click.testing import CliRunner


def test_channel_group_module_is_deleted() -> None:
    """The ``scitex_agent_container.cli_pkg.channel_group`` module is
    no longer importable. The tombstone is fully removed (handoff §6).
    """
    # Arrange
    target = "scitex_agent_container.cli_pkg.channel_group"
    # Act
    try:
        importlib.import_module(target)
    except ModuleNotFoundError:
        importable = False
    else:
        importable = True
    # Assert
    assert importable is False


def test_main_cli_does_not_advertise_channel_command() -> None:
    """``sac --help`` text must not mention ``channel`` — it would be a
    surfaced tombstone (handoff §0 Hard rules).
    """
    # Arrange
    from scitex_agent_container.cli_pkg._main import main as sac_main

    runner = CliRunner()
    # Act
    result = runner.invoke(sac_main, ["--help"])
    # Assert
    assert "channel" not in result.output.lower(), result.output


def test_sac_channel_invocation_is_unknown_command() -> None:
    """``sac channel send`` must exit non-zero — the group is gone.
    Click reports 'No such command' for the unknown noun.
    """
    # Arrange
    from scitex_agent_container.cli_pkg._main import main as sac_main

    runner = CliRunner()
    # Act
    result = runner.invoke(sac_main, ["channel", "send", "alpha", "hi"])
    # Assert
    assert result.exit_code != 0


def test_lazy_commands_does_not_register_channel() -> None:
    """The LazyGroup mapping must not list ``channel`` either."""
    # Arrange
    from scitex_agent_container.cli_pkg._main import _MainGroup

    # Act
    keys = set(_MainGroup.LAZY_COMMANDS.keys())
    # Assert
    assert "channel" not in keys


# Guard: ensure the package still imports after the deletion. If
# someone removes ``channel_group`` but leaves a dangling reference in
# ``_main.py``, every CLI invocation breaks at import time — catch it
# here rather than in every other test.
def test_main_cli_module_imports_cleanly() -> None:
    # Arrange
    try:
        importlib.import_module("scitex_agent_container.cli_pkg._main")
    except (PackageNotFoundError, Exception) as exc:  # noqa: BLE001
        pytest.fail(f"_main import failed: {exc!r}")
