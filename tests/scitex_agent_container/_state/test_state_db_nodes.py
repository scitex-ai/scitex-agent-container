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
    list_node_tokens,
    mint_node_token,
    record_comms_policy,
    record_lineage,
    resolve_node_token,
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
            "SELECT name FROM sqlite_master WHERE type='table' AND name='comms_grants'"
        ).fetchall()
    # Assert
    assert len(rows) == 1


@pytest.mark.parametrize("column", ["sender_name", "target_name", "created_at", "note"])
def test_comms_grants_has_column(db_path: Path, column: str) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(comms_grants)").fetchall()
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


def test_record_lineage_re_parent_keeps_existing_parent(db_path: Path) -> None:
    """A re-parent attempt keeps the original parent (no raise, no switch).

    A restart of an existing agent by a different-lineage caller must not
    be blocked and must not re-parent — the original parent is kept, so
    identity drift stays impossible while restarts succeed. (No raise is
    implicit: a raising record_lineage would error this test.)
    """
    # Arrange
    record_lineage(child="bob", parent="alice", db_path=db_path)
    # Act — a different parent must NOT raise; it keeps "alice"
    record_lineage(child="bob", parent="other-root", db_path=db_path)
    # Assert — original parent kept, not switched to the new caller
    conn_ctx = state_db.open_db(db_path)
    with conn_ctx as conn:
        row = conn.execute(
            "SELECT parent_name FROM lineage WHERE child_name='bob'"
        ).fetchone()
    assert row["parent_name"] == "alice"


# ---------------------------------------------------------------------------
# derive_group — the heart of the default ACL check
# ---------------------------------------------------------------------------


def test_derive_group_of_root_with_no_children_is_self_only(pg_schema: str, db_path: Path) -> None:
    # Arrange
    name = "root"
    # Act
    group = derive_group(name=name, db_path=db_path)
    # Assert
    assert group == {"root"}


def test_derive_group_of_parent_includes_direct_children(pg_schema: str, db_path: Path) -> None:
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    # Act
    group = derive_group(name="root", db_path=db_path)
    # Assert
    assert group == {"root", "worker-a", "worker-b"}


def test_derive_group_of_child_includes_parent_and_siblings(pg_schema: str, db_path: Path) -> None:
    """Sibling sees the same group as the parent does — bidirectional."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    # Act
    group = derive_group(name="worker-a", db_path=db_path)
    # Assert
    assert group == {"root", "worker-a", "worker-b"}


def test_derive_group_excludes_cross_group_nodes(pg_schema: str, db_path: Path) -> None:
    """A different root's children are not in this group."""
    # Arrange — two unrelated families
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    # Act
    group = derive_group(name="child-1", db_path=db_path)
    # Assert
    assert group == {"root-1", "child-1"}


def test_derive_group_of_unknown_node_is_singleton(pg_schema: str, db_path: Path) -> None:
    """A fresh, unattached node is its own singleton group."""
    # Arrange
    name = "fresh"
    # Act
    group = derive_group(name=name, db_path=db_path)
    # Assert
    assert group == {"fresh"}


# ---------------------------------------------------------------------------
# derive_group — the Gap-4 solitary override.
#
# These two moved here from test_state_db_acl_policy.py on 2026-08-28. They
# are about derive_group, which now STRADDLES two databases: the policy read
# is PostgreSQL and the lineage walk is still SQLite, so they take both
# ``pg_schema`` and ``db_path``. Left in the acl-policy file they would have
# read as coverage of the migrated module, which they are not.
# ---------------------------------------------------------------------------


def test_derive_group_solitary_returns_a_singleton_despite_siblings(
    pg_schema: str, db_path: Path
) -> None:
    """Gap-4: ``lineage_group='solitary'`` isolates a capsule from its
    siblings AND its parent, without depending on the lineage table being
    empty — so a sibling capsule can never address it via the group-default
    ACL even though they share a parent edge."""
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    record_lineage(child="cap-b", parent="root", db_path=db_path)
    record_comms_policy(name="cap-a", lineage_group="solitary")
    # Act
    group = derive_group(name="cap-a", db_path=db_path)
    # Assert
    assert group == {"cap-a"}


def test_derive_group_without_a_policy_keeps_the_legacy_siblings(
    pg_schema: str, db_path: Path
) -> None:
    """Default-preservation: with no policy record, derive_group keeps the
    legacy parent + direct-children semantics."""
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    record_lineage(child="cap-b", parent="root", db_path=db_path)
    # Act
    group = derive_group(name="cap-a", db_path=db_path)
    # Assert
    assert "cap-b" in group


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


def test_spawn_allowed_returns_true_for_root_node(pg_schema: str, db_path: Path) -> None:
    """A node with no parent → root → allowed."""
    # Arrange
    caller = "root"
    # Act
    allowed, _reason = spawn_allowed(caller=caller, db_path=db_path)
    # Assert
    assert allowed is True


