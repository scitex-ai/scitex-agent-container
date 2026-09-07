"""The auto-port scan must skip a port something is actually LISTENING on.

MEASURED 2026-09-07 by scitex-hub on scitex-compute-03, and it is the reason
this probe exists:

  * handyman-c03-01 started; its turn bridge bound 127.0.0.1:19003.
  * `sac agents stop handyman-c03-01` released the CLAIM and left the bridge,
    the claude process, the mcp channel and the tmux session all running.
  * figrecipe-qwen started with `a2a.port: auto`, was handed 19003 because the
    claim table said free, and its claude launched with
    --turn-url http://127.0.0.1:19003/v1/turn.
  * A turn sent to figrecipe-qwen appeared VERBATIM in handyman-c03-01's pane —
    different repo, different identity, different git credentials — and the
    sender received {"ok": true, "http_status": 200}.

A MISDELIVERED turn reported as delivered is the inverse of the failure the
turn bridge's 502 exists to prevent, and worse: a task that edited files would
have edited the wrong tree, authored as the wrong agent.

The claim table and the socket had diverged, and only the table was consulted.
These tests pin the socket as the arbiter.

REAL SOCKETS, NO MOCKS — matching port_allocator's own stated contract. A
mocked "port is busy" would prove the test's own bookkeeping, not the bind.
"""

from __future__ import annotations

import socket

from scitex_agent_container._state.port_allocator import port_is_bindable


def _free_port() -> int:
    """A port the OS just confirmed is free, released before returning."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_port_held_by_a_live_listener_is_NOT_bindable() -> None:
    # Arrange — a real listener, exactly like a stopped agent's stray bridge.
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = int(held.getsockname()[1])

    # Act
    try:
        verdict = port_is_bindable(port)
    finally:
        held.close()

    # Assert
    assert verdict is False, port


def test_CONTROL_a_free_port_IS_bindable() -> None:
    # Arrange — without this the test above passes for a probe that always
    # says False, which would block every allocation instead of one port.
    port = _free_port()

    # Act
    verdict = port_is_bindable(port)

    # Assert
    assert verdict is True, port


def test_the_probe_reflects_RELEASE_rather_than_caching_a_verdict() -> None:
    # Arrange — hold, then release. A probe that memoised would still say busy,
    # and would strand a port permanently after one stray listener.
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = int(held.getsockname()[1])
    port_is_bindable(port)
    held.close()

    # Act
    after_release = port_is_bindable(port)

    # Assert
    assert after_release is True, port


def test_the_probe_asks_about_BINDING_not_about_being_served() -> None:
    # Arrange — a bound socket that never calls listen(). A connect-based probe
    # would call this port free; a bind-based one must not, because the bridge
    # binds and would collide.
    bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    bound.bind(("127.0.0.1", 0))
    port = int(bound.getsockname()[1])

    # Act
    try:
        verdict = port_is_bindable(port)
    finally:
        bound.close()

    # Assert
    assert verdict is False, port
