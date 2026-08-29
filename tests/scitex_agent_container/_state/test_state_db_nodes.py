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
acceptance criterion was DEFERRED (lead 2026-05-20), later implemented
as per-node bearer tokens, and REMOVED on 2026-08-28. A ``node_tokens``
section at the end of this file covered ``mint_node_token`` /
``resolve_node_token`` / ``list_node_tokens`` and the table's existence;
it went with them. Those functions had no callers outside tests, the
table held 0 rows on every fleet host, and the tests were therefore the
only thing that ever exercised the round trip they asserted. Identity
is once more the self-claimed ``metadata.from_agent``, which is what the
grant tests above already assume.

No mocks (handoff §0): real SQLite under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_lineage_store import read_edges
from scitex_agent_container._state.state_db_nodes import (
    derive_group,
    record_comms_policy,
    record_lineage,
    spawn_allowed,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # Arrange
    p = tmp_path / "state.db"
    return p


# ---------------------------------------------------------------------------
# Schema — lineage + comms_grants tables exist
# ---------------------------------------------------------------------------


def test_lineage_table_is_absent_from_a_fresh_state_db(db_path: Path) -> None:
    """INVERTED on 2026-08-28, and kept rather than deleted.

    This asserted the SQLite ``lineage`` table EXISTS. The spawn DAG moved
    to the shared PostgreSQL store and its DDL is gone, so the honest
    version of the same question is the opposite one — and it is worth
    asking, because an empty leftover would be worse here than a crash:
    every reader treats "no row for this child" as ROOT, and a root MAY
    SPAWN. A stray ``CREATE TABLE lineage`` sneaking back into the schema
    would hand the whole fleet spawn authority, silently. This test is what
    would notice.
    """
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lineage'"
        ).fetchall()
    # Assert
    assert rows == []


# ``test_comms_grants_table_exists`` and ``test_comms_grants_has_column``
# were here until 2026-08-28. They asked ``sqlite_master`` and
# ``PRAGMA table_info`` whether the SQLite ``comms_grants`` table and its four
# columns existed -- and this commit deletes that DDL, because every reader
# had already moved to the shared PostgreSQL store. A DDL-presence test
# outlives its table by exactly one commit; keeping them would mean either a
# permanently red suite or restoring a table nothing reads.
#
# Nothing is lost. They asserted the SHAPE of storage, never a behaviour:
# what a grant MEANS is covered by test_state_db_grants.py against the store
# the code actually uses.


# ---------------------------------------------------------------------------
# record_lineage — parent → child edges
# ---------------------------------------------------------------------------


def test_record_lineage_persists_parent_pointer(pg_schema: str) -> None:
    # Arrange
    record_lineage(child="bob", parent="alice")
    # Act — read back through the production index, not raw SQL.
    parent = read_edges().parent("bob")
    # Assert
    assert parent == "alice"


def test_record_lineage_idempotent_no_duplicate_rows(pg_schema: str) -> None:
    """Re-recording the same edge does not duplicate the record.

    ``child_name`` is the store IDENTITY, so a duplicate is not merely
    unlikely — it is unrepresentable. Kept anyway: the test now pins that
    the SECOND call still leaves exactly one live edge for ``bob``, which
    is the property the caller depends on however it is enforced.
    """
    # Arrange
    record_lineage(child="bob", parent="alice")
    record_lineage(child="bob", parent="alice")
    # Act
    bobs = [child for child in read_edges().parent_of if child == "bob"]
    # Assert
    assert len(bobs) == 1


def test_record_lineage_re_parent_keeps_existing_parent(pg_schema: str) -> None:
    """A re-parent attempt keeps the original parent (no raise, no switch).

    A restart of an existing agent by a different-lineage caller must not
    be blocked and must not re-parent — the original parent is kept, so
    identity drift stays impossible while restarts succeed. (No raise is
    implicit: a raising record_lineage would error this test.)
    """
    # Arrange
    record_lineage(child="bob", parent="alice")
    # Act — a different parent must NOT raise; it keeps "alice"
    record_lineage(child="bob", parent="other-root")
    # Assert — original parent kept, not switched to the new caller
    assert read_edges().parent("bob") == "alice"


# ---------------------------------------------------------------------------
# derive_group — the heart of the default ACL check
# ---------------------------------------------------------------------------


def test_derive_group_of_root_with_no_children_is_self_only(pg_schema: str, db_path: Path) -> None:
    # Arrange
    name = "root"
    # Act
    group = derive_group(name=name)
    # Assert
    assert group == {"root"}


