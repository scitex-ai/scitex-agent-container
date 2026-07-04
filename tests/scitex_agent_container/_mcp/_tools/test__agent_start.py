"""Tests for the MCP ``agent_start`` tool wrapper.

The CLI ``sac agents start <name>`` refuses to run against a
non-running agent without ``--yes/-y`` (exit 2, "refusing to start
... without --yes/-y"). An MCP tool call has no TTY to confirm on, so
the wrapper must pass ``--yes`` itself — otherwise the documented MCP
surface dead-ends on a guard that can never be satisfied. This guards
that regression (the same class of bug already fixed on the sibling
``agent_restart`` tool; ``agent_start`` was simply missed).

PA-306 / STX-NM002: no ``unittest.mock``, no ``monkeypatch``. The
``invoke_cli_text`` collaborator is swapped on the ``_agent`` module
namespace via a real save/restore context manager — mirrors
``test__agent_restart.py``.

STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from scitex_agent_container._mcp._tools import _agent
from scitex_agent_container._mcp._tools._agent import agent_start


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


def test_start_passes_yes_flag() -> None:
    # Arrange
    with _record_argv() as captured:
        # Act
        agent_start("foo")
    # Assert
    assert captured[0] == ["agents", "start", "foo", "--yes"]


def test_start_foreground_appends_foreground_flag_after_yes() -> None:
    # Arrange
    with _record_argv() as captured:
        # Act
        agent_start("foo", foreground=True)
    # Assert
    assert captured[0] == ["agents", "start", "foo", "--yes", "--foreground"]


def test_start_session_continue_appends_continue_flag_after_yes() -> None:
    # Arrange
    with _record_argv() as captured:
        # Act
        agent_start("foo", session="continue")
    # Assert
    assert captured[0] == ["agents", "start", "foo", "--yes", "--continue"]


def test_start_session_fresh_appends_fresh_flag_after_yes() -> None:
    # Arrange
    with _record_argv() as captured:
        # Act
        agent_start("foo", session="fresh")
    # Assert
    assert captured[0] == ["agents", "start", "foo", "--yes", "--fresh"]


def test_start_session_resume_appends_session_flag_after_yes() -> None:
    # Arrange
    with _record_argv() as captured:
        # Act
        agent_start("foo", session="resume")
    # Assert
    assert captured[0] == ["agents", "start", "foo", "--yes", "--session", "resume"]


def test_start_session_new_session_alias_maps_to_session_resume_after_yes() -> None:
    # Arrange
    with _record_argv() as captured:
        # Act
        agent_start("foo", session="new-session")
    # Assert
    assert captured[0] == [
        "agents",
        "start",
        "foo",
        "--yes",
        "--session",
        "new-session",
    ]


def test_start_invalid_session_returns_error_without_dispatch() -> None:
    # Arrange
    with _record_argv() as captured:
        # Act
        result = agent_start("foo", session="bogus")
    # Assert
    assert result["status"] == "error" and captured == []
