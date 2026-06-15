"""Bug regression: TUI ``--mcp-config`` must emit one flag per value.

P0 operator-reported (2026-06-15): figrecipe + todo + neurovista's TUI
panes showed::

    server:claude-code-telegrammer,server:sac · no MCP server configured
    with that name

The ``--dangerously-load-development-channels`` flag was being passed
(dev-channels load), but the matching MCP servers were absent from the
session — telegram reply impossible, a2a impossible. Root cause: the
``_tui_runner_argv`` joined ``mcp_config`` (the workspace ``.mcp.json``
path) and ``channel_mcp`` (the inline ``sac mcp channel`` JSON) onto a
SINGLE ``--mcp-config`` flag with two space-separated values. ``claude
--help`` documents that syntax (``--mcp-config <configs...>``) but the
real binary only honoured the first value, dropping the second silently.

The fix emits ONE ``--mcp-config`` flag per value, matching the SDK
runtime's repeated-flag pattern.

STX-TQ002 AAA-marker + STX-TQ007 one-assert + PA-306 no-mock-fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scitex_agent_container.runtimes._apptainer_inner_argv import (
    build_inner_argv,
)


@dataclass
class _ClaudeSpec:
    model: str = "sonnet"
    flags: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)


@dataclass
class _Config:
    name: str = "agt"
    workdir: str = "/tmp/agt"
    kind: str = "Agent"
    startup_commands: list = field(default_factory=list)
    startup_prompts: list = field(default_factory=list)
    claude: _ClaudeSpec = field(default_factory=_ClaudeSpec)


def _count_flag_occurrences(argv: list[str], flag: str) -> int:
    return sum(1 for a in argv if a == flag)


def _values_after_flag(argv: list[str], flag: str) -> list[str]:
    """Return every value that immediately follows ``flag`` in ``argv``.

    With one ``--mcp-config`` per value (the desired shape) this returns
    each value as a separate element. With the BUGGY single-flag shape
    it returns only the first value, because the second was an arg of
    ``argv`` but not adjacent to its own ``--mcp-config`` flag.
    """
    out: list[str] = []
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            out.append(argv[i + 1])
    return out


def test_tui_runner_emits_one_mcp_config_flag_per_value() -> None:
    # Arrange — both an mcp_config path and a channel_mcp JSON.
    config = _Config()
    # Act
    argv = build_inner_argv(
        config,
        tui=True,
        tui_mcp_config="/home/agent/.mcp.json",
        tui_channel_mcp='{"mcpServers":{"sac":{}}}',
    )
    # Assert — exactly two ``--mcp-config`` flags (one per value), not
    # one flag with two space-separated values (the operator-reported
    # silent-drop shape).
    assert _count_flag_occurrences(argv, "--mcp-config") == 2


def test_tui_runner_each_mcp_config_value_immediately_follows_its_flag() -> None:
    # Arrange
    config = _Config()
    # Act
    argv = build_inner_argv(
        config,
        tui=True,
        tui_mcp_config="/home/agent/.mcp.json",
        tui_channel_mcp='{"mcpServers":{"sac":{}}}',
    )
    # Assert — each value sits adjacent to its OWN ``--mcp-config``.
    # The buggy shape would put both behind a single flag and the second
    # would be unreachable via this lookup (returns only one entry).
    values = _values_after_flag(argv, "--mcp-config")
    assert values == [
        "/home/agent/.mcp.json",
        '{"mcpServers":{"sac":{}}}',
    ]


def test_tui_runner_single_mcp_config_value_still_emits_one_flag() -> None:
    # Arrange — only the workspace .mcp.json, no channel inline JSON.
    config = _Config()
    # Act
    argv = build_inner_argv(
        config,
        tui=True,
        tui_mcp_config="/home/agent/.mcp.json",
        tui_channel_mcp=None,
    )
    # Assert
    assert _count_flag_occurrences(argv, "--mcp-config") == 1


def test_tui_runner_only_channel_mcp_still_emits_one_flag() -> None:
    # Arrange — only the inline sac-channel JSON, no workspace file.
    config = _Config()
    # Act
    argv = build_inner_argv(
        config,
        tui=True,
        tui_mcp_config=None,
        tui_channel_mcp='{"mcpServers":{"sac":{}}}',
    )
    # Assert
    assert _count_flag_occurrences(argv, "--mcp-config") == 1


def test_tui_runner_no_mcp_config_emits_no_flag() -> None:
    # Arrange — neither path nor JSON.
    config = _Config()
    # Act
    argv = build_inner_argv(
        config,
        tui=True,
        tui_mcp_config=None,
        tui_channel_mcp=None,
    )
    # Assert
    assert _count_flag_occurrences(argv, "--mcp-config") == 0


def test_tui_runner_mcp_config_value_is_not_a_joined_pair() -> None:
    # Arrange — guard against the SPECIFIC buggy shape
    # ``["--mcp-config", "<path> <json>"]`` (or
    # ``["--mcp-config", "<path>", "<json>"]`` interpreted as ONE flag with
    # two values — claude only picked up the first).
    config = _Config()
    # Act
    argv = build_inner_argv(
        config,
        tui=True,
        tui_mcp_config="/home/agent/.mcp.json",
        tui_channel_mcp='{"mcpServers":{"sac":{}}}',
    )
    # Assert — no single argv element contains BOTH the path and the
    # JSON (the smoking-gun of the joined-string regression).
    joined_entries = [
        a for a in argv if "/home/agent/.mcp.json" in a and "mcpServers" in a
    ]
    assert joined_entries == []