def test_derive_group_of_parent_includes_direct_children(pg_schema: str, db_path: Path) -> None:
    # Arrange
    record_lineage(child="worker-a", parent="root")
    record_lineage(child="worker-b", parent="root")
    # Act
    group = derive_group(name="root")
    # Assert
    assert group == {"root", "worker-a", "worker-b"}


def test_derive_group_of_child_includes_parent_and_siblings(pg_schema: str, db_path: Path) -> None:
    """Sibling sees the same group as the parent does — bidirectional."""
    # Arrange
    record_lineage(child="worker-a", parent="root")
    record_lineage(child="worker-b", parent="root")
    # Act
    group = derive_group(name="worker-a")
    # Assert
    assert group == {"root", "worker-a", "worker-b"}


def test_derive_group_excludes_cross_group_nodes(pg_schema: str, db_path: Path) -> None:
    """A different root's children are not in this group."""
    # Arrange — two unrelated families
    record_lineage(child="child-1", parent="root-1")
    record_lineage(child="child-2", parent="root-2")
    # Act
    group = derive_group(name="child-1")
    # Assert
    assert group == {"root-1", "child-1"}


def test_derive_group_of_unknown_node_is_singleton(pg_schema: str, db_path: Path) -> None:
    """A fresh, unattached node is its own singleton group."""
    # Arrange
    name = "fresh"
    # Act
    group = derive_group(name=name)
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
    record_lineage(child="cap-a", parent="root")
    record_lineage(child="cap-b", parent="root")
    record_comms_policy(name="cap-a", lineage_group="solitary")
    # Act
    group = derive_group(name="cap-a")
    # Assert
    assert group == {"cap-a"}


def test_derive_group_without_a_policy_keeps_the_legacy_siblings(
    pg_schema: str, db_path: Path
) -> None:
    """Default-preservation: with no policy record, derive_group keeps the
    legacy parent + direct-children semantics."""
    # Arrange
    record_lineage(child="cap-a", parent="root")
    record_lineage(child="cap-b", parent="root")
    # Act
    group = derive_group(name="cap-a")
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
    allowed, _reason = spawn_allowed(caller=caller)
    # Assert
    assert allowed is True


def test_spawn_allowed_returns_true_for_root_node(pg_schema: str, db_path: Path) -> None:
    """A node with no parent → root → allowed."""
    # Arrange
    caller = "root"
    # Act
    allowed, _reason = spawn_allowed(caller=caller)
    # Assert
    assert allowed is True


def test_spawn_allowed_returns_false_for_child_node(pg_schema: str, db_path: Path) -> None:
    """A node with a parent → child → denied under current policy."""
    # Arrange
    record_lineage(child="worker-a", parent="root")
    # Act
    allowed, _reason = spawn_allowed(caller="worker-a")
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
    record_lineage(child="worker-a", parent="root")
    # Act
    _allowed, reason = spawn_allowed(caller="worker-a")
    # Assert
    assert reason is not None and "developer, researcher, privileged" in reason


def test_spawn_deny_reason_for_unregistered_caller_says_it_has_no_row(
    pg_schema: str,
    db_path: Path,
) -> None:
    """No policy row and "registered but ungrouped" both resolve to an
    empty group set, and they are DIFFERENT facts (2026-08-09)."""
    # Arrange — a lineage edge but no node_comms_policy row.
    record_lineage(child="worker-a", parent="root")
    # Act
    _allowed, reason = spawn_allowed(caller="worker-a")
    # Assert
    assert "NO node_comms_policy row" in reason


def test_spawn_allowed_returns_true_for_developer_group_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """A developer-group child may spawn even though it has a parent."""
    # Arrange
    record_lineage(child="worker-a", parent="root")
    record_comms_policy(name="worker-a", group_name="developer")
    # Act
    allowed, _reason = spawn_allowed(caller="worker-a")
    # Assert
    assert allowed is True


def test_spawn_allowed_returns_true_for_researcher_group_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """A researcher-group child may spawn even though it has a parent."""
    # Arrange
    record_lineage(child="neurovista", parent="scitex-cv")
    record_comms_policy(name="neurovista", group_name="researcher")
    # Act
    allowed, _reason = spawn_allowed(caller="neurovista")
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
    record_lineage(child="dotfiles", parent="root")
    record_comms_policy(name="dotfiles", group_name="privileged")
    # Act
    allowed, _reason = spawn_allowed(caller="dotfiles")
    # Assert
    assert allowed is True


