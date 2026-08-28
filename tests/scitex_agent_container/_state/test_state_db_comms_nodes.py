"""ADR-0014 — the ``comms_nodes`` directory on PostgreSQL, and the resolver.

Covers :mod:`scitex_agent_container._state.state_db_comms_nodes` and the
``resolve_node_host`` / ``is_local_node`` extensions in
:mod:`scitex_agent_container._state.state_db_nodes`.

WHAT CHANGED WHEN THE TABLE MOVED, AND WHAT THESE TESTS NOW ASSERT
==================================================================
This file used to open a SQLite file under ``tmp_path`` and read
``sqlite_master`` / ``PRAGMA table_info`` to prove the seven columns
existed. Those tests are GONE, not ported: the store owns its own DDL from
a declared schema, so "does the column exist" is a question about
scitex-dev's dialect rather than about sac, and asserting it here would test
someone else's code through a keyhole. What replaced them is the pair of
claims that are actually sac's:

  * ``test_comms_nodes_is_gone_from_a_fresh_state_db`` — a state.db created
    by sac's own ``init_schema`` has NO ``comms_nodes`` table. That is the
    real regression risk of a half-finished migration: a leftover empty
    table answers the routing resolver with "that agent is not registered"
    instead of raising.
  * ``test_the_declared_schema_has_exactly_the_four_fields`` — the store is
    opened with the four fields ``_store_plugin`` declared, so the three
    dropped columns cannot creep back as hand-rolled copies of things the
    primitive already maintains.

PER-HOST ISOLATION WAS A PROPERTY OF THE OLD TESTS, NOT OF THE FEATURE. The
sibling file ``test_state_db_export_comms_nodes_sync.py`` opened TWO SQLite
files and asserted a row written to A was invisible in B until a sync ran.
It is deleted, because that separation is precisely what this migration
removes: one shared directory is the point, and a test asserting the
opposite would be pinning the bug.

Needs a real PostgreSQL: ``pg_schema`` is the shared opt-in fixture, which
skips where no cluster exists and FAILS where a configured one is broken.

NO MONKEYPATCH (PA-306 §3): the module is exercised through its real public
surface, and isolation comes from the fixture pointing ``SCITEX_STORE_DSN``
at a throwaway schema.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    CommsNodeConflictError,
    is_local_node,
    list_comms_nodes,
    lookup_comms_node,
    register_comms_node,
    rename_comms_node,
    resolve_node_host,
    unregister_comms_node,
)

#: The name the store stamps into ``_origin`` for writes made here. The
#: conflict check compares a caller's declared source against it, so a test
#: that wants the "another host claims this name" branch must pass something
#: OTHER than this.
THIS_NODE = socket.gethostname()

#: A source host that CANNOT be this one, derived rather than written down.
#: A literal here can COINCIDE with the runner's own hostname, and a control
#: that can equal the thing it controls against is not a control — see the
#: sibling tombstone file, where the literal "scitex-compute-04" made the
#: cross-host test pass on one runner and fail on another, same commit.
FOREIGN_HOST = f"not-{THIS_NODE}"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A real state.db — still needed for the ``instances`` half of the resolver.

    ``resolve_node_host`` reads ``instances`` from SQLite and falls through to
    the PostgreSQL directory, so the resolver tests below need both stores.
    """
    # Arrange
    p = tmp_path / "state.db"
    state_db.init_schema(p)
    return p


# ---------------------------------------------------------------------------
# the departure — comms_nodes is not a SQLite table any more
# ---------------------------------------------------------------------------


def test_comms_nodes_is_gone_from_a_fresh_state_db(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='comms_nodes'"
        ).fetchall()
    # Assert — an empty leftover table would answer the routing resolver
    # "not registered" instead of raising, which is worse than no table.
    assert rows == []


def test_known_tables_no_longer_offers_comms_nodes() -> None:
    # Arrange
    from scitex_agent_container._state.state_db import KNOWN_TABLES

    # Act
    contains = "comms_nodes" in KNOWN_TABLES
    # Assert
    assert contains is False


def test_the_declared_schema_has_exactly_the_four_fields() -> None:
    # Arrange
    from scitex_agent_container._state.state_db_comms_nodes_store import (
        comms_nodes_schema,
    )

    # Act
    fields = set(comms_nodes_schema().fields)
    # Assert — ended_at / source_host / updated_at are the primitive's job.
    assert fields == {"name", "host", "a2a_port", "registered_at"}


# ---------------------------------------------------------------------------
# register_comms_node — fresh insert + idempotent re-register
# ---------------------------------------------------------------------------


