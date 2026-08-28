"""``comms_nodes`` on PostgreSQL — and an unregister that actually stops
resolving.

This is a ROUTING DIRECTORY, so the tests that matter are the FLIPS: not
"a node can be written" but "after an unregister, the function the
forwarder calls says NOTHING IS THERE". A migration that stored every
node perfectly and forgot to stop resolving a tombstone would read green
on every round-trip assertion and would keep POSTing to a dead port.

WHAT WAS DELETED FROM THIS FILE, AND WHY IT WAS NOT JUST EDITED
===============================================================
Nine tests are GONE rather than adapted, because the migration killed
their premise and each of them would have stayed PERMANENTLY GREEN while
asserting nothing:

* ``test_comms_nodes_table_exists`` and the seven
  ``test_comms_nodes_has_<column>_column`` tests read
  ``PRAGMA table_info(comms_nodes)`` on a freshly ``init_schema``'d
  SQLite file. That CREATE TABLE is still in ``state_db_schema.py`` (this
  PR does not remove it — see the PR notes on the vestigial-DDL residue),
  so all eight would still pass — while the data they claim to describe
  lives in PostgreSQL and no longer touches that table at all. A green
  test whose name asserts a property it can no longer observe is worse
  than a red one, because nothing forces anyone to look at it.
* ``test_known_tables_includes_comms_nodes`` is the same shape one level
  up: ``KNOWN_TABLES`` still lists the name, so it passes, and what it
  was protecting (``sac db export --tables comms_nodes`` moving real
  rows) is exactly what stopped being true.

The property those tests were reaching for — "the fields are declared
and the declaration is the one the code writes" — is now testable
directly against the store ``Schema``, which is the actual source of
truth. ``test_the_schema_declares_*`` below does that.

A WHOLE FILE WENT WITH THEM: ``test_state_db_export_comms_nodes_sync.py``
staged two ``state.db`` files and asserted that host A's registration
reached host B through ``export_state`` / ``import_state``. Every one of
its assertions would now pass for the WRONG REASON — the register writes
the shared store, so B "sees" the node whether or not a single byte
crossed the export. It tested convergence between two SQLite files, and
there are no longer two of anything to converge. The scenario it
protected (a name registered on one host resolves on another) is now a
property of the storage rather than of a sync verb, and asserting it here
would be asserting that PostgreSQL is shared.

The per-path isolation those tests relied on is gone too: there is one
shared store, not a file per ``tmp_path``. Isolation comes from
``pg_schema`` pointing ``SCITEX_STORE_DSN`` at a throwaway schema, which
is better isolation than a temp path was because it exercises the real
resolver.

Needs a real PostgreSQL: ``pg_schema`` is the shared opt-in fixture,
which skips where no cluster exists and FAILS where a configured one is
broken.

NO MONKEYPATCH (PA-306 §3): the module is exercised through its real
public surface.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state.state_db_nodes import (
    CommsNodeConflictError,
    list_comms_nodes,
    lookup_comms_node,
    register_comms_node,
    unregister_comms_node,
)
from scitex_agent_container._state.state_db_comms_nodes import (
    resolve_comms_node_host,
)


# ---------------------------------------------------------------------------
# the schema, read from the declaration the code actually writes through
# ---------------------------------------------------------------------------


def test_the_schema_declares_name_as_the_identity() -> None:
    # Arrange
    from scitex_dev.store import FieldRole

    from scitex_agent_container._state.state_db_comms_nodes import (
        _comms_nodes_schema,
    )

    # Act
    role = _comms_nodes_schema().fields["name"].role
    # Assert
    assert role is FieldRole.IDENTITY


def test_the_schema_declares_registered_at_immutable() -> None:
    # Arrange — it is a historical fact: when this name entered the
    # directory. A merge that could move it rewrites the audit trail.
    from scitex_dev.store import MergeRule

    from scitex_agent_container._state.state_db_comms_nodes import (
        _comms_nodes_schema,
    )

    # Act
    merge = _comms_nodes_schema().fields["registered_at"].merge
    # Assert
    assert merge is MergeRule.IMMUTABLE


def test_the_schema_declares_updated_at_as_max_not_last_writer_wins() -> None:
    # Arrange — the record's own clock. LAST_WRITER_WINS would let a
    # late-arriving stale replica walk it backwards.
    from scitex_dev.store import MergeRule

    from scitex_agent_container._state.state_db_comms_nodes import (
        _comms_nodes_schema,
    )

    # Act
    merge = _comms_nodes_schema().fields["updated_at"].merge
    # Assert
    assert merge is MergeRule.MAX


def test_the_store_is_multi_writer() -> None:
    # Arrange — `_dispatch.py` registers a record from the DISPATCHING
    # host about a placement on ANOTHER host; SINGLE_WRITER would make
    # that ordinary cross-host spawn an illegal write. Read from the
    # declaration, NOT from an opened Store: `Store.__init__` connects,
    # so asserting `store.writer_policy` would make this pure-decision
    # test SKIP on every host with no PostgreSQL — which is where a
    # wrong policy would be least likely to be noticed.
    from scitex_dev.store import WriterPolicy

    from scitex_agent_container._state.state_db_comms_nodes import (
        writer_policy,
    )

    # Act
    policy = writer_policy()
    # Assert
    assert policy is WriterPolicy.MULTI_WRITER


# ---------------------------------------------------------------------------
# register — fresh insert
# ---------------------------------------------------------------------------


def test_a_registered_node_resolves(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    info = resolve_comms_node_host(name="lead")
    # Assert
    assert info == {"host": "mba", "a2a_port": 8642}


def test_a_registered_node_stores_its_host(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    row = lookup_comms_node(name="lead")
    # Assert
    assert row is not None and row["host"] == "mba"


def test_a_registered_node_stores_its_port(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    row = lookup_comms_node(name="lead")
    # Assert
    assert row is not None and row["a2a_port"] == 8642


def test_a_registered_node_stores_its_source_host(pg_schema: str) -> None:
    # Arrange
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, source_host="peer-host"
    )
    # Act
    row = lookup_comms_node(name="lead")
    # Assert
    assert row is not None and row["source_host"] == "peer-host"


def test_a_locally_registered_node_has_no_source_host(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    row = lookup_comms_node(name="lead")
    # Assert
    assert row is not None and row["source_host"] is None


def test_re_registering_the_same_target_does_not_move_registered_at(
    pg_schema: str,
) -> None:
    # Arrange
    import time

    register_comms_node(name="lead", host="mba", a2a_port=8642)
    first = lookup_comms_node(name="lead")["registered_at"]
    time.sleep(0.01)
    # Act
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Assert — registered_at is IMMUTABLE; only updated_at moves.
    assert lookup_comms_node(name="lead")["registered_at"] == first


def test_re_registering_the_same_target_bumps_updated_at(pg_schema: str) -> None:
    # Arrange
    import time

    register_comms_node(name="lead", host="mba", a2a_port=8642)
    first = lookup_comms_node(name="lead")["updated_at"]
    time.sleep(0.01)
    # Act
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Assert
    assert lookup_comms_node(name="lead")["updated_at"] > first


def test_an_empty_name_is_refused(pg_schema: str) -> None:
    # Arrange
    refused = None
    # Act
    try:
        register_comms_node(name="", host="mba", a2a_port=8642)
    except ValueError as exc:
        refused = exc
    # Assert
    assert refused is not None


def test_a_zero_port_is_refused(pg_schema: str) -> None:
    # Arrange — the port=0 production-bug signature.
    refused = None
    # Act
    try:
        register_comms_node(name="lead", host="mba", a2a_port=0)
    except ValueError as exc:
        refused = exc
    # Assert
    assert refused is not None


# ---------------------------------------------------------------------------
# the flip: an unregistered node stops resolving
# ---------------------------------------------------------------------------


def test_an_unregistered_node_no_longer_resolves(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    unregister_comms_node(name="lead")
    # Assert — the whole point: the forwarder must stop dialling a dead
    # port the instant the tombstone lands.
    assert resolve_comms_node_host(name="lead") is None


def test_an_unregistered_node_is_absent_from_lookup(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    unregister_comms_node(name="lead")
    # Assert
    assert lookup_comms_node(name="lead") is None


def test_an_unregistered_node_is_absent_from_the_listing(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    register_comms_node(name="peer", host="spartan", a2a_port=8643)
    # Act
    unregister_comms_node(name="lead")
    # Assert
    assert [r["name"] for r in list_comms_nodes()] == ["peer"]


def test_unregister_reports_true_when_a_live_node_was_tombstoned(
    pg_schema: str,
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    removed = unregister_comms_node(name="lead")
    # Assert
    assert removed is True


def test_unregister_reports_false_for_a_name_never_registered(
    pg_schema: str,
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    removed = unregister_comms_node(name="ghost")
    # Assert
    assert removed is False


def test_unregister_reports_false_the_second_time(pg_schema: str) -> None:
    # Arrange — the hidden record still occupies the identity, so a naive
    # "does the record exist" check would answer True and lie.
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    unregister_comms_node(name="lead")
    # Act
    again = unregister_comms_node(name="lead")
    # Assert
    assert again is False


# ---------------------------------------------------------------------------
# the tombstone stops resolving WITHOUT forgetting
# ---------------------------------------------------------------------------


def test_a_tombstoned_node_is_still_on_record(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    unregister_comms_node(name="lead")
    # Act
    names = [r["name"] for r in list_comms_nodes(include_ended=True)]
    # Assert — "never registered" and "registered then stopped" stay
    # distinguishable; a DELETE could not answer this.
    assert names == ["lead"]


def test_a_tombstoned_node_keeps_the_port_it_died_on(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    unregister_comms_node(name="lead")
    # Act
    row = list_comms_nodes(include_ended=True)[0]
    # Assert
    assert row["a2a_port"] == 8642


def test_a_tombstone_records_when_it_was_written(pg_schema: str) -> None:
    # Arrange — ``hidden`` is the liveness truth, but a bool cannot carry
    # a WHEN, which is why ended_at survives as an audit stamp.
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    unregister_comms_node(name="lead")
    # Act
    row = list_comms_nodes(include_ended=True)[0]
    # Assert
    assert row["ended_at"] is not None


def test_reviving_a_node_clears_its_tombstone_stamp(pg_schema: str) -> None:
    # Arrange — the stamp and the hide flag must never drift apart.
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    unregister_comms_node(name="lead")
    # Act
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Assert
    assert lookup_comms_node(name="lead")["ended_at"] is None


# ---------------------------------------------------------------------------
# the conflict policy — fail loud, never a silent winner
# ---------------------------------------------------------------------------


def test_a_live_node_refuses_a_different_target_from_the_same_source(
    pg_schema: str,
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)

    # Act
    def _reregister() -> None:
        register_comms_node(name="lead", host="mba", a2a_port=9999)

    # Assert
    with pytest.raises(CommsNodeConflictError):
        _reregister()


def test_a_refused_registration_leaves_the_stored_target_untouched(
    pg_schema: str,
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    try:
        register_comms_node(name="lead", host="mba", a2a_port=9999)
    except CommsNodeConflictError:
        pass
    # Act
    row = lookup_comms_node(name="lead")
    # Assert — a refusal that half-wrote would be worse than an overwrite.
    assert row["a2a_port"] == 8642


def test_replace_true_overwrites_a_same_source_target(pg_schema: str) -> None:
    # Arrange — the explicit-client-option half of directive 12847.
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    register_comms_node(name="lead", host="mba", a2a_port=9999, replace=True)
    # Assert
    assert lookup_comms_node(name="lead")["a2a_port"] == 9999


def test_a_different_source_host_claiming_the_name_is_refused(
    pg_schema: str,
) -> None:
    # Arrange
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, source_host="host-a"
    )

    # Act
    def _claim() -> None:
        register_comms_node(
            name="lead", host="spartan", a2a_port=9999, source_host="host-b"
        )

    # Assert
    with pytest.raises(CommsNodeConflictError):
        _claim()


def test_replace_does_not_open_the_cross_host_conflict(pg_schema: str) -> None:
    # Arrange — the escape hatch must not loosen ADR-0014 name
    # uniqueness. This is the flip that matters most about `replace`.
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, source_host="host-a"
    )

    # Act
    def _claim() -> None:
        register_comms_node(
            name="lead",
            host="spartan",
            a2a_port=9999,
            source_host="host-b",
            replace=True,
        )

    # Assert
    with pytest.raises(CommsNodeConflictError):
        _claim()


def test_the_conflict_message_names_the_incoming_kind(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    message = ""
    # Act
    try:
        register_comms_node(name="lead", host="mba", a2a_port=9999, kind="spec")
    except CommsNodeConflictError as exc:
        message = str(exc)
    # Assert — the operator must see WHICH path tried to overwrite WHICH.
    assert "kind='spec'" in message


def test_the_conflict_message_names_the_incoming_source_path(
    pg_schema: str,
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    message = ""
    # Act
    try:
        register_comms_node(
            name="lead",
            host="mba",
            a2a_port=9999,
            kind="self-peer",
            source_path="/etc/agents/lead/spec.yaml",
        )
    except CommsNodeConflictError as exc:
        message = str(exc)
    # Assert
    assert "/etc/agents/lead/spec.yaml" in message


def test_the_conflict_message_names_the_existing_target(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    message = ""
    # Act
    try:
        register_comms_node(name="lead", host="mba", a2a_port=9999)
    except CommsNodeConflictError as exc:
        message = str(exc)
    # Assert
    assert "port=8642" in message


# ---------------------------------------------------------------------------
# the listing order — alphabetical by name, deliberately not the HLC
# ---------------------------------------------------------------------------


def test_the_listing_is_ordered_by_name_not_by_insertion(
    pg_schema: str,
) -> None:
    # Arrange — written in reverse-alphabetical order on purpose, so an
    # insertion-ordered (HLC) listing would fail this. `name` is the
    # IDENTITY: total, stable, tie-free and immune to clock skew, so it
    # needed no successor when rowid went away.
    register_comms_node(name="zeta", host="mba", a2a_port=8644)
    register_comms_node(name="alpha", host="mba", a2a_port=8642)
    register_comms_node(name="mid", host="mba", a2a_port=8643)
    # Act
    order = [r["name"] for r in list_comms_nodes()]
    # Assert
    assert order == ["alpha", "mid", "zeta"]


def test_the_listing_order_ignores_a_skewed_registered_at(
    pg_schema: str,
) -> None:
    # Arrange — a record carrying a registered_at far OLDER than one
    # already stored, the way a migrated peer row carries its original
    # timestamp verbatim. Ordering by a wall clock would put it first.
    from scitex_dev.store import NEW_RECORD

    from scitex_agent_container._state.state_db_comms_nodes import _open

    register_comms_node(name="alpha", host="mba", a2a_port=8642)
    store = _open()
    try:
        store.put(
            {
                "name": "zeta",
                "host": "spartan",
                "a2a_port": 8643,
                "registered_at": 1.0,
                "updated_at": 1.0,
                "source_host": "peer-with-a-skewed-clock",
                "ended_at": None,
            },
            expected_revision=NEW_RECORD,
        )
    finally:
        store.close()
    # Act
    order = [r["name"] for r in list_comms_nodes()]
    # Assert
    assert order == ["alpha", "zeta"]


def test_an_empty_name_never_resolves(pg_schema: str) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    info = resolve_comms_node_host(name="")
    # Assert
    assert info is None
