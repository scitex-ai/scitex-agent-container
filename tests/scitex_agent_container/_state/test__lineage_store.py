#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The lineage edges on PostgreSQL — and the authority that derives from them.

Lineage is not a log. Three gates read these edges and turn them into
permission, so the tests that matter here are the FLIPS — not "an edge can be
written" but "the gate's answer CHANGES when the edge does". A port that
stored edges perfectly and left the gates reading nothing would pass every
round-trip assertion and be a silent privilege change.

WHICH DIRECTION EACH GATE FAILS IN, because they are not the same:

  * ``spawn_allowed`` — a node with NO parent edge is a ROOT, and roots may
    spawn. So a LOST edge does not deny, it PROMOTES. That asymmetry is why
    ``test_a_node_with_no_parent_edge_may_spawn`` and
    ``test_recording_a_parent_edge_denies_spawn`` are a PAIR here rather
    than an ACL-suite detail: the edge is the only thing standing between a
    child and spawn authority, and the pair is split in two because one
    assertion per test means the failing half names itself.
  * ``check_lineage_acl`` — ``target in descendants_of(caller)``. A lost edge
    DENIES; a parent stops being able to manage its own child.
  * ``derive_group`` — the default-ACL mesh; a lost edge collapses an agent
    to a singleton and intra-group sends start being refused.

TWO PROPERTIES OF THE SQLITE VERSION THAT ARE EASY TO LOSE, each with a test:

  * KEEP-FIRST-PARENT. ``record_lineage`` sets a child's parent once and a
    later, different parent is IGNORED (logged, not raised) so a restart by a
    non-original-parent caller works in place. ``MergeRule.IMMUTABLE`` makes a
    conflicting REMOTE edge loud, which is a different mechanism for a
    different case — it would not keep the local path quiet.
  * THE LISTING ORDER IS CAUSAL, NOT WALL-CLOCK. The predecessor ordered by
    ``rowid`` and its docstring recorded why: ``created_at`` ties on
    bulk-imported peer rows and skews across hosts, so a foreign row sorted
    into a plausible position instead of standing out.
    ``test_listing_order_is_insertion_order_not_created_at`` writes an edge
    whose ``created_at`` is older than one already stored and asserts it
    still lists LAST. Ordering by ``created_at`` fails that test; ordering by
    the HLC passes it.

THERE IS NO RENAME TEST, AND THAT IS NOT AN OVERSIGHT. ``sac agents rename``
used to rewrite these edges in SQLite and can no longer do so — the record
identity is the child name and ``parent_name`` is IMMUTABLE, so neither a
hide-then-insert nor a put can move one. The behaviour that replaced it is
covered where it now lives, in
``test__rename_db.py::test_the_dry_run_reports_the_lineage_edges_it_cannot_rename``.
Writing a green rename test here would have certified a capability this
module does not have.

Needs a real PostgreSQL: ``pg_schema`` is the shared opt-in fixture, which
skips where no cluster exists and FAILS where a configured one is broken.

