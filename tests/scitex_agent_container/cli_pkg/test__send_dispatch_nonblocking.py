"""Tests for ``scitex_agent_container.cli_pkg._send_dispatch_nonblocking``.

THE SCITEX-HPC REGRESSION (operator-reported, 2026-08-29). Three task
cards were routed to ``scitex-hpc``, an agent that had NEVER been
started (``status=defined``, zero tmux sessions). Every ``agent_send``
call reported ``status="dispatched"`` with a hardcoded
``delivered_subscriber_count: 1`` — a hardcoded literal, not a
measurement — because on the cross-host / in-container BROKERED path
(``resolve_send_endpoint_via_host``), neither loud-failure gate
(``pid_alive is False`` / ``port_reachable is False``) can ever fire:
``pid_alive`` is always ``None`` there by design, and ``port_reachable``
is ``None`` for any non-local target. See
:mod:`scitex_agent_container.cli_pkg._send_dispatch_nonblocking` for the
full account.

These tests reproduce that exact shape with a REAL
``scitex_agent_container.cli_pkg._send_broker.BrokeredPeer`` value (not a
mock — the same dataclass-like NamedTuple the host broker itself
constructs) standing in for "the host confirmed a port claim for a
cross-host agent, but nothing here could probe it". No state.db, no
PostgreSQL: ``diagnose_send_failure`` takes the ``brokered`` peer
directly and never touches a store when it is supplied, so these tests
EXECUTE on a host with no writable fleet database (contrast with
``test__send.py``'s ``pg_schema``-gated suite).

PA-306 / STX-NM002: no ``unittest.mock``, no ``monkeypatch``. Real values
throughout (a real ``BrokeredPeer``, a real bound TCP listener for the
"locally verified" cases). STX-TQ007 / STX-TQ002: one fact per test,
Arrange / Act / Assert markers.
"""

from __future__ import annotations

import socket
from contextlib import contextmanager
from typing import Iterator

from scitex_agent_container.cli_pkg._send_broker import BrokeredPeer
from scitex_agent_container.cli_pkg._send_dispatch_nonblocking import (
    dispatch_nonblocking,
)


