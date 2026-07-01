"""Named-group persistence + readers (group-based a2a ACL, 2026-06-25).

Covers the state-DB half of the group ACL:

* ``record_comms_policy`` persists ``group_name`` and ``read_comms_policy``
  round-trips it (default ``""`` when absent).
* ``resolve_group_name`` returns the persisted group / ``""``.
* ``same_named_group`` is True only for two NON-EMPTY equal groups.
* ``is_developer`` recognises a developer-group member.

AAA (each marker on its own line), one assertion per test, no mocks —
real on-disk SQLite via the yield-based ``db_path`` env override.
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
# record / read round-trip
# ---------------------------------------------------------------------------


def test_read_comms_policy_group_name_defaults_empty(db_path: Path) -> None:
    """A node with no policy row reports the ungrouped default."""
    # Arrange — no record_comms_policy call.
    # Act
    policy = read_comms_policy(name="never-recorded", db_path=db_path)
    # Assert
    assert policy["group_name"] == ""


def test_record_comms_policy_persists_group_name(db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="developer", db_path=db_path)
    # Act
    policy = read_comms_policy(name="alice", db_path=db_path)
    # Assert
    assert policy["group_name"] == "developer"


def test_record_comms_policy_group_name_is_trimmed(db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="  analysts  ", db_path=db_path)
    # Act
    policy = read_comms_policy(name="alice", db_path=db_path)
    # Assert
    assert policy["group_name"] == "analysts"


def test_record_comms_policy_group_name_upsert_refreshes(db_path: Path) -> None:
    """A re-record (e.g. a restart after a spec edit) updates the group."""
    # Arrange
    record_comms_policy(name="alice", group_name="analysts", db_path=db_path)
    record_comms_policy(name="alice", group_name="developer", db_path=db_path)
    # Act
    policy = read_comms_policy(name="alice", db_path=db_path)
    # Assert
    assert policy["group_name"] == "developer"


def test_record_comms_policy_rejects_non_str_group_name(db_path: Path) -> None:
    # Arrange
    bad_group = 123
    # Act
    raises = pytest.raises(ValueError)
    # Assert
    with raises:
        record_comms_policy(name="alice", group_name=bad_group, db_path=db_path)


# ---------------------------------------------------------------------------
# resolve_group_name
# ---------------------------------------------------------------------------


def test_resolve_group_name_returns_persisted_group(db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="developer", db_path=db_path)
    # Act
    group = resolve_group_name(name="alice", db_path=db_path)
    # Assert
    assert group == "developer"


def test_resolve_group_name_unknown_node_is_empty(db_path: Path) -> None:
    # Arrange — no row.
    # Act
    group = resolve_group_name(name="ghost", db_path=db_path)
    # Assert
    assert group == ""


# ---------------------------------------------------------------------------
# same_named_group
# ---------------------------------------------------------------------------


def test_same_named_group_true_for_equal_nonempty(db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="developer", db_path=db_path)
    record_comms_policy(name="bob", group_name="developer", db_path=db_path)
    # Act
    result = same_named_group(sender="alice", target="bob", db_path=db_path)
    # Assert
    assert result is True


def test_same_named_group_false_for_different_groups(db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="developer", db_path=db_path)
    record_comms_policy(name="bob", group_name="analysts", db_path=db_path)
    # Act
    result = same_named_group(sender="alice", target="bob", db_path=db_path)
    # Assert
    assert result is False


def test_same_named_group_false_when_both_ungrouped(db_path: Path) -> None:
    """Two ungrouped agents must NOT match — absence is a no-op."""
    # Arrange — neither has a group_name.
    record_comms_policy(name="alice", db_path=db_path)
    record_comms_policy(name="bob", db_path=db_path)
    # Act
    result = same_named_group(sender="alice", target="bob", db_path=db_path)
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# is_developer
# ---------------------------------------------------------------------------


def test_is_developer_true_for_developer_group_member(db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="developer", db_path=db_path)
    # Act
    result = is_developer(name="alice", db_path=db_path)
    # Assert
    assert result is True


def test_is_developer_false_for_other_group_member(db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="analysts", db_path=db_path)
    # Act
    result = is_developer(name="alice", db_path=db_path)
    # Assert
    assert result is False
