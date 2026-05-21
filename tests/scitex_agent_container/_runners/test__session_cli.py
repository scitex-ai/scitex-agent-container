"""CLI parsing for the daemon ``--channels`` flag (sac-node-comms fix).

``_session_cli._parse_argv`` must accept a repeatable ``--channels`` so
``spec.claude.channels`` can be carried into the long-lived runner. When
the set contains ``server:sac`` the runner threads it into
``build_sdk_options`` and the ``sac mcp channel`` adapter is registered.
"""

from __future__ import annotations

import scitex_agent_container._runners.claude_session as runner


def test_parse_argv_channels_absent_defaults_none() -> None:
    # Arrange
    argv = ["--name", "ag"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.channels is None


def test_parse_argv_single_channel_collected_into_list() -> None:
    # Arrange
    argv = ["--name", "ag", "--channels", "server:sac"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.channels == ["server:sac"]


def test_parse_argv_repeated_channels_accumulate() -> None:
    # Arrange
    argv = ["--name", "ag", "--channels", "server:sac", "--channels", "client:x"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.channels == ["server:sac", "client:x"]
