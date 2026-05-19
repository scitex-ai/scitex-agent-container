"""WI-4 — name → host resolver primitive (handoff §4).

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-4 "Cross-host routing"):

  Required: A name→host→URL resolver; reuse sac's peer/fleet host
  registry and the ``peer.py post_turn`` cross-host pattern.

This module exercises the *resolver primitive*. The HTTP forwarding
layer that uses this primitive is gated on cross-host bearer
discovery (see QUESTIONS Q4) and lands as a separate change.

No mocks (handoff §0): real SQLite under ``tmp_path``, real
``record_instance_start`` writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    is_local_node,
    resolve_node_host,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # Arrange
    p = tmp_path / "state.db"
    state_db.init_schema(p)
    return p


# ---------------------------------------------------------------------------
# resolve_node_host — name → (host, a2a_port) lookup
# ---------------------------------------------------------------------------


def test_resolve_node_host_returns_none_for_unknown_name(db_path: Path) -> None:
    # Arrange
    name = "no-such-node"
    # Act
    result = resolve_node_host(name=name, db_path=db_path)
    # Assert
    assert result is None


def test_resolve_node_host_returns_host_for_recorded_instance(db_path: Path) -> None:
    """A sac-managed agent recorded via ``record_instance_start`` is
    resolvable by name."""
    # Arrange
    state_db.record_instance_start(
        name="alice",
        host="host-a",
        a2a_port=8801,
        db_path=db_path,
    )
    # Act
    result = resolve_node_host(name="alice", db_path=db_path)
    # Assert
    assert result is not None and result["host"] == "host-a"


def test_resolve_node_host_returns_a2a_port_when_set(db_path: Path) -> None:
    # Arrange
    state_db.record_instance_start(
        name="alice",
        host="host-a",
        a2a_port=8801,
        db_path=db_path,
    )
    # Act
    result = resolve_node_host(name="alice", db_path=db_path)
    # Assert
    assert result is not None and result["a2a_port"] == 8801


def test_resolve_node_host_skips_ended_instances(db_path: Path) -> None:
    """An ended instance must not satisfy a live-routing lookup."""
    # Arrange
    instance_id = state_db.record_instance_start(
        name="alice",
        host="host-a",
        a2a_port=8801,
        db_path=db_path,
    )
    state_db.record_instance_stop(instance_id, db_path=db_path)
    # Act
    result = resolve_node_host(name="alice", db_path=db_path)
    # Assert
    assert result is None


def test_resolve_node_host_returns_latest_when_multiple_live(db_path: Path) -> None:
    """Two live records for the same name (e.g., a restart race) —
    return the most recent. Determinism matters: cross-host forward
    cannot pick non-deterministically.
    """
    # Arrange
    state_db.record_instance_start(
        name="alice", host="host-a", a2a_port=8801, db_path=db_path
    )
    state_db.record_instance_start(
        name="alice", host="host-b", a2a_port=8802, db_path=db_path
    )
    # Act
    result = resolve_node_host(name="alice", db_path=db_path)
    # Assert — most-recent wins
    assert result is not None and result["host"] == "host-b"


# ---------------------------------------------------------------------------
# is_local_node — local-vs-remote decision for the forwarder
# ---------------------------------------------------------------------------


def test_is_local_node_true_when_host_matches_local(db_path: Path) -> None:
    # Arrange
    state_db.record_instance_start(
        name="alice", host="host-a", a2a_port=8801, db_path=db_path
    )
    # Act
    local = is_local_node(name="alice", local_host="host-a", db_path=db_path)
    # Assert
    assert local is True


def test_is_local_node_false_when_host_differs(db_path: Path) -> None:
    # Arrange
    state_db.record_instance_start(
        name="alice", host="host-a", a2a_port=8801, db_path=db_path
    )
    # Act
    local = is_local_node(name="alice", local_host="host-b", db_path=db_path)
    # Assert
    assert local is False


def test_is_local_node_true_for_unknown_name(db_path: Path) -> None:
    """An unknown name (no instance recorded — e.g., an external node
    we haven't seen before) is treated as local. The local
    ``NodeRegistry`` handles implicit registration; deferring the
    decision to the cross-host forwarder for a node sac doesn't know
    about would just synthesise an SSRF target.
    """
    # Arrange
    # (no instance recorded)
    # Act
    local = is_local_node(name="ghost", local_host="host-a", db_path=db_path)
    # Assert
    assert local is True