def test_register_comms_node_inserts_a_record(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    info = lookup_comms_node(name="lead")
    # Assert
    assert info is not None


def test_register_comms_node_stores_host(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    info = lookup_comms_node(name="lead")
    # Assert
    assert info["host"] == "mba"


def test_register_comms_node_stores_port(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    info = lookup_comms_node(name="lead")
    # Assert
    assert info["a2a_port"] == 8642


def test_source_host_reported_is_the_stores_origin_not_the_argument(
    pg_schema: str,
) -> None:
    # Arrange — the caller declares a source on another host's behalf.
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, source_host="spartan"
    )
    # Act
    info = lookup_comms_node(name="lead")
    # Assert — provenance is now ``_origin``, stamped by the primitive from
    # the WRITING node. The argument annotates the claim; it is not stored.
    assert info["source_host"] == THIS_NODE


def test_re_registering_the_same_target_advances_updated_at(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    first = lookup_comms_node(name="lead")
    # Act
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    second = lookup_comms_node(name="lead")
    # Assert — ``updated_at`` is the HLC now; every op restamps it.
    assert second["updated_at"] >= first["updated_at"]


def test_re_registering_does_not_restamp_registered_at(pg_schema: str) -> None:
    # Arrange — ``registered_at`` is IMMUTABLE, so a refresh that rewrote it
    # would report a MergeConflict on ordinary lifecycle and drown the one
    # collision the field exists to surface.
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    first = lookup_comms_node(name="lead")
    # Act
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    second = lookup_comms_node(name="lead")
    # Assert
    assert second["registered_at"] == first["registered_at"]


def test_same_source_different_target_raises_without_replace(pg_schema: str) -> None:
    # Arrange — PR L1 (operator directive 12847): same-origin same-name with
    # a DIFFERENT (host, a2a_port) must NOT silently overwrite.
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    raised: BaseException | None = None
    # Act
    try:
        register_comms_node(name="lead", host="mba", a2a_port=9000)
    except CommsNodeConflictError as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; the function is contracted to raise on same-source different-target without replace=True.)
        raised = exc
    # Assert
    assert isinstance(raised, CommsNodeConflictError)


def test_same_source_different_target_overwrites_with_replace(pg_schema: str) -> None:
    # Arrange — opt-in via replace=True (wired by the --prefer flag).
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    register_comms_node(name="lead", host="mba", a2a_port=9000, replace=True)
    info = lookup_comms_node(name="lead")
    # Assert
    assert info["a2a_port"] == 9000


def test_conflict_message_names_the_incoming_kind(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    raised: BaseException | None = None
    # Act
    try:
        register_comms_node(name="lead", host="mba", a2a_port=9000, kind="self-peer")
    except CommsNodeConflictError as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; the only thing the assert checks is that the message contains the kind.)
        raised = exc
    # Assert
    assert raised is not None and "self-peer" in str(raised)


def test_conflict_message_names_the_incoming_source_path(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    incoming_path = "/home/agent/proj/foo/agents/lead/spec.yaml"
    raised: BaseException | None = None
    # Act
    try:
        register_comms_node(
            name="lead",
            host="mba",
            a2a_port=9000,
            kind="self-peer",
            source_path=incoming_path,
        )
    except (
        CommsNodeConflictError
    ) as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert.)
        raised = exc
    # Assert
    assert raised is not None and incoming_path in str(raised)


def test_conflict_message_names_the_existing_target(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    raised: BaseException | None = None
    # Act
    try:
        register_comms_node(name="lead", host="mba", a2a_port=9000)
    except (
        CommsNodeConflictError
    ) as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert.)
        raised = exc
    # Assert
    assert raised is not None and "8642" in str(raised)


def test_conflict_message_mentions_the_prefer_flag_hint(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    raised: BaseException | None = None
    # Act
    try:
        register_comms_node(name="lead", host="mba", a2a_port=9000, kind="self-peer")
    except (
        CommsNodeConflictError
    ) as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert.)
        raised = exc
    # Assert
    assert raised is not None and "--prefer" in str(raised)


def test_a_refused_write_leaves_the_record_unchanged(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    raised: BaseException | None = None
    # Act
    try:
        register_comms_node(name="lead", host="mba", a2a_port=9000)
    except CommsNodeConflictError as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; assert is that the record stayed unchanged.)
        raised = exc
    info = lookup_comms_node(name="lead")
    # Assert
    assert raised is not None and info["a2a_port"] == 8642


def test_the_pre_l1_call_surface_still_inserts(pg_schema: str) -> None:
    # Arrange — no kwargs beyond the pre-L1 surface; an INSERT must succeed.
    # Act
    register_comms_node(name="alpha", host="mba", a2a_port=12345, source_host=None)
    info = lookup_comms_node(name="alpha")
    # Assert
    assert info is not None


def test_another_host_claiming_the_name_raises(pg_schema: str) -> None:
    # Arrange — written here, so the record's origin is THIS_NODE.
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    # Assert — a declared source that is not this record's origin is the
    # ADR-0014 name-uniqueness collision, and it always raises.
    with pytest.raises(CommsNodeConflictError):
        register_comms_node(
            name="lead",
            host=FOREIGN_HOST,
            a2a_port=8642,
            source_host=FOREIGN_HOST,
        )


def test_register_comms_node_rejects_empty_name(pg_schema: str) -> None:
    # Arrange
    port = 8642
    # Act
    # Assert
    with pytest.raises(ValueError):
        register_comms_node(name="", host="mba", a2a_port=port)


def test_register_comms_node_rejects_zero_port(pg_schema: str) -> None:
    # Arrange
    name = "lead"
    # Act
    # Assert
    with pytest.raises(ValueError):
        register_comms_node(name=name, host="mba", a2a_port=0)


# ---------------------------------------------------------------------------
# unregister — hide(), not DELETE
# ---------------------------------------------------------------------------


def test_unregister_reports_true_when_a_live_entry_was_withdrawn(
    pg_schema: str,
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    removed = unregister_comms_node(name="lead")
    # Assert
    assert removed is True


def test_a_withdrawn_entry_is_invisible_to_lookup(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    unregister_comms_node(name="lead")
    # Act
    after = lookup_comms_node(name="lead")
    # Assert
    assert after is None


def test_unregister_reports_false_the_second_time(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    unregister_comms_node(name="lead")
    # Act
    second = unregister_comms_node(name="lead")
    # Assert
    assert second is False


def test_unregister_no_longer_forgets(pg_schema: str) -> None:
    # Arrange — under DELETE, "never registered" and "registered then
    # stopped" were the same answer. hide() keeps them apart.
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    unregister_comms_node(name="lead")
    # Act
    withdrawn = [r["name"] for r in list_comms_nodes(include_ended=True)]
    # Assert
    assert withdrawn == ["lead"]


def test_re_registering_revives_a_withdrawn_entry(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    unregister_comms_node(name="lead")
    # Act
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    info = lookup_comms_node(name="lead")
    # Assert
    assert info["ended_at"] is None


def test_list_comms_nodes_omits_withdrawn_entries_by_default(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="alpha", host="h1", a2a_port=7000)
    register_comms_node(name="beta", host="h2", a2a_port=7001)
    unregister_comms_node(name="beta")
    # Act
    rows = list_comms_nodes()
    # Assert
    assert [r["name"] for r in rows] == ["alpha"]


def test_list_comms_nodes_include_ended_returns_all(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="alpha", host="h1", a2a_port=7000)
    register_comms_node(name="beta", host="h2", a2a_port=7001)
    unregister_comms_node(name="beta")
    # Act
    rows = list_comms_nodes(include_ended=True)
    # Assert
    assert [r["name"] for r in rows] == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# rename_comms_node — the step that used to be a NAME_COLUMNS pair
# ---------------------------------------------------------------------------


def test_rename_moves_the_routing_tuple_onto_the_new_name(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="old", host="mba", a2a_port=8642)
    # Act
    rename_comms_node(old="old", new="new")
    # Assert
    assert lookup_comms_node(name="new")["a2a_port"] == 8642


def test_rename_withdraws_the_old_name(pg_schema: str) -> None:
    # Arrange — a live entry under a name no agent answers to is a routing
    # target nobody owns.
    register_comms_node(name="old", host="mba", a2a_port=8642)
    # Act
    rename_comms_node(old="old", new="new")
    # Assert
    assert lookup_comms_node(name="old") is None


def test_rename_carries_registered_at_forward(pg_schema: str) -> None:
    # Arrange — a renamed agent is the SAME agent; its join time is a fact
    # about the graph, not about the rename.
    register_comms_node(name="old", host="mba", a2a_port=8642)
    before = lookup_comms_node(name="old")["registered_at"]
    # Act
    rename_comms_node(old="old", new="new")
    # Assert
    assert lookup_comms_node(name="new")["registered_at"] == before


def test_rename_refuses_to_overwrite_a_live_occupant(pg_schema: str) -> None:
    # Arrange — two DIFFERENT agents, both live. The SQLite path refused this
    # via the ``name`` PRIMARY KEY (IntegrityError -> DbRenameError); the
    # store has no such constraint, so the refusal has to be explicit.
    register_comms_node(name="old", host="mba", a2a_port=8642)
    register_comms_node(name="taken", host="other-host", a2a_port=9100)
    # Act
    # Assert — silently repointing "taken" at old's address would leave the
    # real "taken" agent unreachable, with nothing logged anywhere.
    with pytest.raises(CommsNodeConflictError):
        rename_comms_node(old="old", new="taken")


def test_a_refused_rename_leaves_the_occupant_untouched(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="old", host="mba", a2a_port=8642)
    register_comms_node(name="taken", host="other-host", a2a_port=9100)
    raised: BaseException | None = None
    # Act
    try:
        rename_comms_node(old="old", new="taken")
    except CommsNodeConflictError as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; the assert is that the victim's routing entry survived.)
        raised = exc
    info = lookup_comms_node(name="taken")
    # Assert
    assert raised is not None and info["a2a_port"] == 9100


def test_a_refused_rename_leaves_the_source_live(pg_schema: str) -> None:
    # Arrange — the refusal must not half-apply: withdrawing ``old`` before
    # discovering the collision would strand the agent being renamed.
    register_comms_node(name="old", host="mba", a2a_port=8642)
    register_comms_node(name="taken", host="other-host", a2a_port=9100)
    raised: BaseException | None = None
    # Act
    try:
        rename_comms_node(old="old", new="taken")
    except CommsNodeConflictError as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; the assert is that the source survived.)
        raised = exc
    # Assert
    assert raised is not None and lookup_comms_node(name="old") is not None


def test_rename_takes_over_a_withdrawn_target_name(pg_schema: str) -> None:
    # Arrange — renaming BACK is the documented inverse, and it necessarily
    # targets a name the forward rename withdrew. Refusing here would make
    # the inverse impossible; a withdrawn entry is not a live claim.
    register_comms_node(name="old", host="mba", a2a_port=8642)
    rename_comms_node(old="old", new="new")
    # Act
    rename_comms_node(old="new", new="old")
    # Assert
    assert lookup_comms_node(name="old")["a2a_port"] == 8642


def test_rename_reports_false_when_nothing_lives_under_the_old_name(
    pg_schema: str,
) -> None:
    # Arrange
    register_comms_node(name="other", host="mba", a2a_port=8642)
    # Act
    moved = rename_comms_node(old="ghost", new="new")
    # Assert
    assert moved is False


def test_rename_is_its_own_inverse(pg_schema: str) -> None:
    # Arrange — the undo the rename flow pushes is this verb with the
    # arguments swapped.
    register_comms_node(name="old", host="mba", a2a_port=8642)
    rename_comms_node(old="old", new="new")
    # Act
    rename_comms_node(old="new", new="old")
    # Assert
    assert lookup_comms_node(name="old")["a2a_port"] == 8642


# ---------------------------------------------------------------------------
# resolve_node_host — fallback to the directory after instances misses
# ---------------------------------------------------------------------------


def test_resolve_node_host_returns_none_for_unknown_name(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange
    target_db = db_path
    # Act
    info = resolve_node_host(name="ghost", db_path=target_db)
    # Assert
    assert info is None


def test_resolve_node_host_finds_the_directory_when_no_instance(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    info = resolve_node_host(name="lead", db_path=db_path)
    # Assert
    assert info == {"host": "mba", "a2a_port": 8642}


def test_resolve_node_host_prefers_instances_when_both_answer(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange
    import time as _time

    with state_db.open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO instances (id, name, host, scope, a2a_port, "
            "started_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "inst-1",
                "lead",
                "instances-host",
                "global",
                9000,
                _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
            ),
        )
    register_comms_node(name="lead", host="comms-host", a2a_port=8642)
    # Act
    info = resolve_node_host(name="lead", db_path=db_path)
    # Assert
    assert info["host"] == "instances-host"


def test_resolve_node_host_skips_a_withdrawn_directory_entry(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    unregister_comms_node(name="lead")
    # Act
    info = resolve_node_host(name="lead", db_path=db_path)
    # Assert
    assert info is None


# ---------------------------------------------------------------------------
# is_local_node — the directory correctly identifies cross-host targets
# ---------------------------------------------------------------------------


def test_is_local_node_true_when_the_entry_names_the_local_host(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    local = is_local_node(name="lead", local_host="mba", db_path=db_path)
    # Assert
    assert local


def test_is_local_node_false_when_the_entry_lives_elsewhere(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    local = is_local_node(name="lead", local_host="spartan", db_path=db_path)
    # Assert
    assert not local


def test_is_local_node_true_for_an_unknown_name(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange
    target_db = db_path
    # Act
    local = is_local_node(name="ghost", local_host="mba", db_path=target_db)
    # Assert
    assert local
