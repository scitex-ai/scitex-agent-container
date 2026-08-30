"""ONE store handle per process, because this table is the a2a routing path.

Every other module migrated to ``scitex_dev.store`` this month opens and
closes a ``Store`` per call, mirroring the old ``with open_db(...)`` shape.
``comms_nodes`` cannot: ``lookup_comms_node`` / ``resolve_comms_node_host`` /
``list_comms_nodes`` sit under ``resolve_node_host``,
``resolve_forward_target`` and ``_agents_list``, which run PER MESSAGE.

MEASURED, not assumed (card ``store-connect-cost-per-call-20260828``):
the previous local connect 0.067 ms against ``psycopg.connect`` 10.707 ms — 159x —
and ``Store.__init__`` pays that connect plus the dialect ``schema_lock`` and
two probes even when no DDL runs. End to end, ``resolve_comms_node_host``
measured 1.03 ms/call before the move against 45.3 ms/call with a per-call
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
    CommsNodeConflictError,
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


# ---------------------------------------------------------------------------
# the connect bound, and what a reconnect will and will not retry
# ---------------------------------------------------------------------------


def test_the_resolved_dsn_carries_a_connect_timeout(pg_schema: str) -> None:
    # Arrange — libpq's default is WAIT FOREVER, and this store is read on
    # the a2a routing path, so an unbounded connect turns a blackholed
    # primary into a stalled listen daemon rather than a failed request.
    store = open_comms_nodes_store()
    # Act
    dsn = str(store.target.dsn)
    # Assert
    assert "connect_timeout=" in dsn


def test_an_operator_connect_timeout_is_not_overridden() -> None:
    # Arrange — an explicit value in the DSN outranks this module's default.
    from scitex_dev.store import StoreTarget

    from scitex_agent_container._state.state_db_comms_nodes_store import (
        _with_connect_timeout,
    )

    declared = StoreTarget.postgres(
        "postgresql://h:5432/db?connect_timeout=30",
        pkg="scitex_agent_container",
        name="comms_nodes",
    )
    # Act
    resolved = _with_connect_timeout(declared)
    # Assert
    assert str(resolved.dsn).count("connect_timeout") == 1


def test_a_data_verdict_is_not_retried_as_a_lost_connection(
    pg_schema: str,
) -> None:
    # Arrange — the retry must distinguish "the socket died" from "the store
    # said no". Re-running a rejected operation on a fresh handle would only
    # hide the rejection, and ``CommsNodeConflictError`` is a verdict about
    # the DATA that means the same thing on every connection.
    from scitex_agent_container._state.state_db_comms_nodes_store import (
        _is_connection_lost,
    )

    verdict = CommsNodeConflictError("two hosts claim this name")
    # Act
    retryable = _is_connection_lost(verdict)
    # Assert
    assert retryable is False


def test_a_closed_connection_is_recognised_as_lost(pg_schema: str) -> None:
    # Arrange — the case measured on the live primary: after the backend was
    # killed, the cached handle raised this forever while a fresh connection
    # proved the server healthy.
    import psycopg

    from scitex_agent_container._state.state_db_comms_nodes_store import (
        _is_connection_lost,
    )

    dead = psycopg.OperationalError("the connection is closed")
    # Act
    retryable = _is_connection_lost(dead)
    # Assert
    assert retryable is True


def test_a_dead_handle_is_evicted_and_the_next_call_succeeds(
    pg_schema: str,
) -> None:
    # Arrange — close the cached connection out from under the cache, which
    # is what a PostgreSQL restart does to a long-lived listen daemon. Before
    # the eviction path existed this poisoned the handle permanently.
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    open_comms_nodes_store()._connection.close()
    # Act
    info = lookup_comms_node(name="lead")
    # Assert — served from a reopened handle, not from the dead one.
    assert info is not None and info["a2a_port"] == 8642


def test_a_dead_handle_is_evicted_for_writes_too(pg_schema: str) -> None:
    # Arrange — reads recovering while writes stayed broken would be the
    # worst shape: the directory would look healthy and stop being updated.
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    open_comms_nodes_store()._connection.close()
    # Act
    register_comms_node(name="lead", host="mba", a2a_port=8642, replace=True)
    # Assert
    assert lookup_comms_node(name="lead") is not None