NO MONKEYPATCH (PA-306 §3): the module is exercised through its real public
surface, and isolation comes from the fixture pointing SCITEX_STORE_DSN at a
throwaway schema.
"""

from __future__ import annotations

from scitex_agent_container._state._lineage import (
    ancestors_to_root,
    children_of,
    descendants_of,
    lineage_edges,
    parent_of,
    record_lineage,
)

# ---------------------------------------------------------------------------
# the write, and the rule that governs a second one
# ---------------------------------------------------------------------------


def test_a_recorded_edge_is_readable(pg_schema: str) -> None:
    # Arrange
    record_lineage(child="kid", parent="mom")
    # Act
    parent = parent_of(child="kid")
    # Assert
    assert parent == "mom"


def test_a_child_with_no_edge_has_no_parent(pg_schema: str) -> None:
    # Arrange
    record_lineage(child="kid", parent="mom")
    # Act
    parent = parent_of(child="stranger")
    # Assert
    assert parent is None


def test_recording_the_same_edge_twice_does_not_move_the_timestamp(
    pg_schema: str,
) -> None:
    # Arrange
    import time

    record_lineage(child="kid", parent="mom")
    first = lineage_edges()[0]["created_at"]
    time.sleep(0.01)
    # Act
    record_lineage(child="kid", parent="mom")
    # Assert
    assert lineage_edges()[0]["created_at"] == first


def test_a_second_parent_for_one_child_is_ignored(pg_schema: str) -> None:
    # Arrange — keep-first-parent. A restart brokered by somebody other
    # than the original parent must work in place, not re-parent.
    record_lineage(child="kid", parent="mom")
    # Act
    record_lineage(child="kid", parent="usurper")
    # Assert
    assert parent_of(child="kid") == "mom"


def test_an_empty_child_is_refused(pg_schema: str) -> None:
    # Arrange
    refused = None
    # Act
    try:
        record_lineage(child="", parent="mom")
    except ValueError as exc:
        refused = exc
    # Assert
    assert refused is not None


# ---------------------------------------------------------------------------
# the flips — the edge IS the permission
# ---------------------------------------------------------------------------


def test_a_node_with_no_parent_edge_may_spawn(pg_schema: str, tmp_path) -> None:
    # Arrange — the BEFORE half of the flip below. Kept as its own test
    # because a second assertion in one function hides behind the first
    # when it fails, and this half is what makes the other meaningful.
    from scitex_agent_container._state.state_db_nodes import spawn_allowed

    record_lineage(child="somebody-else", parent="mom")
    # Act
    allowed, _reason = spawn_allowed(caller="kid", db_path=tmp_path / "state.db")
    # Assert
    assert allowed is True


def test_recording_a_parent_edge_denies_spawn(pg_schema: str, tmp_path) -> None:
    # Arrange — the AFTER half. A lost edge does not deny here, it
    # PROMOTES: without the edge "kid" reads as a root and spawns freely.
    from scitex_agent_container._state.state_db_nodes import spawn_allowed

    record_lineage(child="kid", parent="mom")
    # Act
    allowed, _reason = spawn_allowed(caller="kid", db_path=tmp_path / "state.db")
    # Assert
    assert allowed is False


def test_lineage_scope_is_empty_before_any_edge_is_recorded(pg_schema: str) -> None:
    # Arrange — the BEFORE half of the manage-scope flip.
    record_lineage(child="unrelated", parent="somebody")
    # Act
    scope = descendants_of(name="mom")
    # Assert
    assert scope == set()


def test_a_recorded_edge_puts_the_child_in_the_parents_lineage_scope(
    pg_schema: str,
) -> None:
    # Arrange — the AFTER half.
    record_lineage(child="kid", parent="mom")
    # Act
    scope = descendants_of(name="mom")
    # Assert
    assert scope == {"kid"}


# ---------------------------------------------------------------------------
# single-hop reads
# ---------------------------------------------------------------------------


def test_children_of_returns_every_direct_child(pg_schema: str) -> None:
    # Arrange
    record_lineage(child="alice", parent="root")
    record_lineage(child="bob", parent="root")
    record_lineage(child="ada", parent="alice")
    # Act
    kids = children_of(parent="root")
    # Assert — direct children only; "ada" is a grandchild.
    assert kids == {"alice", "bob"}


def test_children_of_is_empty_for_a_node_nobody_names_as_parent(
    pg_schema: str,
) -> None:
    # Arrange
    record_lineage(child="alice", parent="root")
    # Act
    kids = children_of(parent="alice")
    # Assert
    assert kids == set()


# ---------------------------------------------------------------------------
# the ordering property that once let a foreign row hide
# ---------------------------------------------------------------------------


def test_listing_order_is_insertion_order_not_created_at(pg_schema: str) -> None:
    # Arrange — write an edge carrying an OLDER created_at than one already
    # stored, the way import_state carries a peer's timestamp verbatim.
    from scitex_dev.store import NEW_RECORD

    from scitex_agent_container._state._lineage import _open

    record_lineage(child="local", parent="root")
    store = _open()
    try:
        store.put(
            {
                "child_name": "imported",
                "parent_name": "root",
                "created_at": 1.0,  # far older than the edge above
            },
            expected_revision=NEW_RECORD,
        )
    finally:
        store.close()
    # Act
    order = [e["child"] for e in lineage_edges()]
    # Assert — created_at ordering would put the imported edge FIRST, which
    # is precisely how a foreign row used to hide in a listing like this.
    assert order == ["local", "imported"]


def test_listing_order_ignores_created_at_even_when_two_edges_share_one(
    pg_schema: str,
) -> None:
    # Arrange — written in reverse-alphabetical order so that an
    # alphabetical fallback (which is what a created_at tie used to fall
    # through to) FAILS. created_at is not in the HLC sort key at all.
    from scitex_dev.store import NEW_RECORD

    from scitex_agent_container._state._lineage import _open

    store = _open()
    try:
        for child in ("zeta", "alpha"):
            store.put(
                {
                    "child_name": child,
                    "parent_name": "root",
                    "created_at": 100.0,  # identical, on purpose
                },
                expected_revision=NEW_RECORD,
            )
    finally:
        store.close()
    # Act
    order = [e["child"] for e in lineage_edges()]
    # Assert
    assert order == ["zeta", "alpha"]


# ---------------------------------------------------------------------------
# the walks, on the store
# ---------------------------------------------------------------------------


def test_descendants_walks_transitively(pg_schema: str) -> None:
    # Arrange
    record_lineage(child="alice", parent="root")
    record_lineage(child="ada", parent="alice")
    # Act
    found = descendants_of(name="root")
    # Assert
    assert found == {"alice", "ada"}


def test_ancestors_climb_parent_first_root_last(pg_schema: str) -> None:
    # Arrange
    record_lineage(child="pusher", parent="parent")
    record_lineage(child="parent", parent="lead")
    # Act
    chain = ancestors_to_root(name="pusher")
    # Assert
    assert chain == ["parent", "lead"]
