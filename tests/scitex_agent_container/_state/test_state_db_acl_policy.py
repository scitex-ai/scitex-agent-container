"""Phase-3 (ADR-0010 Step 2) — per-spec ACL policy persistence + the
state-DB primitives that surface it to the listen-server's
``check_send_acl``.

Tests cover:

* ``record_comms_policy`` upserts and is idempotent on conflict.
* ``read_comms_policy`` returns the persisted row or the legacy default.
* ``sender_target_relationship`` classifies parent / child / sibling /
  other from the ``lineage`` table.
* ``derive_group`` honours ``lineage_group='solitary'`` (Gap-4) by
  returning the singleton group regardless of lineage edges.

AAA, one assertion per test, no mocks (real SQLite).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    derive_group,
    read_comms_policy,
    record_comms_policy,
    record_lineage,
    sender_target_relationship,
)


@pytest.fixture
def db_path(tmp_path: Path):
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = state_db.DEFAULT_DB_PATH
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    state_db.init_schema(db)
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env


# ---------------------------------------------------------------------------
# record_comms_policy / read_comms_policy
# ---------------------------------------------------------------------------


def test_read_comms_policy_returns_defaults_when_row_absent(
    db_path: Path,
) -> None:
    """No row → byte-equivalent to pre-Phase-3 (everything allow)."""
    # Arrange — no record_comms_policy call.
    # Act
    policy = read_comms_policy(name="never-recorded", db_path=db_path)
    # Assert
    assert policy["outbound_siblings"] == "allow"


def test_record_comms_policy_persists_outbound_siblings_deny(
    db_path: Path,
) -> None:
    """Round-trip through state.db preserves the deny flag."""
    # Arrange
    record_comms_policy(name="cap-a", outbound_siblings="deny", db_path=db_path)
    # Act
    policy = read_comms_policy(name="cap-a", db_path=db_path)
    # Assert
    assert policy["outbound_siblings"] == "deny"


def test_record_comms_policy_upsert_refreshes_on_restart(db_path: Path) -> None:
    """A spec edit at restart re-publishes the row in place (no manual
    state.db surgery)."""
    # Arrange
    record_comms_policy(name="cap-a", inbound_parent="deny", db_path=db_path)
    record_comms_policy(name="cap-a", inbound_parent="allow", db_path=db_path)
    # Act
    policy = read_comms_policy(name="cap-a", db_path=db_path)
    # Assert
    assert policy["inbound_parent"] == "allow"


def test_record_comms_policy_rejects_unknown_outbound_value(db_path: Path) -> None:
    """Out-of-domain values are rejected (defence-in-depth — parser also
    rejects them at YAML-load time)."""
    # Arrange — about to call with a bad value.
    # Act / Assert
    with pytest.raises(ValueError):
        record_comms_policy(
            name="cap-a", outbound_siblings="maybe", db_path=db_path
        )


# ---------------------------------------------------------------------------
# sender_target_relationship
# ---------------------------------------------------------------------------


def test_sender_target_relationship_sibling(db_path: Path) -> None:
    """Two children of the same parent classify as siblings."""
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    record_lineage(child="cap-b", parent="root", db_path=db_path)
    # Act
    rel = sender_target_relationship(
        sender="cap-a", target="cap-b", db_path=db_path
    )
    # Assert
    assert rel == "sibling"


def test_sender_target_relationship_parent(db_path: Path) -> None:
    """Sender → its parent classifies as 'parent'."""
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    # Act
    rel = sender_target_relationship(
        sender="cap-a", target="root", db_path=db_path
    )
    # Assert
    assert rel == "parent"


def test_sender_target_relationship_child(db_path: Path) -> None:
    """Sender → its direct child classifies as 'child'."""
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    # Act
    rel = sender_target_relationship(
        sender="root", target="cap-a", db_path=db_path
    )
    # Assert
    assert rel == "child"


def test_sender_target_relationship_other(db_path: Path) -> None:
    """Unrelated nodes (no shared parent, no direct edge) → 'other'."""
    # Arrange — no lineage edges.
    # Act
    rel = sender_target_relationship(
        sender="cap-a", target="cap-z", db_path=db_path
    )
    # Assert
    assert rel == "other"


# ---------------------------------------------------------------------------
# derive_group — Gap-4 solitary override
# ---------------------------------------------------------------------------


def test_derive_group_solitary_returns_singleton_despite_siblings(
    db_path: Path,
) -> None:
    """Gap-4: a child whose policy sets ``lineage_group='solitary'`` has
    a singleton group regardless of the lineage table — no transitive
    parent-group inheritance, so a sibling capsule can never address
    it via the group-default ACL."""
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    record_lineage(child="cap-b", parent="root", db_path=db_path)
    record_comms_policy(name="cap-a", lineage_group="solitary", db_path=db_path)
    # Act
    group = derive_group(name="cap-a", db_path=db_path)
    # Assert
    assert group == {"cap-a"}


def test_derive_group_default_includes_siblings(db_path: Path) -> None:
    """Default-preservation: with no policy row, derive_group keeps the
    legacy parent + direct-children semantics."""
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    record_lineage(child="cap-b", parent="root", db_path=db_path)
    # Act
    group = derive_group(name="cap-a", db_path=db_path)
    # Assert
    assert "cap-b" in group
