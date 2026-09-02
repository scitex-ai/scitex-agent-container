#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The ``lineage`` store — the properties the previous backend could not have.

Companion to the 2026-08-28 move. Behaviour that merely SURVIVED the
migration (the three group relations, the walks, the ACL decisions) stays
covered by the suites that already owned it. What is here is the set of
claims the STORE introduces, each of which the ACL now rests on:

* IMMUTABLE ``parent_name`` KEEPS THE FIRST VALUE AND DOES NOT RAISE.
  ``record_lineage`` is built on that sentence, so it is measured against
  the real primitive rather than taken from the documentation. If ``put``
  ever started raising instead of reporting, the writer would go from
  "logs a contradiction" to "crashes a spawn", and only this file would
  say so.
* The solitary short-circuit never opens the store. Cheap against a local file,
  load-bearing against PostgreSQL: an isolated capsule must resolve its
  own group without a network round-trip.
* The handle is cached per process and EVICTED when the DSN changes.

Real PostgreSQL via the ``pg_schema`` fixture, no mocks. These SKIP on a
host with no writable database — which includes every agent container,
where loopback is a read-only replica of the fleet cluster. See
``tests/_store_isolation.py``.
"""

from __future__ import annotations

import logging
import os

import pytest

from scitex_agent_container._state import state_db_lineage_store as store_mod
from scitex_agent_container._state.state_db_lineage_group import (
    derive_group,
    record_lineage,
)
from scitex_agent_container._state.state_db_lineage_store import (
    new_lineage_store,
    open_lineage_store,
    parent_name_of,
    read_edges,
    reset_lineage_store,
)

UNREACHABLE_DSN = "postgresql://sac_tests@127.0.0.1:1/there_is_no_server_here"


@pytest.fixture(autouse=True)
def _drop_cached_handle():
    """Drop the process-wide handle around every test in this file.

    The cache is keyed on the resolved target, so ``pg_schema`` already
    swaps it — but these tests assert ON the handle, so they must start
    from a known-empty cache rather than from whatever a neighbour left
    behind. A real function call, not ``monkeypatch``: the reset hook
    exists precisely so nobody has to reach into the module.
    """
    reset_lineage_store()
    yield
    reset_lineage_store()


# ---------------------------------------------------------------------------
# The premise the writer is built on: IMMUTABLE keeps first, never raises.
# ---------------------------------------------------------------------------


@pytest.fixture
def contradicted_edge(pg_schema: str):
    """Two DIFFERENT parents written for one child, through the primitive.

    Yields ``(second_put_result, store)``. The store stays open so a test
    can read back what survived.
    """
    from scitex_dev.store import ANY_REVISION

    store = new_lineage_store()
    try:
        store.put(
            {"child_name": "kid", "parent_name": "first", "created_at": 1.0},
            expected_revision=ANY_REVISION,
        )
        result = store.put(
            {"child_name": "kid", "parent_name": "second", "created_at": 2.0},
            expected_revision=ANY_REVISION,
        )
        yield result, store
    finally:
        store.close()


def test_a_second_differing_parent_does_not_raise(contradicted_edge) -> None:
    """The whole keep-first contract depends on ``put`` returning normally."""
    # Arrange
    result, _store = contradicted_edge
    # Act
    conflicted_fields = [conflict.field for conflict in result.conflicts]
    # Assert
    assert "parent_name" in conflicted_fields


def test_the_conflict_names_the_kept_parent(contradicted_edge) -> None:
    # Arrange
    result, _store = contradicted_edge
    # Act
    parent_conflict = next(
        conflict for conflict in result.conflicts if conflict.field == "parent_name"
    )
    # Assert
    assert parent_conflict.kept == "first"


def test_the_conflict_names_the_rejected_parent(contradicted_edge) -> None:
    # Arrange
    result, _store = contradicted_edge
    # Act
    parent_conflict = next(
        conflict for conflict in result.conflicts if conflict.field == "parent_name"
    )
    # Assert
    assert parent_conflict.rejected == "second"


def test_the_first_parent_is_what_the_store_still_holds(contradicted_edge) -> None:
    # Arrange
    _result, store = contradicted_edge
    # Act
    stored = str(store.get({"child_name": "kid"}).values["parent_name"])
    # Assert
    assert stored == "first"


# ---------------------------------------------------------------------------
# record_lineage over the store.
# ---------------------------------------------------------------------------


@pytest.fixture
def reparent_attempt(pg_schema: str, caplog: pytest.LogCaptureFixture):
    """A re-parent attempt through the production writer.

    Yields the WARNING messages it emitted, SNAPSHOT HERE rather than the
    ``caplog`` object itself, and that detail is the whole fixture.
    ``caplog.records`` is PHASE-SCOPED: the plugin resets its capture
    handler between setup / call / teardown, so a record emitted inside a
    FIXTURE is filed under "setup" and ``caplog.records`` reads EMPTY from
    the test body.

    Measured, because the first version of this file got it wrong and CI
    caught it: yielding ``caplog`` made the assertion below fail on an
    empty list while pytest's own report printed the record under
    "Captured log setup" three lines further down. The reparent had been
    refused correctly the whole time — the test was reading the wrong
    phase, not observing a missing log.
    """
    record_lineage(child="kid", parent="original")
    with caplog.at_level(logging.WARNING):
        record_lineage(child="kid", parent="usurper")
    yield [record.getMessage() for record in caplog.records]


def test_a_reparent_attempt_keeps_the_original_parent(reparent_attempt) -> None:
    # Arrange
    _messages = reparent_attempt
    # Act
    parent = parent_name_of("kid")
    # Assert
    assert parent == "original"


def test_a_reparent_attempt_is_logged_rather_than_raised(reparent_attempt) -> None:
    """Logged, not raised — the restart-in-place contract from WI-2."""
    # Arrange
    messages = reparent_attempt
    # Act
    logged = [message for message in messages if "keeps parent" in message]
    # Assert
    assert logged != []


def test_recording_the_same_parent_twice_is_a_no_op(pg_schema: str) -> None:
    # Arrange
    record_lineage(child="kid", parent="mum")
    # Act
    record_lineage(child="kid", parent="mum")
    # Assert
    assert read_edges().parent_of["kid"] == "mum"


def test_an_empty_child_name_is_a_programming_error(pg_schema: str) -> None:
    # Arrange
    child = ""
    # Act
    # (the call itself is the act; it must refuse rather than write)
    # Assert
    with pytest.raises(ValueError):
        record_lineage(child=child, parent="mum")


def test_read_edges_indexes_children_by_parent(pg_schema: str) -> None:
    # Arrange
    record_lineage(child="kid-a", parent="root")
    record_lineage(child="kid-b", parent="root")
    # Act
    children = read_edges().children("root")
    # Assert
    assert children == {"kid-a", "kid-b"}


def test_parent_name_of_an_unknown_name_is_none(pg_schema: str) -> None:
    # Arrange
    name = "never-spawned"
    # Act
    parent = parent_name_of(name)
    # Assert
    assert parent is None


# ---------------------------------------------------------------------------
# The solitary short-circuit must not touch PostgreSQL at all.
# ---------------------------------------------------------------------------


@pytest.fixture
def solitary_capsule(pg_schema: str):
    """A capsule with ``lineage_group='solitary'`` AND a real parent edge.

    The edge matters: without one, a singleton answer would prove nothing,
    because an agent with no edges is a singleton anyway.
    """
    from scitex_agent_container._state.state_db_acl_policy import (
        record_comms_policy,
    )

    record_comms_policy(
        name="capsule",
        outbound_siblings="allow",
        outbound_parent="allow",
        inbound_siblings="allow",
        inbound_parent="allow",
        lineage_group="solitary",
        may_spawn=True,
    )
    record_lineage(child="capsule", parent="root")
    record_lineage(child="sibling", parent="root")
    reset_lineage_store()
    yield "capsule"


def test_a_solitary_capsule_resolves_to_itself(solitary_capsule: str) -> None:
    # Arrange
    name = solitary_capsule
    # Act
    group = derive_group(name=name)
    # Assert
    assert group == {name}


def test_the_solitary_path_never_opens_the_lineage_store(
    solitary_capsule: str,
) -> None:
    """Proven by OBSERVING THE REAL MODULE rather than by patching it.

    The process-wide handle starts as ``None`` (the fixture resets it), and
    opening the store is the only thing that fills it. A handle still
    ``None`` after the call is direct evidence that no round-trip happened
    — which is the property that keeps an isolated capsule resolvable when
    the primary is unreachable.
    """
    # Arrange
    derive_group(name=solitary_capsule)
    # Act
    handle_after = store_mod._HANDLE
    # Assert
    assert handle_after is None


# ---------------------------------------------------------------------------
# The cached handle.
# ---------------------------------------------------------------------------


def test_the_handle_is_shared_across_calls(pg_schema: str) -> None:
    # Arrange
    first = open_lineage_store()
    # Act
    second = open_lineage_store()
    # Assert
    assert first is second


@pytest.fixture
def repointed_dsn(pg_schema: str):
    """Open the store, then repoint ``SCITEX_STORE_DSN`` at nothing.

    Real ``os.environ`` save/restore, not ``monkeypatch``: the point is
    that the REAL resolver reads the REAL variable.
    """
    open_lineage_store()
    saved = os.environ.get("SCITEX_STORE_DSN")
    os.environ["SCITEX_STORE_DSN"] = UNREACHABLE_DSN
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("SCITEX_STORE_DSN", None)
        else:
            os.environ["SCITEX_STORE_DSN"] = saved


def test_a_changed_dsn_evicts_the_cached_handle(repointed_dsn) -> None:
    """The cache is keyed on the RESOLVED TARGET, not on "have we opened one".

    Proven without needing a second database: repointing at an address
    nothing answers must FAIL rather than hand back the previous handle. A
    cache keyed on mere existence would serve the old connection and
    silently read the wrong store — which is the bug this key prevents, and
    the one the ``pg_schema`` fixture would otherwise hit on every test
    after the first.
    """
    # Arrange
    _ = repointed_dsn
    # Act
    # (opening again is the act; it must re-resolve rather than reuse)
    # Assert
    with pytest.raises(Exception):
        open_lineage_store()

# EOF
