"""WI-2 — lineage + grant primitives (limited scope per lead 2026-05-20).

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-2 "ACL: permissioned
messaging"):

  * Group-based default ACL — derive from lineage.
  * Cross-group grants — accepted, keyed on the self-claimed
    ``metadata.from_agent`` (with the audit caveat documented per
    grant).
  * Spawn-permission policy — root-only by current policy
    (lift-able).

The "authenticated sender identity" / "identity cannot be spoofed"
acceptance criterion is DEFERRED (lead 2026-05-20) to a separate
follow-on handoff related to sac-accounts.

No mocks (handoff §0): real SQLite under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    derive_group,
    grant_send,
    has_grant,
    list_comms_grants,
    list_node_tokens,
    mint_node_token,
    record_comms_policy,
    record_lineage,
    resolve_node_token,
    revoke_send,
    spawn_allowed,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # Arrange
    p = tmp_path / "state.db"
    state_db.init_schema(p)
    return p


# ---------------------------------------------------------------------------
# Schema — lineage + comms_grants tables exist
# ---------------------------------------------------------------------------


def test_lineage_table_exists(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lineage'"
        ).fetchall()
    # Assert
    assert len(rows) == 1


def test_comms_grants_table_exists(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='comms_grants'"
        ).fetchall()
    # Assert
    assert len(rows) == 1


@pytest.mark.parametrize(
    "column", ["sender_name", "target_name", "created_at", "note"]
)
def test_comms_grants_has_column(db_path: Path, column: str) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(comms_grants)").fetchall()
        }
    # Assert
    assert column in cols


# ---------------------------------------------------------------------------
# record_lineage — parent → child edges
# ---------------------------------------------------------------------------


def test_record_lineage_persists_parent_pointer(db_path: Path) -> None:
    # Arrange
    record_lineage(child="bob", parent="alice", db_path=db_path)
    # Act
    conn_ctx = state_db.open_db(db_path)
    with conn_ctx as conn:
        row = conn.execute(
            "SELECT parent_name FROM lineage WHERE child_name='bob'"
        ).fetchone()
    # Assert
    assert row["parent_name"] == "alice"


def test_record_lineage_idempotent_no_duplicate_rows(db_path: Path) -> None:
    """Re-recording the same edge does not duplicate the row."""
    # Arrange
    record_lineage(child="bob", parent="alice", db_path=db_path)
    record_lineage(child="bob", parent="alice", db_path=db_path)
    # Act
    conn_ctx = state_db.open_db(db_path)
    with conn_ctx as conn:
        rows = conn.execute(
            "SELECT child_name FROM lineage WHERE child_name='bob'"
        ).fetchall()
    # Assert
    assert len(rows) == 1


def test_record_lineage_re_parent_raises(db_path: Path) -> None:
    """A child cannot silently switch parents."""
    # Arrange
    record_lineage(child="bob", parent="alice", db_path=db_path)
    # Act
    # Assert
    with pytest.raises(ValueError, match="refusing to re-parent"):
        record_lineage(child="bob", parent="other-root", db_path=db_path)


# ---------------------------------------------------------------------------
# derive_group — the heart of the default ACL check
# ---------------------------------------------------------------------------


def test_derive_group_of_root_with_no_children_is_self_only(db_path: Path) -> None:
    # Arrange
    name = "root"
    # Act
    group = derive_group(name=name, db_path=db_path)
    # Assert
    assert group == {"root"}


def test_derive_group_of_parent_includes_direct_children(db_path: Path) -> None:
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    # Act
    group = derive_group(name="root", db_path=db_path)
    # Assert
    assert group == {"root", "worker-a", "worker-b"}


def test_derive_group_of_child_includes_parent_and_siblings(db_path: Path) -> None:
    """Sibling sees the same group as the parent does — bidirectional."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    # Act
    group = derive_group(name="worker-a", db_path=db_path)
    # Assert
    assert group == {"root", "worker-a", "worker-b"}


def test_derive_group_excludes_cross_group_nodes(db_path: Path) -> None:
    """A different root's children are not in this group."""
    # Arrange — two unrelated families
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    # Act
    group = derive_group(name="child-1", db_path=db_path)
    # Assert
    assert group == {"root-1", "child-1"}


def test_derive_group_of_unknown_node_is_singleton(db_path: Path) -> None:
    """A fresh, unattached node is its own singleton group."""
    # Arrange
    name = "fresh"
    # Act
    group = derive_group(name=name, db_path=db_path)
    # Assert
    assert group == {"fresh"}


# ---------------------------------------------------------------------------
# spawn_allowed — root-only spawn policy
# ---------------------------------------------------------------------------


def test_spawn_allowed_returns_true_for_admin_caller(db_path: Path) -> None:
    """``caller=None`` → admin / operator path → allowed."""
    # Arrange
    caller = None
    # Act
    allowed, _reason = spawn_allowed(caller=caller, db_path=db_path)
    # Assert
    assert allowed is True


