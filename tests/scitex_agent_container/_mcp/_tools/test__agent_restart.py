"""Tests for the MCP ``agent_restart`` tool wrapper.

The CLI ``sac agents restart <name>`` refuses to run without
``--yes/-y`` (exit 2). An MCP tool call has no TTY to confirm on, so
the wrapper must pass ``--yes`` itself — otherwise the documented MCP
surface dead-ends on a guard that can never be satisfied. This guards
that regression.

PA-306 / STX-NM002: no ``unittest.mock``, no ``monkeypatch``. The
``invoke_cli_text`` collaborator is swapped on the ``_agent`` module
namespace via a real save/restore context manager — mirrors the
``test_mcp_cli_subcommand_parity.py`` pattern.

STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from scitex_agent_container._mcp._tools import _agent
from scitex_agent_container._mcp._tools._agent import agent_restart, agent_stop


@contextmanager
def _record_argv() -> Iterator[list[list[str]]]:
    """Swap ``_agent.invoke_cli_text`` with a recording fake.

    Yields the list of captured argv lists (one per wrapper call). No
    mocks — the original is saved and restored.
    """
    captured: list[list[str]] = []

    def fake_text(argv):
        captured.append(list(argv))
        return {"exit_code": 0, "stdout": "ok"}

    saved = _agent.invoke_cli_text
    _agent.invoke_cli_text = fake_text  # type: ignore[assignment]
    try:
        yield captured
    finally:
        _agent.invoke_cli_text = saved  # type: ignore[assignment]


def test_restart_passes_yes_flag() -> None:
    # Arrange
    with _record_argv() as captured:
        # Act
        agent_restart("foo")
    # Assert
    assert captured[0] == ["agents", "restart", "foo", "--yes"]


def test_stop_does_not_force_yes() -> None:
    # Arrange
    with _record_argv() as captured:
        # Act
        agent_stop("foo")
    # Assert
    assert captured[0] == ["agents", "stop", "foo"]