def test_spawn_allowed_returns_false_for_non_dev_research_group_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """A child in an unrelated named group is still denied."""
    # Arrange
    record_lineage(child="worker-a", parent="root")
    record_comms_policy(name="worker-a", group_name="analysts")
    # Act
    allowed, _reason = spawn_allowed(caller="worker-a")
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
    record_lineage(child="worker-a", parent="root")
    record_comms_policy(name="worker-a", group_name="analysts")
    # Act
    _allowed, reason = spawn_allowed(caller="worker-a")
    # Assert
    assert reason is not None and "['analysts']" in reason


def test_spawn_allowed_may_spawn_false_still_denies_developer_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """Per-spec may_spawn=false overrides the developer-group allow."""
    # Arrange
    record_lineage(child="worker-a", parent="root")
    record_comms_policy(
        name="worker-a",
        group_name="developer",
        may_spawn=False,
    )
    # Act
    allowed, _reason = spawn_allowed(caller="worker-a")
    # Assert
    assert allowed is False


def test_spawn_allowed_may_spawn_false_reason_for_developer_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """The deny reason names the per-spec may_spawn=false override."""
    # Arrange
    record_lineage(child="worker-a", parent="root")
    record_comms_policy(
        name="worker-a",
        group_name="developer",
        may_spawn=False,
    )
    # Act
    _allowed, reason = spawn_allowed(caller="worker-a")
    # Assert
    assert reason is not None and "may_spawn=false" in reason


def test_spawn_allowed_may_spawn_false_still_denies_researcher_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """Per-spec may_spawn=false overrides the researcher-group allow."""
    # Arrange
    record_lineage(child="neurovista", parent="scitex-cv")
    record_comms_policy(
        name="neurovista",
        group_name="researcher",
        may_spawn=False,
    )
    # Act
    allowed, _reason = spawn_allowed(caller="neurovista")
    # Assert
    assert allowed is False


def test_spawn_allowed_may_spawn_false_reason_for_researcher_child(
    pg_schema: str,
    db_path: Path,
) -> None:
    """The deny reason names the per-spec may_spawn=false override."""
    # Arrange
    record_lineage(child="neurovista", parent="scitex-cv")
    record_comms_policy(
        name="neurovista",
        group_name="researcher",
        may_spawn=False,
    )
    # Act
    _allowed, reason = spawn_allowed(caller="neurovista")
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
    record_lineage(child="worker-dev", parent="root")
    record_comms_policy(name="worker-dev", group_name="developer")
    # Act
    allowed, _reason = spawn_allowed(caller="worker-dev")
    # Assert
    assert allowed is True


def test_spawn_allowed_allows_research_group_child(pg_schema: str, db_path: Path) -> None:
    """A child in the researcher group may spawn (the incident's case)."""
    # Arrange
    record_lineage(child="neurovista", parent="scitex-cv")
    record_comms_policy(name="neurovista", group_name="researcher")
    # Act
    allowed, _reason = spawn_allowed(caller="neurovista")
    # Assert
    assert allowed is True


def test_spawn_allowed_denies_child_in_neither_group(pg_schema: str, db_path: Path) -> None:
    """A child in NEITHER the developer nor research group stays denied."""
    # Arrange
    record_lineage(child="worker-gen", parent="root")
    record_comms_policy(name="worker-gen", group_name="generalist")
    # Act
    allowed, _reason = spawn_allowed(caller="worker-gen")
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
    record_lineage(child="worker-gen", parent="root")
    record_comms_policy(name="worker-gen", group_name="generalist")
    # Act
    _allowed, reason = spawn_allowed(caller="worker-gen")
    # Assert
    assert reason is not None and "developer, researcher, privileged" in reason


def test_spawn_deny_reason_points_at_refresh_acl_for_a_stale_row(
    pg_schema: str,
    db_path: Path,
) -> None:
    """A denial whose group list disagrees with the spec means a STALE
    row; the message must name the command that re-publishes it."""
    # Arrange
    record_lineage(child="worker-gen", parent="root")
    record_comms_policy(name="worker-gen", group_name="generalist")
    # Act
    _allowed, reason = spawn_allowed(caller="worker-gen")
    # Assert
    assert "refresh-acl" in reason


def test_spawn_allowed_developer_group_child_still_respects_may_spawn(
    pg_schema: str,
    db_path: Path,
) -> None:
    """The per-spec may_spawn=false deny survives the group short-circuit."""
    # Arrange
    record_lineage(child="worker-dev", parent="root")
    record_comms_policy(
        name="worker-dev",
        group_name="developer",
        may_spawn=False,
    )
    # Act
    allowed, _reason = spawn_allowed(caller="worker-dev")
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