@contextmanager
def _real_listener() -> Iterator[int]:
    """Bind a real TCP listener on a free loopback port; yield the port.

    Used only for the "locally verified" cases below, so the diagnosis
    sees a genuinely reachable port — no mocks.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        yield srv.getsockname()[1]
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# THE RED CASE — cross-host brokered claim, reachability never verified.
# This is the exact scitex-hpc shape: the host confirmed a port CLAIM for
# an agent on ANOTHER host, and this container has no way to probe it.
# ---------------------------------------------------------------------------


def test_cross_host_brokered_dispatch_does_not_fabricate_subscriber_count():
    # Arrange — a real BrokeredPeer: known, holds a port claim, lives on a
    # DIFFERENT host than the caller (so no local TCP probe can run).
    peer = BrokeredPeer(
        known=True, a2a_port=19005, host="scitex-compute-01", host_status=None, body={}
    )
    # Act
    result = dispatch_nonblocking(
        "scitex-hpc",
        "route this task card",
        a2a_port=19005,
        peer_host="scitex-compute-01",
        current_host="scitex-compute-04",
        url="http://scitex-compute-01:19005/v1/turn",
        metadata_extras={},
        brokered=peer,
    )
    # Assert — pre-fix this was a hardcoded ``1``; the runtime never
    # measured it. Honest is ``None`` (unverified), never a fabricated 1.
    assert result["delivered_subscriber_count"] is None


def test_cross_host_brokered_dispatch_still_reports_dispatched():
    # Arrange — same as above: backward compatibility means the STRING
    # status is unchanged; only the fabricated count and the new
    # status_code field change.
    peer = BrokeredPeer(
        known=True, a2a_port=19005, host="scitex-compute-01", host_status=None, body={}
    )
    # Act
    result = dispatch_nonblocking(
        "scitex-hpc",
        "route this task card",
        a2a_port=19005,
        peer_host="scitex-compute-01",
        current_host="scitex-compute-04",
        url="http://scitex-compute-01:19005/v1/turn",
        metadata_extras={},
        brokered=peer,
    )
    # Assert
    assert result["status"] == "dispatched"


def test_cross_host_brokered_dispatch_status_code_is_http_202():
    # Arrange
    peer = BrokeredPeer(
        known=True, a2a_port=19005, host="scitex-compute-01", host_status=None, body={}
    )
    # Act
    result = dispatch_nonblocking(
        "scitex-hpc",
        "route this task card",
        a2a_port=19005,
        peer_host="scitex-compute-01",
        current_host="scitex-compute-04",
        url="http://scitex-compute-01:19005/v1/turn",
        metadata_extras={},
        brokered=peer,
    )
    # Assert
    assert (result["status_code"]["kind"], result["status_code"]["code"]) == (
        "http",
        202,
    )


def test_cross_host_brokered_dispatch_message_states_unverified():
    # Arrange — the honesty fix: the message must say plainly that
    # reachability was not confirmed, not merely omit the confirmation.
    peer = BrokeredPeer(
        known=True, a2a_port=19005, host="scitex-compute-01", host_status=None, body={}
    )
    # Act
    result = dispatch_nonblocking(
        "scitex-hpc",
        "route this task card",
        a2a_port=19005,
        peer_host="scitex-compute-01",
        current_host="scitex-compute-04",
        url="http://scitex-compute-01:19005/v1/turn",
        metadata_extras={},
        brokered=peer,
    )
    # Assert
    assert "NOT verified" in result["status_code"]["message"]


def test_cross_host_brokered_dispatch_names_a_probe_in_the_message():
    # Arrange — M2: a non-final http 202 MUST name a runnable probe.
    peer = BrokeredPeer(
        known=True, a2a_port=19005, host="scitex-compute-01", host_status=None, body={}
    )
    # Act
    result = dispatch_nonblocking(
        "scitex-hpc",
        "route this task card",
        a2a_port=19005,
        peer_host="scitex-compute-01",
        current_host="scitex-compute-04",
        url="http://scitex-compute-01:19005/v1/turn",
        metadata_extras={},
        brokered=peer,
    )
    # Assert
    assert "`sac agents status scitex-hpc`" in result["status_code"]["message"]


# ---------------------------------------------------------------------------
# THE GREEN CONTRAST — a LOCALLY verified sidecar still reports a real 1.
# Proves the fix is a HONESTY fix, not a regression that always nulls the
# field: when a probe genuinely confirms the sidecar, the count is real.
# ---------------------------------------------------------------------------


def test_local_verified_dispatch_reports_subscriber_count_one():
    # Arrange — a real bound listener, and ``peer_host=""`` (local) so the
    # brokered diagnosis actually runs its TCP probe.
    with _real_listener() as port:
        peer = BrokeredPeer(known=True, a2a_port=port, host=None, host_status=None, body={})
        # Act
        result = dispatch_nonblocking(
            "alpha",
            "hi",
            a2a_port=port,
            peer_host="",
            current_host="lead-host",
            url=f"http://127.0.0.1:{port}/v1/turn",
            metadata_extras={},
            brokered=peer,
        )
    # Assert
    assert result["delivered_subscriber_count"] == 1


def test_local_verified_dispatch_status_code_message_says_confirmed():
    # Arrange
    with _real_listener() as port:
        peer = BrokeredPeer(known=True, a2a_port=port, host=None, host_status=None, body={})
        # Act
        result = dispatch_nonblocking(
            "alpha",
            "hi",
            a2a_port=port,
            peer_host="",
            current_host="lead-host",
            url=f"http://127.0.0.1:{port}/v1/turn",
            metadata_extras={},
            brokered=peer,
        )
    # Assert
    assert "confirmed" in result["status_code"]["message"]


def test_local_verified_dispatch_status_code_is_still_not_final():
    # Arrange — even a locally-confirmed listener has not proven the turn
    # was READ; 202 stays non-final either way.
    with _real_listener() as port:
        peer = BrokeredPeer(known=True, a2a_port=port, host=None, host_status=None, body={})
        # Act
        result = dispatch_nonblocking(
            "alpha",
            "hi",
            a2a_port=port,
            peer_host="",
            current_host="lead-host",
            url=f"http://127.0.0.1:{port}/v1/turn",
            metadata_extras={},
            brokered=peer,
        )
    # Assert
    from scitex_dev.status import StatusCode

    sc = StatusCode.from_dict(result["status_code"])
    assert sc.final is False


# EOF