def test_spawn_allowed_returns_false_for_child_node(pg_schema: str, db_path: Path) -> None:
    """A node with a parent → child → denied under current policy."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    # Act
    allowed, _reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert allowed is False


def test_spawn_allowed_deny_reason_explains_role_policy(
    pg_schema: str,
    db_path: Path,
) -> None:
    """The reason names the groups that WOULD have authorised the spawn.

    It no longer asserts the caller "is in none of" them: that sentence
    was a claim about the AGENT, and when the multi-group defect made a
    caller's ``developer`` label unreadable it was flatly false against
    the same server's own a2a_peers output (2026-08-10).
    """
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    # Act
    _allowed, reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert reason is not None and "developer, researcher, privileged" in reason


def test_spawn_deny_reason_for_unregistered_caller_says_it_has_no_row(
    pg_schema: str,
    db_path: Path,
) -> None:
    """No policy row and "registered but ungrouped" both resolve to an
    empty group set, and they are DIFFERENT facts (2026-08-09)."""
    # Arrange — a lineage edge but no node_comms_policy row.
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    # Act
    _allowed, reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert "NO node_comms_policy row" in reason


def test_spawn_allowed_returns_true_for_developer_group_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """A developer-group child may spawn even though it has a parent."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_comms_policy(name="worker-a", group_name="developer")
    # Act
    allowed, _reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert allowed is True


def test_spawn_allowed_returns_true_for_researcher_group_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """A researcher-group child may spawn even though it has a parent."""
    # Arrange
    record_lineage(child="neurovista", parent="scitex-cv", db_path=db_path)
    record_comms_policy(name="neurovista", group_name="researcher")
    # Act
    allowed, _reason = spawn_allowed(caller="neurovista", db_path=db_path)
    # Assert
    assert allowed is True


def test_spawn_allowed_returns_true_for_privileged_group_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """A privileged-group child may spawn (operator ruling 2026-07-16).

    Watch-it-fail: on the pre-fix code this is the exact 403 the
    dotfiles agent (groups [privileged, infra]) hit — the nominally
    strongest group was absent from the spawn allowlist.
    """
    # Arrange
    record_lineage(child="dotfiles", parent="root", db_path=db_path)
    record_comms_policy(name="dotfiles", group_name="privileged")
    # Act
    allowed, _reason = spawn_allowed(caller="dotfiles", db_path=db_path)
    # Assert
    assert allowed is True


def test_spawn_allowed_returns_false_for_non_dev_research_group_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """A child in an unrelated named group is still denied."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_comms_policy(name="worker-a", group_name="analysts")
    # Act
    allowed, _reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert allowed is False


def test_spawn_allowed_deny_reason_for_non_dev_research_group_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """The deny reason reports the group the gate ACTUALLY resolved.

    Reporting what the gate SAW — rather than asserting what the agent
    is — is what lets a reader tell a correct denial from a stale row
    without guessing (2026-08-10).
    """
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_comms_policy(name="worker-a", group_name="analysts")
    # Act
    _allowed, reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert reason is not None and "['analysts']" in reason


def test_spawn_allowed_may_spawn_false_still_denies_developer_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """Per-spec may_spawn=false overrides the developer-group allow."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_comms_policy(
        name="worker-a",
        group_name="developer",
        may_spawn=False,
    )
    # Act
    allowed, _reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert allowed is False


