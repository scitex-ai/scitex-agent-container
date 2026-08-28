"""ONE store handle per process, because this table is the a2a routing path.

Every other module migrated to ``scitex_dev.store`` this month opens and
closes a ``Store`` per call, mirroring the old ``with open_db(...)`` shape.
``comms_nodes`` cannot: ``lookup_comms_node`` / ``resolve_comms_node_host`` /
``list_comms_nodes`` sit under ``resolve_node_host``,
``resolve_forward_target`` and ``_agents_list``, which run PER MESSAGE.

MEASURED, not assumed (card ``sqlite-out-per-call-connect-cost-20260828``):
``sqlite3.connect`` 0.067 ms against ``psycopg.connect`` 10.707 ms — 159x —
and ``Store.__init__`` pays that connect plus the dialect ``schema_lock`` and
two probes even when no DDL runs. End to end, ``resolve_comms_node_host``
measured 1.03 ms/call on SQLite against 45.3 ms/call with a per-call
``Store``: a ~44x routing regression.

WHAT THESE TESTS ARE FOR, AND WHAT THEY ARE NOT
===============================================
They pin the SHAPE (one handle, reused; a separate owned handle for the
one-shot migration; an explicit reset), not the timing. A performance
assertion here would be a flake on a loaded runner and would not fail for
the reason it claims — the numbers above belong in the card and in the
module docstring, where they can be re-measured rather than re-run.

The identity assertion is the honest proxy: if ``open_comms_nodes_store``
ever went back to constructing per call, ``second is first`` fails, and it
fails for exactly that reason.

Needs a real PostgreSQL: ``pg_schema`` is the shared opt-in fixture. NO
MONKEYPATCH (PA-306 §3) — the cache is dropped through the module's own
public ``reset_comms_nodes_store`` hook, which exists precisely so nobody
has to reach in and rewrite a module global.
"""

from __future__ import annotations

from scitex_agent_container._state.state_db_comms_nodes import (
    lookup_comms_node,
    new_comms_nodes_store,
    open_comms_nodes_store,
    register_comms_node,
    reset_comms_nodes_store,
    unregister_comms_node,
)


def test_the_store_handle_is_reused_across_calls(pg_schema: str) -> None:
    # Arrange
    first = open_comms_nodes_store()
    # Act
    second = open_comms_nodes_store()
    # Assert — a per-call Store would make these different objects, and
    # would pay psycopg.connect + schema_lock on every routing lookup.
    assert second is first


def test_a_fresh_handle_is_not_the_shared_one(pg_schema: str) -> None:
    # Arrange — the one-shot migration owns and closes its own connection,
    # so it must not be handed the process-wide handle to close.
    shared = open_comms_nodes_store()
    # Act
    owned = new_comms_nodes_store()
    try:
        same = owned is shared
    finally:
        owned.close()
    # Assert
    assert same is False


def test_closing_an_owned_handle_leaves_the_shared_one_usable(
    pg_schema: str,
) -> None:
    # Arrange — the failure this guards: the migration closes what it was
    # given, and if that were the cached handle every later routing lookup
    # in the process would raise on a dead connection.
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    owned = new_comms_nodes_store()
    # Act
    owned.close()
    # Assert
    assert lookup_comms_node(name="lead") is not None


def test_resetting_the_cache_yields_a_new_handle(pg_schema: str) -> None:
    # Arrange — the explicit hook, because the ecosystem bans monkeypatch and
    # a cache nothing can drop is a cache tests cannot isolate.
    first = open_comms_nodes_store()
    # Act
    reset_comms_nodes_store()
    second = open_comms_nodes_store()
    # Assert
    assert second is not first


def test_resetting_twice_is_a_no_op(pg_schema: str) -> None:
    # Arrange — a teardown hook that raised on a second call would turn an
    # unrelated failure into a confusing one.
    open_comms_nodes_store()
    reset_comms_nodes_store()
    # Act
    reset_comms_nodes_store()
    # Assert — still serves a handle afterwards.
    assert open_comms_nodes_store() is not None


def test_the_reused_handle_still_reads_what_it_wrote(pg_schema: str) -> None:
    # Arrange — the caching must not change any ANSWER. A handle that went
    # stale between calls would show up here and nowhere else.
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    unregister_comms_node(name="lead")
    # Act
    register_comms_node(name="lead", host="mba", a2a_port=9000)
    # Assert
    assert lookup_comms_node(name="lead")["a2a_port"] == 9000
