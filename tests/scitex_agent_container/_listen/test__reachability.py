"""REGISTERED is not REACHABLE — the inbox-subscriber observation.

``GET /agents`` (which backs the ``a2a_peers`` MCP tool) used to report only
what the registry DECLARED: a pid, a port, a start time, a group. An agent
whose inbox adapter is not attached to the channel bus satisfies every one of
those and still swallows every message sent to it. Two of four live agents
were in exactly that state while advertising themselves as ``active``.

These tests pin the distinction, and — just as importantly — pin the THIRD
state. A remote agent's broker lives in another process that this listen
cannot see; reporting it as ``unreachable`` would be a false accusation
against a healthy peer, and the remedy a caller reaches for on a false death
verdict is destructive. So "cannot observe" must come back as ``unknown``.

Real ``Broker`` throughout — no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._listen._reachability import (
    REACHABLE,
    UNKNOWN,
    UNREACHABLE,
    annotate_reachability,
    annotate_rows,
)
from scitex_agent_container.a2a._inbox_bus import Broker

LOCAL = "ywata-note-win"


# ---------------------------------------------------------------------------
# Broker.subscriber_counts — the only OBSERVATION of reachability there is.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broker_reports_zero_for_an_agent_with_no_subscriber():
    """The deaf case: nobody ever subscribed, so the agent is simply absent
    from the map. Callers must read "absent" as zero."""
    # Arrange
    broker = Broker()
    # Act
    counts = await broker.subscriber_counts()
    # Assert
    assert counts.get("deaf-agent", 0) == 0


@pytest.mark.asyncio
async def test_broker_counts_a_live_subscriber():
    # Arrange
    broker = Broker()
    await broker.subscribe("alice")
    # Act
    counts = await broker.subscriber_counts()
    # Assert
    assert counts["alice"] == 1


@pytest.mark.asyncio
async def test_broker_counts_drop_when_a_subscriber_leaves():
    """An agent whose adapter disconnects becomes deaf again — the count must
    follow reality, not the registry."""
    # Arrange
    broker = Broker()
    queue = await broker.subscribe("alice")
    await broker.unsubscribe("alice", queue)
    # Act
    counts = await broker.subscriber_counts()
    # Assert
    assert counts.get("alice", 0) == 0


@pytest.mark.asyncio
async def test_broker_snapshot_matches_publish_fanout():
    """The count must be the SAME number ``publish`` fans out to — otherwise
    a2a_peers would be advertising a reachability the bus does not honour."""
    # Arrange
    broker = Broker()
    await broker.subscribe("alice")
    await broker.subscribe("alice")
    # Act
    counts = await broker.subscriber_counts()
    delivered = await broker.publish("alice", {"msg_id": "m1"})
    # Assert
    assert counts["alice"] == delivered


# ---------------------------------------------------------------------------
# annotate_reachability — the three states.
# ---------------------------------------------------------------------------


def test_agent_with_a_subscriber_is_reachable():
    # Arrange
    row = {"name": "scitex-todo", "host": LOCAL, "pid": 123}
    # Act
    out = annotate_reachability(
        row, subscriber_counts={"scitex-todo": 1}, local_host=LOCAL
    )
    # Assert
    assert out["inbox_reachable"] == REACHABLE


def test_agent_with_no_subscriber_is_unreachable_not_active():
    """THE BUG. A registered, running, ``active`` agent with zero subscribers
    is NOT reachable, and must not be presented as though it were."""
    # Arrange — every declaration says healthy; the bus says nobody is home.
    row = {
        "name": "claude-code-telegrammer",
        "host": LOCAL,
        "pid": 4242,
        "groups": ["active"],
    }
    # Act
    out = annotate_reachability(row, subscriber_counts={}, local_host=LOCAL)
    # Assert
    assert out["inbox_reachable"] == UNREACHABLE


def test_unreachable_agent_reports_zero_subscribers():
    # Arrange
    row = {"name": "scitex-dev", "host": LOCAL, "pid": 7, "groups": ["active"]}
    # Act
    out = annotate_reachability(row, subscriber_counts={}, local_host=LOCAL)
    # Assert
    assert out["inbox_subscribers"] == 0


def test_remote_agent_is_unknown_not_unreachable():
    """A peer on ANOTHER host has its subscribers in another process's broker.
    We cannot see it, so we must not claim it is deaf — that would be a false
    accusation, and false death verdicts get healthy agents destroyed."""
    # Arrange
    row = {"name": "remote-agent", "host": "spartan", "pid": 9}
    # Act
    out = annotate_reachability(row, subscriber_counts={}, local_host=LOCAL)
    # Assert
    assert out["inbox_reachable"] == UNKNOWN


def test_remote_agent_reports_null_subscribers_not_zero():
    """A count we could not take is ``None``, never ``0``. Zero is a claim."""
    # Arrange
    row = {"name": "remote-agent", "host": "spartan"}
    # Act
    out = annotate_reachability(row, subscriber_counts={}, local_host=LOCAL)
    # Assert
    assert out["inbox_subscribers"] is None


def test_row_without_a_host_is_observed_locally():
    """The publish path treats a host-less name as local (see
    ``state_db_nodes.is_local_node``), so the local broker IS authoritative
    for it and a zero there is a real observation."""
    # Arrange
    row = {"name": "hostless", "pid": 1}
    # Act
    out = annotate_reachability(row, subscriber_counts={}, local_host=LOCAL)
    # Assert
    assert out["inbox_reachable"] == UNREACHABLE


def test_unknown_local_identity_makes_a_hosted_row_unknown():
    """If we don't know which host WE are, we cannot prove a hosted row is
    ours — so we cannot claim its broker is the one we just read."""
    # Arrange
    row = {"name": "somebody", "host": "elsewhere"}
    # Act
    out = annotate_reachability(row, subscriber_counts={}, local_host=None)
    # Assert
    assert out["inbox_reachable"] == UNKNOWN


def test_annotation_preserves_the_registry_declaration():
    """We ADD the observation next to the declaration. We never overwrite or
    reinterpret what the registry said — both facts must stay readable."""
    # Arrange
    row = {"name": "a", "host": LOCAL, "pid": 5, "groups": ["active"], "role": "dev"}
    # Act
    out = annotate_reachability(row, subscriber_counts={}, local_host=LOCAL)
    # Assert
    assert (out["pid"], out["groups"], out["role"]) == (5, ["active"], "dev")


def test_annotate_rows_separates_reachable_from_deaf_peers():
    """The scorecard that started this: two agents delivered, two swallowed —
    and the peer list called all four ``active``."""
    # Arrange
    rows = [
        {"name": "scitex-todo", "host": LOCAL, "groups": ["active"]},
        {"name": "scitex-agent-container", "host": LOCAL, "groups": ["active"]},
        {"name": "claude-code-telegrammer", "host": LOCAL, "groups": ["active"]},
        {"name": "scitex-dev", "host": LOCAL, "groups": ["active"]},
    ]
    counts = {"scitex-todo": 1, "scitex-agent-container": 1}
    # Act
    out = annotate_rows(rows, subscriber_counts=counts, local_host=LOCAL)
    deaf = [r["name"] for r in out if r["inbox_reachable"] == UNREACHABLE]
    # Assert
    assert deaf == ["claude-code-telegrammer", "scitex-dev"]