def test_spawn_allowed_may_spawn_false_reason_for_developer_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """The deny reason names the per-spec may_spawn=false override."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_comms_policy(
        name="worker-a",
        group_name="developer",
        may_spawn=False,
    )
    # Act
    _allowed, reason = spawn_allowed(caller="worker-a", db_path=db_path)
    # Assert
    assert reason is not None and "may_spawn=false" in reason


def test_spawn_allowed_may_spawn_false_still_denies_researcher_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """Per-spec may_spawn=false overrides the researcher-group allow."""
    # Arrange
    record_lineage(child="neurovista", parent="scitex-cv", db_path=db_path)
    record_comms_policy(
        name="neurovista",
        group_name="researcher",
        may_spawn=False,
    )
    # Act
    allowed, _reason = spawn_allowed(caller="neurovista", db_path=db_path)
    # Assert
    assert allowed is False


def test_spawn_allowed_may_spawn_false_reason_for_researcher_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """The deny reason names the per-spec may_spawn=false override."""
    # Arrange
    record_lineage(child="neurovista", parent="scitex-cv", db_path=db_path)
    record_comms_policy(
        name="neurovista",
        group_name="researcher",
        may_spawn=False,
    )
    # Act
    _allowed, reason = spawn_allowed(caller="neurovista", db_path=db_path)
    # Assert
    assert reason is not None and "may_spawn=false" in reason


# ---------------------------------------------------------------------------
# spawn_allowed — group-scoped child allowance (operator 2026-07-06 ACL
# incident): a developer- OR research-group child may spawn / restart a peer
# to self-heal a DOWN agent, without waiting on the operator.
# ---------------------------------------------------------------------------


def test_spawn_allowed_allows_developer_group_child(pg_schema: str, db_path: Path) -> None:
    """A child in the developer group may spawn (group short-circuit)."""
    # Arrange
    record_lineage(child="worker-dev", parent="root", db_path=db_path)
    record_comms_policy(name="worker-dev", group_name="developer")
    # Act
    allowed, _reason = spawn_allowed(caller="worker-dev", db_path=db_path)
    # Assert
    assert allowed is True


def test_spawn_allowed_allows_research_group_child(pg_schema: str, db_path: Path) -> None:
    """A child in the researcher group may spawn (the incident's case)."""
    # Arrange
    record_lineage(child="neurovista", parent="scitex-cv", db_path=db_path)
    record_comms_policy(name="neurovista", group_name="researcher")
    # Act
    allowed, _reason = spawn_allowed(caller="neurovista", db_path=db_path)
    # Assert
    assert allowed is True


def test_spawn_allowed_denies_child_in_neither_group(pg_schema: str, db_path: Path) -> None:
    """A child in NEITHER the developer nor research group stays denied."""
    # Arrange
    record_lineage(child="worker-gen", parent="root", db_path=db_path)
    record_comms_policy(name="worker-gen", group_name="generalist")
    # Act
    allowed, _reason = spawn_allowed(caller="worker-gen", db_path=db_path)
    # Assert
    assert allowed is False


def test_spawn_allowed_deny_reason_names_group_policy(pg_schema: str, db_path: Path) -> None:
    """The neither-group deny reason states the group-scoped policy.

    Spelled ``researcher`` in full. The old text said "research", and a
    reader reasonably guessed a "research" vs "researcher" string
    mismatch was the bug — it was not, and the wrong hypothesis cost
    time (2026-08-10).
    """
    # Arrange
    record_lineage(child="worker-gen", parent="root", db_path=db_path)
    record_comms_policy(name="worker-gen", group_name="generalist")
    # Act
    _allowed, reason = spawn_allowed(caller="worker-gen", db_path=db_path)
    # Assert
    assert reason is not None and "developer, researcher, privileged" in reason


def test_spawn_deny_reason_points_at_refresh_acl_for_a_stale_row(
    pg_schema: str,
    db_path: Path,
) -> None:
    """A denial whose group list disagrees with the spec means a STALE
    row; the message must name the command that re-publishes it."""
    # Arrange
    record_lineage(child="worker-gen", parent="root", db_path=db_path)
    record_comms_policy(name="worker-gen", group_name="generalist")
    # Act
    _allowed, reason = spawn_allowed(caller="worker-gen", db_path=db_path)
    # Assert
    assert "refresh-acl" in reason


def test_spawn_allowed_developer_group_child_still_respects_may_spawn(
    pg_schema: str,
    db_path: Path,
) -> None:
    """The per-spec may_spawn=false deny survives the group short-circuit."""
    # Arrange
    record_lineage(child="worker-dev", parent="root", db_path=db_path)
    record_comms_policy(
        name="worker-dev",
        group_name="developer",
        may_spawn=False,
    )
    # Act
    allowed, _reason = spawn_allowed(caller="worker-dev", db_path=db_path)
    # Assert
    assert allowed is False


# ---------------------------------------------------------------------------
# grant_send / has_grant / revoke_send — cross-group ACL grants
#
# THE BEHAVIOUR TESTS MOVED, they were not dropped. comms_grants is on
# PostgreSQL now, and every grant test that used to live here is asserted
# store-natively in test_state_db_grants.py: has_grant false when none /
# grant makes it true / directional / note round-trip / idempotent / revoke
# removes, denies and returns False / the listing pairs. Keeping copies here
# meant every future grants change had to be edited in two places, and the
# copies here were the weaker pair — their arrange step wrote through
# ``state_db.open_db``, i.e. into the abandoned SQLite table, which is why
# they read back empty rather than failing loudly.
#
# WHAT STAYS IS THE ONE THING THIS FILE UNIQUELY COVERS: the four primitives
# are RE-EXPORTED from state_db_nodes, and production imports them from HERE
# (cli_pkg/a2a_group.py, _lifecycle/_instances.py, _listen/_acl.py). Deleting
# the whole block would silently drop that coverage, and a broken re-export
# would surface as an ImportError in production rather than in this suite.
# ---------------------------------------------------------------------------


def test_the_grant_primitives_are_importable_from_state_db_nodes() -> None:
    """The re-export is load-bearing: production imports them from here.

    Deliberately NOT a behaviour test — those live in test_state_db_grants.py.
    This asserts only the import surface that state_db_grants.py's own
    docstring promises to keep stable.
    """
    # Arrange
    from scitex_agent_container._state import state_db_nodes
    # Act
    missing = [
        n for n in ("grant_send", "revoke_send", "has_grant", "list_comms_grants")
        if not callable(getattr(state_db_nodes, n, None))
    ]
    # Assert
    assert missing == []


# ---------------------------------------------------------------------------
# node_tokens — authenticated identity primitive (handoff §4 acceptance)
# ---------------------------------------------------------------------------


def test_node_tokens_table_exists(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='node_tokens'"
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
