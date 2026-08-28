"""Tests for :func:`scitex_agent_container._listen._acl.check_lineage_acl`.

PR-3 Checkpoint 3 — pins the lineage-scoped ACL gate that the
DELETE, STATUS, send, and tail surfaces consume. AAA + one-assert
(PA-307); real sqlite (no mocks, PA-306).

Contract:
  caller may operate on target iff
    caller is None / ""              (admin / operator path)   OR
    caller == target                 (self-management)          OR
    target in descendants(caller)    (lineage scope)
  → otherwise deny with structured reason naming caller + target.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._listen._acl import check_lineage_acl
from scitex_agent_container._state.state_db_nodes import record_lineage


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


# ---------------------------------------------------------------------------
# Allow path — admin
# ---------------------------------------------------------------------------


def test_admin_caller_is_allowed(db_path: Path) -> None:
    # Arrange — caller None is the host-wide bearer / operator
    # path; always allowed.
    # Act
    decision, _ = check_lineage_acl(caller=None, target="any-target", db_path=db_path)
    # Assert
    assert decision == "allow"


def test_empty_caller_is_treated_as_admin(db_path: Path) -> None:
    # Arrange — defensive: empty string normalises to admin
    # (matches spawn_allowed's treatment).
    # Act
    decision, _ = check_lineage_acl(caller="", target="x", db_path=db_path)
    # Assert
    assert decision == "allow"


# ---------------------------------------------------------------------------
# Allow path — self
# ---------------------------------------------------------------------------


def test_self_management_is_allowed(db_path: Path) -> None:
    # Arrange — caller managing its own runtime (e.g. status of self).
    # Act
    decision, _ = check_lineage_acl(caller="alice", target="alice", db_path=db_path)
    # Assert
    assert decision == "allow"


# ---------------------------------------------------------------------------
# Allow path — descendant via lineage
# ---------------------------------------------------------------------------


def test_direct_child_is_allowed(pg_schema: str, db_path: Path) -> None:
    # Arrange — root → child edge.
    record_lineage(child="kid", parent="root", db_path=db_path)
    # Act
    decision, _ = check_lineage_acl(caller="root", target="kid", db_path=db_path)
    # Assert
    assert decision == "allow"


def test_transitive_descendant_is_allowed(pg_schema: str, db_path: Path) -> None:
    # Arrange — root → alice → ada (grandchild).
    record_lineage(child="alice", parent="root", db_path=db_path)
    record_lineage(child="ada", parent="alice", db_path=db_path)
    # Act
    decision, _ = check_lineage_acl(caller="root", target="ada", db_path=db_path)
    # Assert
    assert decision == "allow"


# ---------------------------------------------------------------------------
# Deny path
# ---------------------------------------------------------------------------


def test_deny_when_caller_has_no_lineage_to_target(pg_schema: str, db_path: Path) -> None:
    # Arrange — alice and bob are unrelated roots.
    # (no lineage records: both are independent root nodes)
    # Act
    decision, _ = check_lineage_acl(caller="alice", target="bob", db_path=db_path)
    # Assert
    assert decision == "deny"


def test_deny_for_sibling_target(pg_schema: str, db_path: Path) -> None:
    # Arrange — alice + bob share a parent but are siblings.
    # Lineage gate denies sibling control (only descendants).
    record_lineage(child="alice", parent="root", db_path=db_path)
    record_lineage(child="bob", parent="root", db_path=db_path)
    # Act
    decision, _ = check_lineage_acl(caller="alice", target="bob", db_path=db_path)
    # Assert
    assert decision == "deny"


def test_deny_for_ancestor_target(pg_schema: str, db_path: Path) -> None:
    # Arrange — child cannot operate on its parent (the gate is
    # downward only).
    record_lineage(child="kid", parent="root", db_path=db_path)
    # Act
    decision, _ = check_lineage_acl(caller="kid", target="root", db_path=db_path)
    # Assert
    assert decision == "deny"


def test_deny_reason_names_caller_and_target(pg_schema: str, db_path: Path) -> None:
    # Arrange — the deny reason must identify both names so the
    # 403 body lets the operator diagnose without guessing.
    record_lineage(child="alice", parent="root", db_path=db_path)
    # Act
    _, reason = check_lineage_acl(caller="alice", target="unrelated", db_path=db_path)
    # Assert
    assert "alice" in (reason or "") and "unrelated" in (reason or "")


# ---------------------------------------------------------------------------
# Returns tuple shape compatible with deny_response
# ---------------------------------------------------------------------------


def test_returns_allow_tuple_with_none_reason(db_path: Path) -> None:
    # Arrange
    # Act
    decision, reason = check_lineage_acl(caller=None, target="x", db_path=db_path)
    # Assert
    assert (decision, reason) == ("allow", None)


def test_returns_deny_tuple_with_string_reason(pg_schema: str, db_path: Path) -> None:
    # Arrange
    # Act
    decision, reason = check_lineage_acl(caller="alice", target="bob", db_path=db_path)
    # Assert — deny carries a non-empty reason string for the body.
    assert decision == "deny" and isinstance(reason, str) and reason
