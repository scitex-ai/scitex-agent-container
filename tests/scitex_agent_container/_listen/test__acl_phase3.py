"""Phase-3 ACL enforcement (ADR-0010 Step 2) — per-spec outbound/inbound
+ may_spawn, evaluated server-side by ``_listen._acl``.

Mirrors the existing ``test__acl.py`` conventions (handoff §0 no mocks:
real SQLite, real lineage rows). AAA structure, one assertion per test,
descriptive names. Each test seeds only the policy row(s) it exercises
so default-preservation tests (no row) stay byte-faithful to legacy
behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container._listen._acl import check_send_acl, check_spawn
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    record_comms_policy,
    record_lineage,
)


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db (mirrors test__acl.py's fixture)."""
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
# Gap-1 — spec.comms.outbound (sender-side per-spec deny)
# ---------------------------------------------------------------------------


def test_outbound_siblings_deny_blocks_sibling_send(db_path: Path) -> None:
    """Gap-1: sender with outbound.siblings=deny cannot address a sibling
    even though they share the same parent (group ACL would allow)."""
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    record_lineage(child="cap-b", parent="root", db_path=db_path)
    record_comms_policy(name="cap-a", outbound_siblings="deny", db_path=db_path)
    # Act
    decision, _ = check_send_acl(
        authenticated_node="cap-a",
        claimed_from_agent="cap-a",
        target="cap-b",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"


def test_outbound_parent_deny_blocks_send_to_parent(db_path: Path) -> None:
    """Gap-1: child with outbound.parent=deny cannot send to its parent."""
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    record_comms_policy(name="cap-a", outbound_parent="deny", db_path=db_path)
    # Act
    decision, _ = check_send_acl(
        authenticated_node="cap-a",
        claimed_from_agent="cap-a",
        target="root",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"


def test_outbound_default_allows_sibling_send(db_path: Path) -> None:
    """Default-preservation: with no comms policy row, the legacy
    intra-group sibling allow continues to fire (Phase-3 is opt-in)."""
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    record_lineage(child="cap-b", parent="root", db_path=db_path)
    # Act
    decision, _ = check_send_acl(
        authenticated_node="cap-a",
        claimed_from_agent="cap-a",
        target="cap-b",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


# ---------------------------------------------------------------------------
# Gap-2 — spec.comms.inbound (receiver-side per-spec deny)
# ---------------------------------------------------------------------------


def test_inbound_siblings_deny_rejects_sibling_inbound(db_path: Path) -> None:
    """Gap-2: target with inbound.siblings=deny rejects a sibling sender
    even when the sender's outbound policy permits."""
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    record_lineage(child="cap-b", parent="root", db_path=db_path)
    record_comms_policy(name="cap-b", inbound_siblings="deny", db_path=db_path)
    # Act
    decision, _ = check_send_acl(
        authenticated_node="cap-a",
        claimed_from_agent="cap-a",
        target="cap-b",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"


def test_inbound_parent_deny_rejects_send_from_parent(db_path: Path) -> None:
    """Gap-2: target with inbound.parent=deny rejects its own parent's
    send (the parent appears as ``rel='child'`` from sender's POV)."""
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    record_comms_policy(name="cap-a", inbound_parent="deny", db_path=db_path)
    # Act
    decision, _ = check_send_acl(
        authenticated_node="root",
        claimed_from_agent="root",
        target="cap-a",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"


# ---------------------------------------------------------------------------
# Gap-5 — spec.lineage.may_spawn (per-spec spawn deny)
# ---------------------------------------------------------------------------


def test_may_spawn_false_denies_root_spawn(db_path: Path) -> None:
    """Gap-5: a root caller that globally-passes the spawn ACL is still
    denied when its persisted policy carries ``may_spawn=False``."""
    # Arrange
    record_comms_policy(name="root", may_spawn=False, db_path=db_path)
    # Act
    decision, _ = check_spawn(caller="root", db_path=db_path)
    # Assert
    assert decision == "deny"


def test_may_spawn_default_preserves_root_allow(db_path: Path) -> None:
    """Default-preservation: a root with no per-spec policy row keeps the
    legacy root-only allow."""
    # Arrange — no lineage row (caller is a root) and no policy row.
    # Act
    decision, _ = check_spawn(caller="root", db_path=db_path)
    # Assert
    assert decision == "allow"