def test_spawn_allowed_returns_true_for_root_node(db_path: Path) -> None:
    """A node with no parent → root → allowed."""
    # Arrange
    caller = "root"
    # Act
    allowed, _reason = spawn_allowed(caller=caller, db_path=db_path)
    # Assert
    assert allowed is True


def test_spawn_allowed_returns_false_for_child_node(db_path: Path) -> None:
    """A node with a parent → child → denied under current policy."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    # Act
    allowed, _reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert allowed is False


def test_spawn_allowed_deny_reason_explains_role_policy(
    db_path: Path,
) -> None:
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    # Act
    _allowed, reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert reason is not None and "not one of the roles permitted to spawn" in reason


def test_spawn_allowed_returns_true_for_developer_group_child(
    db_path: Path,
) -> None:
    """A developer-group child may spawn even though it has a parent."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_comms_policy(name="worker-a", group_name="developer", db_path=db_path)
    # Act
    allowed, _reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert allowed is True


def test_spawn_allowed_returns_true_for_researcher_group_child(
    db_path: Path,
) -> None:
    """A researcher-group child may spawn even though it has a parent."""
    # Arrange
    record_lineage(child="neurovista", parent="scitex-cv", db_path=db_path)
    record_comms_policy(name="neurovista", group_name="researcher", db_path=db_path)
    # Act
    allowed, _reason = spawn_allowed(caller="neurovista", db_path=db_path)
    # Assert
    assert allowed is True


def test_spawn_allowed_returns_false_for_non_dev_research_group_child(
    db_path: Path,
) -> None:
    """A child in an unrelated named group is still denied."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_comms_policy(name="worker-a", group_name="analysts", db_path=db_path)
    # Act
    allowed, _reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert allowed is False


def test_spawn_allowed_deny_reason_for_non_dev_research_group_child(
    db_path: Path,
) -> None:
    """The deny reason names the role-based policy."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_comms_policy(name="worker-a", group_name="analysts", db_path=db_path)
    # Act
    _allowed, reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert reason is not None and "not one of the roles permitted to spawn" in reason


def test_spawn_allowed_may_spawn_false_still_denies_developer_child(
    db_path: Path,
) -> None:
    """Per-spec may_spawn=false overrides the developer-group allow."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_comms_policy(
        name="worker-a",
        group_name="developer",
        may_spawn=False,
        db_path=db_path,
    )
    # Act
    allowed, _reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert allowed is False


def test_spawn_allowed_may_spawn_false_reason_for_developer_child(
    db_path: Path,
) -> None:
    """The deny reason names the per-spec may_spawn=false override."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_comms_policy(
        name="worker-a",
        group_name="developer",
        may_spawn=False,
        db_path=db_path,
    )
    # Act
    _allowed, reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert reason is not None and "may_spawn=false" in reason


def test_spawn_allowed_may_spawn_false_still_denies_researcher_child(
    db_path: Path,
) -> None:
    """Per-spec may_spawn=false overrides the researcher-group allow."""
    # Arrange
    record_lineage(child="neurovista", parent="scitex-cv", db_path=db_path)
    record_comms_policy(
        name="neurovista",
        group_name="researcher",
        may_spawn=False,
        db_path=db_path,
    )
    # Act
    allowed, _reason = spawn_allowed(caller="neurovista", db_path=db_path)
    # Assert
    assert allowed is False


def test_spawn_allowed_may_spawn_false_reason_for_researcher_child(
    db_path: Path,
) -> None:
    """The deny reason names the per-spec may_spawn=false override."""
    # Arrange
    record_lineage(child="neurovista", parent="scitex-cv", db_path=db_path)
    record_comms_policy(
        name="neurovista",
        group_name="researcher",
        may_spawn=False,
        db_path=db_path,
    )
    # Act
    _allowed, reason = spawn_allowed(caller="neurovista", db_path=db_path)
    # Assert
    assert reason is not None and "may_spawn=false" in reason


# ---------------------------------------------------------------------------
# grant_send / has_grant / revoke_send — cross-group ACL grants
# ---------------------------------------------------------------------------


def test_has_grant_returns_false_when_no_grant(db_path: Path) -> None:
    # Arrange
    # (no grant)
    # Act
    granted = has_grant(sender="alice", target="bob", db_path=db_path)
    # Assert
    assert granted is False


def test_grant_send_makes_has_grant_true(db_path: Path) -> None:
    # Arrange
    grant_send(sender="alice", target="bob", db_path=db_path)
    # Act
    granted = has_grant(sender="alice", target="bob", db_path=db_path)
    # Assert
    assert granted is True


