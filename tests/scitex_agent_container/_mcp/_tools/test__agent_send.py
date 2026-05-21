"""Tests for the MCP ``agent_send`` tool wrapper in
``scitex_agent_container._mcp._tools._agent``.

Verifies:

* the tool is registered alongside the rest of the ``agent_*`` group
  by :func:`register_agent_tools` / :func:`register_all_tools`,
* the wrapper delegates to
  :func:`scitex_agent_container.cli_pkg._send.send_to_agent` verbatim
  (returns the helper's dict, no re-shape).

PA-306 / STX-NM002: no ``unittest.mock``, no ``monkeypatch``. The
``send_to_agent`` collaborator is swapped on the ``_send`` module
namespace via a real save/restore context manager.

STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import scitex_agent_container.cli_pkg._send as _send_mod
from scitex_agent_container._mcp._tools import register_all_tools
from scitex_agent_container._mcp._tools._agent import (
    agent_send,
    register_agent_tools,
)


@contextmanager
def _swap_send_to_agent() -> Iterator[list]:
    """Swap ``cli_pkg._send.send_to_agent`` with a recording fake.

    Yields the capture list so tests can assert on the arguments
    the MCP wrapper forwarded. Restores the original on exit.
    """
    captured: list = []

    def fake(*args, **kwargs):
        captured.append({"args": args, "kwargs": kwargs})
        return {"status": "ok", "response_text": "fake", "response_metadata": {}}

    saved = _send_mod.send_to_agent
    _send_mod.send_to_agent = fake  # type: ignore[assignment]
    try:
        yield captured
    finally:
        _send_mod.send_to_agent = saved  # type: ignore[assignment]


class _FakeMCP:
    """Records every fn registered via ``@mcp.tool()``."""

    def __init__(self) -> None:
        self.registered: list = []

    def tool(self, *args, **kw):
        def decorator(fn):
            self.registered.append(fn)
            return fn

        return decorator


# ---------------------------------------------------------------------------
# Spec test 8: agent_send is registered in the MCP tool list
# ---------------------------------------------------------------------------


def test_agent_send_registered_in_mcp_tool_list():
    # Arrange
    mcp = _FakeMCP()
    # Act
    register_all_tools(mcp)
    # Assert
    assert "agent_send" in {fn.__name__ for fn in mcp.registered}


def test_register_agent_tools_includes_agent_send():
    # Arrange
    mcp = _FakeMCP()
    # Act
    register_agent_tools(mcp)
    # Assert
    assert "agent_send" in {fn.__name__ for fn in mcp.registered}


# ---------------------------------------------------------------------------
# Wrapper delegates to send_to_agent with correct kwargs
# ---------------------------------------------------------------------------


def test_agent_send_forwards_prompt_to_send_to_agent():
    # Arrange
    with _swap_send_to_agent() as captured:
        # Act
        agent_send("alpha", prompt="hello there")
        last = captured[-1]
    # Assert
    assert last["kwargs"]["prompt"] == "hello there"


def test_agent_send_forwards_name_to_send_to_agent():
    # Arrange
    with _swap_send_to_agent() as captured:
        # Act
        agent_send("zeta", prompt="hi")
        last = captured[-1]
    # Assert
    assert last["args"][0] == "zeta"


def test_agent_send_forwards_timeout_seconds_kwarg():
    # Arrange
    with _swap_send_to_agent() as captured:
        # Act
        agent_send("alpha", prompt="hi", timeout_seconds=45)
        last = captured[-1]
    # Assert
    assert last["kwargs"]["timeout_seconds"] == 45


def test_agent_send_forwards_model_kwarg():
    # Arrange
    with _swap_send_to_agent() as captured:
        # Act
        agent_send("alpha", prompt="hi", model="opus")
        last = captured[-1]
    # Assert
    assert last["kwargs"]["model"] == "opus"


def test_agent_send_forwards_max_turns_kwarg():
    # Arrange
    with _swap_send_to_agent() as captured:
        # Act
        agent_send("alpha", prompt="hi", max_turns=3)
        last = captured[-1]
    # Assert
    assert last["kwargs"]["max_turns"] == 3


def test_agent_send_defaults_wait_to_false_nonblocking():
    # Arrange
    with _swap_send_to_agent() as captured:
        # Act
        agent_send("alpha", prompt="hi")
        last = captured[-1]
    # Assert — default dispatch is non-blocking.
    assert last["kwargs"]["wait"] is False


def test_agent_send_forwards_explicit_wait_kwarg():
    # Arrange
    with _swap_send_to_agent() as captured:
        # Act
        agent_send("alpha", prompt="hi", wait=True)
        last = captured[-1]
    # Assert
    assert last["kwargs"]["wait"] is True


def test_agent_send_returns_send_to_agent_payload_verbatim():
    # Arrange
    with _swap_send_to_agent():
        # Act
        result = agent_send("alpha", prompt="hi")
    # Assert
    assert result == {
        "status": "ok",
        "response_text": "fake",
        "response_metadata": {},
    }
