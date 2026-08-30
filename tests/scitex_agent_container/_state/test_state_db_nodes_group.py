"""Named-group persistence + readers (group-based a2a ACL, 2026-06-25).

Covers the state-DB half of the group ACL:

* ``record_comms_policy`` persists ``group_name`` and ``read_comms_policy``
  round-trips it (default ``""`` when absent).
* ``resolve_group_name`` returns the persisted group / ``""``.
* ``same_named_group`` is True only for two NON-EMPTY equal groups.
* ``is_developer`` recognises a developer-group member.

AAA (each marker on its own line), one assertion per test, no mocks —
real on-disk state via the yield-based ``db_path`` env override.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    is_developer,
    read_comms_policy,
    record_comms_policy,
    resolve_group_name,
    same_named_group,
)


@pytest.fixture
def db_path(tmp_path: Path):
    # Arrange
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = state_db.DEFAULT_DB_PATH
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env


# ---------------------------------------------------------------------------
# record / read round-trip
# ---------------------------------------------------------------------------


def test_read_comms_policy_group_name_defaults_empty(pg_schema: str, db_path: Path) -> None:
    """A node with no policy row reports the ungrouped default."""
    # Arrange — no record_comms_policy call.
    # Act
    policy = read_comms_policy(name="never-recorded")
    # Assert
    assert policy["group_name"] == ""


def test_record_comms_policy_persists_group_name(pg_schema: str, db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="developer")
    # Act
    policy = read_comms_policy(name="alice")
    # Assert
    assert policy["group_name"] == "developer"


def test_record_comms_policy_group_name_is_trimmed(pg_schema: str, db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="  analysts  ")
    # Act
    policy = read_comms_policy(name="alice")
    # Assert
    assert policy["group_name"] == "analysts"


def test_record_comms_policy_group_name_upsert_refreshes(pg_schema: str, db_path: Path) -> None:
    """A re-record (e.g. a restart after a spec edit) updates the group."""
    # Arrange
    record_comms_policy(name="alice", group_name="analysts")
    record_comms_policy(name="alice", group_name="developer")
    # Act
    policy = read_comms_policy(name="alice")
    # Assert
    assert policy["group_name"] == "developer"


def test_record_comms_policy_rejects_non_str_group_name(db_path: Path) -> None:
    # Arrange
    bad_group = 123
    # Act
    raises = pytest.raises(ValueError)
    # Assert
    with raises:
        record_comms_policy(name="alice", group_name=bad_group)


# ---------------------------------------------------------------------------
# resolve_group_name
# ---------------------------------------------------------------------------


def test_resolve_group_name_returns_persisted_group(pg_schema: str, db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="developer")
    # Act
    group = resolve_group_name(name="alice")
    # Assert
    assert group == "developer"


def test_resolve_group_name_unknown_node_is_empty(pg_schema: str, db_path: Path) -> None:
    # Arrange — no row.
    # Act
    group = resolve_group_name(name="ghost")
    # Assert
    assert group == ""


# ---------------------------------------------------------------------------
# same_named_group
# ---------------------------------------------------------------------------


def test_same_named_group_true_for_equal_nonempty(pg_schema: str, db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="developer")
    record_comms_policy(name="bob", group_name="developer")
    # Act
    result = same_named_group(sender="alice", target="bob")
    # Assert
    assert result is True


def test_same_named_group_false_for_different_groups(pg_schema: str, db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="developer")
    record_comms_policy(name="bob", group_name="analysts")
    # Act
    result = same_named_group(sender="alice", target="bob")
    # Assert
    assert result is False


def test_same_named_group_false_when_both_ungrouped(pg_schema: str, db_path: Path) -> None:
    """Two ungrouped agents must NOT match — absence is a no-op."""
    # Arrange — neither has a group_name.
    record_comms_policy(name="alice")
    record_comms_policy(name="bob")
    # Act
    result = same_named_group(sender="alice", target="bob")
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# is_developer
# ---------------------------------------------------------------------------


def test_is_developer_true_for_developer_group_member(pg_schema: str, db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="developer")
    # Act
    result = is_developer(name="alice")
    # Assert
    assert result is True


def test_is_developer_false_for_other_group_member(pg_schema: str, db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="analysts")
    # Act
    result = is_developer(name="alice")
    # Assert
    assert result is False