def test_grant_send_is_directional(db_path: Path) -> None:
    """A grant alice→bob does NOT imply bob→alice."""
    # Arrange
    grant_send(sender="alice", target="bob", db_path=db_path)
    # Act
    reverse_granted = has_grant(sender="bob", target="alice", db_path=db_path)
    # Assert
    assert reverse_granted is False


def test_grant_send_records_caller_supplied_audit_note(db_path: Path) -> None:
    """An operator-supplied ``note`` round-trips into ``comms_grants``
    so the audit trail records *why* the grant was authorised."""
    # Arrange
    grant_send(
        sender="alice",
        target="bob",
        db_path=db_path,
        note="handoff-2026-05-21",
    )
    # Act
    rows = list_comms_grants(db_path=db_path)
    # Assert
    assert rows and rows[0]["note"] == "handoff-2026-05-21"


def test_grant_send_idempotent_no_duplicate_rows(db_path: Path) -> None:
    # Arrange
    grant_send(sender="alice", target="bob", db_path=db_path)
    grant_send(sender="alice", target="bob", db_path=db_path)
    # Act
    rows = list_comms_grants(db_path=db_path)
    # Assert
    assert len(rows) == 1


def test_revoke_send_removes_existing_grant(db_path: Path) -> None:
    # Arrange
    grant_send(sender="alice", target="bob", db_path=db_path)
    # Act
    removed = revoke_send(sender="alice", target="bob", db_path=db_path)
    # Assert
    assert removed is True


def test_revoke_send_makes_has_grant_false(db_path: Path) -> None:
    # Arrange
    grant_send(sender="alice", target="bob", db_path=db_path)
    revoke_send(sender="alice", target="bob", db_path=db_path)
    # Act
    granted = has_grant(sender="alice", target="bob", db_path=db_path)
    # Assert
    assert granted is False


def test_revoke_send_returns_false_when_no_grant_exists(db_path: Path) -> None:
    # Arrange
    # (no grant)
    # Act
    removed = revoke_send(sender="alice", target="bob", db_path=db_path)
    # Assert
    assert removed is False


def test_list_comms_grants_returns_each_grant_pair(db_path: Path) -> None:
    # Arrange
    grant_send(sender="alice", target="bob", db_path=db_path)
    grant_send(sender="root-1", target="child-2", db_path=db_path)
    # Act
    rows = list_comms_grants(db_path=db_path)
    # Assert
    pairs = sorted((r["sender"], r["target"]) for r in rows)
    assert pairs == [("alice", "bob"), ("root-1", "child-2")]


# ---------------------------------------------------------------------------
# node_tokens — authenticated identity primitive (handoff §4 acceptance)
# ---------------------------------------------------------------------------


def test_node_tokens_table_exists(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='node_tokens'"
        ).fetchall()
    # Assert
    assert len(rows) == 1


def test_mint_node_token_returns_non_empty_string(db_path: Path) -> None:
    # Arrange
    name = "alice"
    # Act
    token = mint_node_token(name=name, db_path=db_path)
    # Assert
    assert isinstance(token, str) and len(token) >= 32


def test_mint_node_token_is_idempotent_per_name(db_path: Path) -> None:
    """Re-registration returns the same token, so an active bearer
    keeps working across a re-register."""
    # Arrange
    first = mint_node_token(name="alice", db_path=db_path)
    # Act
    second = mint_node_token(name="alice", db_path=db_path)
    # Assert
    assert first == second


def test_mint_node_token_is_unique_per_name(db_path: Path) -> None:
    # Arrange
    a = mint_node_token(name="alice", db_path=db_path)
    b = mint_node_token(name="bob", db_path=db_path)
    # Act
    different = a != b
    # Assert
    assert different is True


def test_resolve_node_token_returns_minted_identity(db_path: Path) -> None:
    # Arrange
    token = mint_node_token(name="alice", db_path=db_path)
    # Act
    resolved = resolve_node_token(token=token, db_path=db_path)
    # Assert
    assert resolved == "alice"


def test_resolve_node_token_returns_none_for_unknown_bearer(
    db_path: Path,
) -> None:
    # Arrange
    bogus = "no-such-token-1234567890abcdef"
    # Act
    resolved = resolve_node_token(token=bogus, db_path=db_path)
    # Assert
    assert resolved is None


def test_resolve_node_token_returns_none_for_empty_string(
    db_path: Path,
) -> None:
    # Arrange
    empty = ""
    # Act
    resolved = resolve_node_token(token=empty, db_path=db_path)
    # Assert
    assert resolved is None


def test_list_node_tokens_returns_each_minted_name(db_path: Path) -> None:
    """The token observability surface returns names (NOT token
    values — that would defeat the purpose of storing them as
    secrets)."""
    # Arrange
    mint_node_token(name="alice", db_path=db_path)
    mint_node_token(name="bob", db_path=db_path)
    # Act
    rows = list_node_tokens(db_path=db_path)
    # Assert
    names = sorted(r["name"] for r in rows)
    assert names == ["alice", "bob"]
