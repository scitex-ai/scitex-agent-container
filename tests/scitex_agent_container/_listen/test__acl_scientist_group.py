"""Scientist-group a2a ACL (operator 2026-06-25).

Builds on the merged group-based ACL foundation:

* Sub-goal 1 — scientist↔scientist FULL MESH. Two scientist-group
  agents may address each other with NO lineage edge and NO grant.
  This is already covered by the named-group mesh
  (``same_named_group``); these tests assert it for the scientist
  group specifically.
* Sub-goal 2 — scientist↔developer cross-group PEERING by DEFAULT, in
  BOTH directions, with no per-pair grant. Driven by the
  ``scientist``↔``developer`` entry in the
  :data:`config._group_resolver._PEERED_GROUPS` allowlist.
* Scoped, not blanket-open — a scientist→UNRELATED-group send (e.g.
  ``analysts``) is still DENIED without an explicit grant.

Groups are seeded via ``record_comms_policy(group_name=...)`` — the same
row ``agent_start`` writes from the resolved spec label. No mocks: real
on-disk SQLite via the yield-based ``db_path`` env override.

AAA (each marker on its own line), one assertion per test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container._listen._acl import check_send_acl
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import record_comms_policy


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
# Sub-goal 1 — scientist↔scientist full mesh.
# ---------------------------------------------------------------------------


def test_scientist_to_scientist_allowed(db_path: Path) -> None:
    """Same scientist group, no lineage, no grant → allow (full mesh)."""
    # Arrange
    record_comms_policy(name="clew", group_name="scientist", db_path=db_path)
    record_comms_policy(name="ripple-wm", group_name="scientist", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="clew",
        claimed_from_agent="clew",
        target="ripple-wm",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


# ---------------------------------------------------------------------------
# Sub-goal 2 — scientist↔developer cross-group peering (both directions).
# ---------------------------------------------------------------------------


def test_scientist_to_developer_allowed(db_path: Path) -> None:
    """scientist → developer peered by default, no grant → allow."""
    # Arrange
    record_comms_policy(name="clew", group_name="scientist", db_path=db_path)
    record_comms_policy(name="figrecipe", group_name="developer", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="clew",
        claimed_from_agent="clew",
        target="figrecipe",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_developer_to_scientist_allowed(db_path: Path) -> None:
    """developer → scientist peered by default (reverse direction) → allow."""
    # Arrange
    record_comms_policy(name="clew", group_name="scientist", db_path=db_path)
    record_comms_policy(name="figrecipe", group_name="developer", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="figrecipe",
        claimed_from_agent="figrecipe",
        target="clew",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


# ---------------------------------------------------------------------------
# Scoped, not blanket-open — unrelated third group stays denied.
# ---------------------------------------------------------------------------


def test_scientist_to_unrelated_group_still_denied(db_path: Path) -> None:
    """scientist → an UNRELATED group (analysts) is not peered → deny."""
    # Arrange
    record_comms_policy(name="clew", group_name="scientist", db_path=db_path)
    record_comms_policy(name="quant", group_name="analysts", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="clew",
        claimed_from_agent="clew",
        target="quant",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"
