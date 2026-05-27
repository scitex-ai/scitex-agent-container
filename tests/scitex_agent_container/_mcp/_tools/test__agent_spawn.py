"""Tests for the MCP ``agent_spawn`` tool wrapper.

The wrapper delegates to
:func:`scitex_agent_container._lifecycle._spawn_client.request_spawn`
and re-shapes the result/error into a status-tagged dict suitable for
an MCP host. We verify both the registration shim and the wrap/error
shaping.

PA-306 / STX-NM002: no ``unittest.mock``, no ``monkeypatch``. The
``request_spawn`` collaborator is swapped on the ``_spawn_client``
module namespace via a real save/restore context manager — mirrors
the ``test__agent_send.py`` pattern.

STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import scitex_agent_container._lifecycle._spawn_client as _spawn_mod
from scitex_agent_container._lifecycle._spawn_client import SpawnRequestError
from scitex_agent_container._mcp._tools import register_all_tools
from scitex_agent_container._mcp._tools._agent import (
    agent_spawn,
    register_agent_tools,
)


@contextmanager
def _swap_request_spawn(*, raises: Exception | None = None, returns: dict | None = None) -> Iterator[list]:
    """Swap ``_spawn_client.request_spawn`` with a recording fake.

    Yields the capture list. ``raises`` simulates a SpawnRequestError;
    ``returns`` provides the success payload. Exactly one must be
    given.
    """
    captured: list = []

    def fake(*args, **kwargs):
        captured.append({"args": args, "kwargs": kwargs})
        if raises is not None:
            raise raises
        return returns if returns is not None else {"name": args[0], "returncode": 0}

    saved = _spawn_mod.request_spawn
    _spawn_mod.request_spawn = fake  # type: ignore[assignment]
    try:
        yield captured
    finally:
        _spawn_mod.request_spawn = saved  # type: ignore[assignment]


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
# Registration
# ---------------------------------------------------------------------------


def test_agent_spawn_registered_in_mcp_tool_list():
    # Arrange
    mcp = _FakeMCP()
    # Act
    register_all_tools(mcp)
    # Assert
    assert "agent_spawn" in {fn.__name__ for fn in mcp.registered}


def test_register_agent_tools_includes_agent_spawn():
    # Arrange
    mcp = _FakeMCP()
    # Act
    register_agent_tools(mcp)
    # Assert
    assert "agent_spawn" in {fn.__name__ for fn in mcp.registered}


# ---------------------------------------------------------------------------
# Happy path — wraps result with status=ok
# ---------------------------------------------------------------------------


def test_agent_spawn_returns_status_ok_on_success():
    # Arrange
    with _swap_request_spawn(returns={"name": "c", "returncode": 0}):
        # Act
        out = agent_spawn("c")
    # Assert
    assert out["status"] == "ok"


def test_agent_spawn_wraps_server_body_under_result_key():
    # Arrange
    body = {"name": "c", "returncode": 0, "stdout": "ok", "stderr": ""}
    with _swap_request_spawn(returns=body):
        # Act
        out = agent_spawn("c")
    # Assert
    assert out["result"] == body


def test_agent_spawn_forwards_name_to_request_spawn():
    # Arrange
    with _swap_request_spawn() as captured:
        # Act
        agent_spawn("alpha")
    # Assert
    assert captured[-1]["args"][0] == "alpha"


def test_agent_spawn_forwards_inline_spec_kwarg():
    # Arrange
    spec = {"apiVersion": "scitex-agent-container/v3", "kind": "Agent", "spec": {}}
    with _swap_request_spawn() as captured:
        # Act
        agent_spawn("alpha", spec=spec)
    # Assert
    assert captured[-1]["kwargs"]["spec"] == spec


def test_agent_spawn_forwards_overwrite_kwarg():
    # Arrange
    with _swap_request_spawn() as captured:
        # Act
        agent_spawn("alpha", spec={"a": 1}, overwrite=True)
    # Assert
    assert captured[-1]["kwargs"]["overwrite"] is True


def test_agent_spawn_forwards_explicit_caller_kwarg():
    # Arrange
    with _swap_request_spawn() as captured:
        # Act
        agent_spawn("alpha", caller="parent-bot")
    # Assert
    assert captured[-1]["kwargs"]["caller"] == "parent-bot"


# ---------------------------------------------------------------------------
# Error paths — fail loud, structured
# ---------------------------------------------------------------------------


def test_agent_spawn_returns_status_error_on_spawn_request_error():
    # Arrange — a deny from the server-side ACL gate.
    err = SpawnRequestError(
        "spawn of 'c' rejected: listen returned HTTP 403",
        status=403,
        body={"error": "ACL deny", "reason": "child caller"},
    )
    with _swap_request_spawn(raises=err):
        # Act
        out = agent_spawn("c")
    # Assert
    assert out["status"] == "error"


def test_agent_spawn_error_includes_http_status_code():
    # Arrange
    err = SpawnRequestError("nope", status=403, body={"reason": "x"})
    with _swap_request_spawn(raises=err):
        # Act
        out = agent_spawn("c")
    # Assert
    assert out["http_status"] == 403


def test_agent_spawn_error_includes_server_body_verbatim():
    # Arrange
    err_body = {"error": "ACL deny", "reason": "child caller may not spawn"}
    err = SpawnRequestError("denied", status=403, body=err_body)
    with _swap_request_spawn(raises=err):
        # Act
        out = agent_spawn("c")
    # Assert — server's reason survives so the MCP host can render it.
    assert out["body"] == err_body


def test_agent_spawn_transport_error_returns_null_http_status():
    # Arrange — transport-layer failure (listen unreachable).
    err = SpawnRequestError(
        "spawn of 'c' failed: cannot reach listen at 'http://h:9100'",
        status=None,
        body=None,
    )
    with _swap_request_spawn(raises=err):
        # Act
        out = agent_spawn("c")
    # Assert
    assert out["http_status"] is None


def test_agent_spawn_error_includes_reason_message():
    # Arrange
    err = SpawnRequestError("very specific failure text", status=500, body=None)
    with _swap_request_spawn(raises=err):
        # Act
        out = agent_spawn("c")
    # Assert
    assert out["reason"] == "very specific failure text"
