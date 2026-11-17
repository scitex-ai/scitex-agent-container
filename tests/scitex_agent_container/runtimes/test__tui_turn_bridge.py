"""Tests for the TUI A2A turn bridge (``runtimes._tui_turn_bridge``).

The bridge gives a ``runtime: tui`` agent the same ``/v1/turn`` endpoint
the SDK runner serves, so a bus-pushed message (the ``sac mcp channel``
subscriber's wake POST) DRIVES a turn in the idle TUI instead of timing
out on a dead port.

Real seams only (PA-306 no-mocks): the HTTP routes run against a REAL
``ThreadingHTTPServer`` on an ephemeral port, exercised by a REAL urllib
client; a recording ``on_turn`` callable stands in for the tmux inject.
The launcher is driven through a real ``spawn`` recorder (a plain
function), not a mock.

STX-TQ002 AAA markers (each on its own line) + STX-TQ007 one observable
assert + STX-TQ003 descriptive names.
"""

from __future__ import annotations

import errno
import socket
from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes import _tui_turn_bridge as bridge
from tests.scitex_agent_container.runtimes._tui_turn_bridge_helpers import (
    _PORT,
    _PORT_RESERVATION,
)


@pytest.mark.skipif(
    _PORT_RESERVATION is None,
    reason=(
        "this platform does not permit a hold that is transparent to "
        "port_is_free, so the module fell back to the draw-and-release form"
    ),
)
def test_the_reserved_port_still_reads_free_to_the_bridges_own_probe() -> None:
    """The reservation must not break the gate it exists to stabilise."""
    # Arrange
    host = "127.0.0.1"
    # Act
    observed = bridge.port_is_free(host, _PORT)
    # Assert
    assert observed is True, (
        "the session reservation made the module port look BUSY to the "
        "bridge's own probe, which would fail every start_turn_bridge test "
        "deterministically instead of rarely"
    )


@pytest.mark.skipif(
    _PORT_RESERVATION is None,
    reason=(
        "this platform does not permit a hold that is transparent to "
        "port_is_free, so the module fell back to the draw-and-release form"
    ),
)
def test_the_module_port_is_reserved_not_merely_observed_free() -> None:
    """The kernel must genuinely HOLD it, which is what excludes it.

    A plain socket -- no SO_REUSEADDR -- is the honest question "is this port
    occupied?". An occupied port is excluded from the ephemeral autobind pool,
    and that exclusion is the only thing stopping a co-tenant process being
    handed this port minutes after the module was imported.

    FAILS BEFORE THE FIX: the old helper closed its socket, so the port was
    genuinely unoccupied and this plain bind SUCCEEDED.
    """
    # Arrange
    plain = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Act
    try:
        plain.bind(("127.0.0.1", _PORT))
        observed = None
    except OSError as exc:
        observed = exc.errno
    finally:
        plain.close()
    # Assert
    assert observed == errno.EADDRINUSE, (
        "the module port is not actually held, so the kernel may re-issue it "
        "to another process mid-session -- the exact race this reservation "
        f"exists to remove (plain bind returned {observed!r})"
    )


# ---------------------------------------------------------------------------
# is_turn_route
# ---------------------------------------------------------------------------
def test_is_turn_route_accepts_bare_v1_turn() -> None:
    # Arrange
    path = "/v1/turn"
    # Act
    accepted = bridge.is_turn_route(path, "figrecipe")
    # Assert
    assert accepted is True


def test_is_turn_route_accepts_named_turn_for_this_agent() -> None:
    # Arrange
    path = "/agents/figrecipe/turn"
    # Act
    accepted = bridge.is_turn_route(path, "figrecipe")
    # Assert
    assert accepted is True


def test_is_turn_route_accepts_the_a2a_message_send_verb() -> None:
    """The spelling every a2a caller in this package actually posts.

    Regression for a live cross-host outage (2026-09-02): peer sends to
    `figrecipe` on compute-03 all died with ``no turn route
    '/agents/figrecipe/message:send'`` while the agent was healthy. A peer
    that resolves the target to the host's listen port gets ``sac listen``,
    which serves this verb; one that resolves to the agent's own a2a port
    gets this bridge, which did not. The caller does not choose which, so
    both must answer it.
    """
    # Arrange
    path = "/agents/figrecipe/message:send"
    # Act
    accepted = bridge.is_turn_route(path, "figrecipe")
    # Assert
    assert accepted is True


def test_is_turn_route_rejects_message_send_for_another_agent() -> None:
    """The misroute guard must survive the new alias.

    Widening the accepted set is exactly where a cross-agent leak gets
    introduced: a POST naming a DIFFERENT agent must still 404 rather than
    land in this session.
    """
    # Arrange
    path = "/agents/someone-else/message:send"
    # Act
    accepted = bridge.is_turn_route(path, "figrecipe")
    # Assert
    assert accepted is False


def test_is_turn_route_rejects_named_route_for_other_agent() -> None:
    # Arrange
    path = "/agents/someone-else/turn"
    # Act
    accepted = bridge.is_turn_route(path, "figrecipe")
    # Assert
    assert accepted is False


# ---------------------------------------------------------------------------
# resolved_a2a_port
# ---------------------------------------------------------------------------
def test_resolved_a2a_port_returns_int_when_resolved() -> None:
    # Arrange
    config = SimpleNamespace(a2a=SimpleNamespace(port=_PORT))
    # Act
    port = bridge.resolved_a2a_port(config)
    # Assert
    assert port == _PORT


def test_resolved_a2a_port_none_when_port_is_auto_string() -> None:
    # Arrange
    config = SimpleNamespace(a2a=SimpleNamespace(port="auto"))
    # Act
    port = bridge.resolved_a2a_port(config)
    # Assert
    assert port is None
