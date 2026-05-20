"""Regression guard: every CLI-backed MCP tool dispatches a subcommand
token that actually exists on the live ``sac`` click group.

This is the drift-prevention test for the released ``agent`` → ``agents``
CLI-group rename. The MCP layer in ``_agent.py`` had frozen the old
singular ``agent`` group name (and several since-removed subcommands)
into its argv, so every ``agent_*`` tool that shelled to the CLI failed
with ``No such command 'agent'``.

Rather than re-hardcode the expected tokens here (which would rot the
same way), we *introspect* the live click tree from
``cli_pkg._main.main`` and assert that whatever first/second argv tokens
each tool emits resolve to a real ``GROUP`` / ``GROUP SUBCOMMAND`` pair.

No mocks (STX-NM002): we swap ``invoke_cli_{json,text}`` on each leaf
module with real capturing callables via a save/restore context
manager, exactly like ``test___init__.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import click
import pytest

from scitex_agent_container._mcp._tools import _agent
from scitex_agent_container._mcp._tools import _helpers as _h
from scitex_agent_container.cli_pkg._main import main


@contextmanager
def _recording() -> Iterator[list[list[str]]]:
    """Swap invoke_cli_{json,text} on the agent leaf module + the shared
    helpers module with real capturing callables. Yields the list of
    captured argv lists (one per tool invocation). No mocks."""
    captured: list[list[str]] = []

    def fake_json(argv):
        captured.append(list(argv))
        return {"exit_code": 0, "data": None, "stdout": ""}

    def fake_text(argv):
        captured.append(list(argv))
        return {"exit_code": 0, "stdout": "ok"}

    saved: list[tuple[object, str, object]] = []
    for mod in (_agent, _h):
        if hasattr(mod, "invoke_cli_json"):
            saved.append((mod, "invoke_cli_json", mod.invoke_cli_json))
            mod.invoke_cli_json = fake_json
        if hasattr(mod, "invoke_cli_text"):
            saved.append((mod, "invoke_cli_text", mod.invoke_cli_text))
            mod.invoke_cli_text = fake_text
    try:
        yield captured
    finally:
        for mod, attr, orig in saved:
            setattr(mod, attr, orig)


def _live_subcommands(group_name: str) -> set[str]:
    """Introspect the live click tree: subcommand tokens under
    ``sac <group_name>``. Reads the real lazy group, not a hardcoded
    list, so the guard tracks the CLI surface as it evolves."""
    ctx = click.Context(main)
    group = main.get_command(ctx, group_name)
    assert group is not None, f"sac has no '{group_name}' group"
    gctx = click.Context(group, parent=ctx)
    return set(group.list_commands(gctx))


def _live_groups() -> set[str]:
    """Top-level command/group tokens under ``sac``."""
    ctx = click.Context(main)
    return set(main.list_commands(ctx))


def _capture_argv(fn, args) -> list[str]:
    """Fire one CLI-backed tool and return the argv it dispatched."""
    with _recording() as captured:
        fn(*args)
    assert captured, f"{fn.__name__} did not dispatch any CLI invocation"
    return captured[-1]


# Every CLI-backed agent_* tool, with the arg it needs to fire once.
# id == tool name so a failure names the broken tool directly.
_AGENT_TOOLS = [
    pytest.param(_agent.agent_list, (), id="agent_list"),
    pytest.param(_agent.agent_status, ("x",), id="agent_status"),
    pytest.param(_agent.agent_logs, ("x",), id="agent_logs"),
    pytest.param(_agent.agent_health, ("x",), id="agent_health"),
    pytest.param(_agent.agent_find, ("x",), id="agent_find"),
    pytest.param(_agent.agent_check, ("x",), id="agent_check"),
    pytest.param(_agent.agent_recall, ("x",), id="agent_recall"),
    pytest.param(_agent.agent_start, ("x",), id="agent_start"),
    pytest.param(_agent.agent_stop, ("x",), id="agent_stop"),
    pytest.param(_agent.agent_restart, ("x",), id="agent_restart"),
]


@pytest.mark.parametrize(("fn", "args"), _AGENT_TOOLS)
def test_agent_tool_dispatches_a_real_top_level_sac_group(fn, args):
    # Arrange
    groups = _live_groups()
    # Act
    argv = _capture_argv(fn, args)
    # Assert
    assert argv[0] in groups, (
        f"{fn.__name__} dispatches unknown sac group {argv[0]!r}; "
        f"live groups: {sorted(groups)}"
    )


@pytest.mark.parametrize(("fn", "args"), _AGENT_TOOLS)
def test_agent_tool_targets_the_plural_agents_group(fn, args):
    # Arrange
    # (no fixture state — the rename target is the literal 'agents')
    expected_group = "agents"
    # Act
    argv = _capture_argv(fn, args)
    # Assert
    assert argv[0] == expected_group, (
        f"{fn.__name__} no longer targets the 'agents' group: {argv}"
    )


@pytest.mark.parametrize(("fn", "args"), _AGENT_TOOLS)
def test_agent_tool_dispatches_a_live_agents_subcommand(fn, args):
    # Arrange
    agents_subs = _live_subcommands("agents")
    # Act
    argv = _capture_argv(fn, args)
    # Assert
    assert argv[1] in agents_subs, (
        f"{fn.__name__} dispatches removed/renamed subcommand "
        f"{argv[1]!r}; live agents subcommands: {sorted(agents_subs)}"
    )
