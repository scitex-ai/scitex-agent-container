"""ONE store handle per process — ``instances`` is the a2a routing path too.

``comms_nodes`` earned its cached handle first; ``instances`` sits UNDER it
in the same resolvers. ``list_active_instances`` / ``live_instance_for_name``
run beneath ``_send_resolve``, ``resolve_node_host``,
``resolve_forward_target`` and ``_agents_list`` — per message, not per
operator command.

MEASURED, not assumed (card ``sqlite-out-per-call-connect-cost-20260828``):
the previous local connect 0.067 ms against ``psycopg.connect`` 10.707 ms — 159x —
and ``Store.__init__`` pays that connect plus the dialect ``schema_lock`` and
two probes even when no DDL runs. On the sibling table a per-call ``Store``
measured a ~44x routing regression end to end.

WHAT THESE TESTS ARE FOR, AND WHAT THEY ARE NOT
===============================================
They pin the SHAPE (one handle, reused; a separate owned handle for the
one-shot migration; an explicit reset; eviction of a DEAD handle), not the
timing. A performance assertion here would flake on a loaded runner and
would not fail for the reason it claims.

The identity assertion is the honest proxy: if ``open_instances_store`` ever
went back to constructing per call, ``second is first`` fails, and it fails
for exactly that reason.

Needs a real PostgreSQL: ``pg_schema`` is the shared opt-in fixture. NO
MONKEYPATCH (PA-306 §3) — the cache is dropped through the module's own
public ``reset_instances_store`` hook.
"""

from __future__ import annotations

from scitex_agent_container._state.state_db_instances import (
    last_known_instance,
    record_instance_start,
    record_instance_stop,
)
from scitex_agent_container._state.state_db_instances_store import (
    new_instances_store,
    open_instances_store,
    reset_instances_store,
)


def test_the_store_handle_is_reused_across_calls(pg_schema: str) -> None:
    # Arrange
    first = open_instances_store()
    # Act
    second = open_instances_store()
    # Assert — a per-call Store would make these different objects, and would
    # pay psycopg.connect + schema_lock on every routing lookup.
    assert second is first


def test_a_fresh_handle_is_not_the_shared_one(pg_schema: str) -> None:
    # Arrange — the one-shot migration owns and closes its own connection, so
    # it must not be handed the process-wide handle to close.
    shared = open_instances_store()
    # Act
    owned = new_instances_store()
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
    # given, and if that were the cached handle every later lifecycle read in
    # the process would raise on a dead connection.
    record_instance_start("alpha", host="host-a", a2a_port=8001)
    owned = new_instances_store()
    # Act
    owned.close()
    # Assert
    assert last_known_instance("alpha") is not None


def test_resetting_the_cache_yields_a_new_handle(pg_schema: str) -> None:
    # Arrange — the explicit hook, because the ecosystem bans monkeypatch and
    # a cache nothing can drop is a cache tests cannot isolate.
    first = open_instances_store()
    # Act
    reset_instances_store()
    second = open_instances_store()
    # Assert
    assert second is not first


def test_resetting_twice_is_a_no_op(pg_schema: str) -> None:
    # Arrange — a teardown hook that raised on a second call would turn an
    # unrelated failure into a confusing one.
    open_instances_store()
    reset_instances_store()
    # Act
    reset_instances_store()
    # Assert — still serves a handle afterwards.
    assert open_instances_store() is not None


def test_a_changed_dsn_swaps_the_handle_rather_than_serving_the_old_store(
    pg_schema: str,
) -> None:
    # Arrange — the cache is keyed on the RESOLVED TARGET, not on "have we
    # opened one". This is the property the suite itself depends on: the
    # pg_schema fixture points SCITEX_STORE_DSN at a fresh throwaway schema
    # per test, and a cache that ignored it would hand every test the FIRST
    # test's store. Real save/restore, no monkeypatch.
    import os

    first = open_instances_store()
    key = "SCITEX_STORE_DSN"
    saved = os.environ.get(key)
    os.environ[key] = f"{saved}&application_name=handle_swap_probe"
    try:
        # Act
        second = open_instances_store()
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        reset_instances_store()
    # Assert
    assert second is not first


def test_the_resolved_dsn_carries_a_connect_timeout(pg_schema: str) -> None:
    # Arrange — libpq's default is WAIT FOREVER, and this store is read on
    # the a2a routing path, so an unbounded connect turns a blackholed
    # primary into a stalled listen daemon rather than a failed request.
    store = open_instances_store()
    # Act
    dsn = str(store.target.dsn)
    # Assert
    assert "connect_timeout=" in dsn


def test_an_operator_connect_timeout_is_not_overridden() -> None:
    # Arrange — an explicit value in the DSN outranks this module's default.
    from scitex_dev.store import StoreTarget

    from scitex_agent_container._state.state_db_instances_store import (
        _with_connect_timeout,
    )

    declared = StoreTarget.postgres(
        "postgresql://h:5432/db?connect_timeout=30",
        pkg="scitex_agent_container",
        name="instances",
    )
    # Act
    resolved = _with_connect_timeout(declared)
    # Assert
    assert str(resolved.dsn).count("connect_timeout") == 1


def test_a_data_verdict_is_not_retried_as_a_lost_connection() -> None:
    # Arrange — the retry must distinguish "the socket died" from "the store
    # said no". Re-running a rejected operation on a fresh handle would only
    # hide the rejection, and RevisionMismatchError is a verdict about the
    # DATA that means the same thing on every connection.
    from scitex_dev.store import RevisionMismatchError

    from scitex_agent_container._state.state_db_instances_store import (
        _is_connection_lost,
    )

    verdict = RevisionMismatchError("another writer won the stop")
    # Act
    retryable = _is_connection_lost(verdict)
    # Assert
    assert retryable is False


def test_a_closed_connection_is_recognised_as_lost() -> None:
    # Arrange — the case measured on the live primary: after the backend was
    # killed, the cached handle raised this forever while a fresh connection
    # proved the server healthy.
    import psycopg

    from scitex_agent_container._state.state_db_instances_store import (
        _is_connection_lost,
    )

    dead = psycopg.OperationalError("the connection is closed")
    # Act
    retryable = _is_connection_lost(dead)
    # Assert
    assert retryable is True


def test_a_dead_handle_is_evicted_and_the_next_read_succeeds(
    pg_schema: str,
) -> None:
    # Arrange — close the cached connection out from under the cache, which
    # is what a PostgreSQL restart does to a long-lived listen daemon.
    # Without eviction this poisons the handle permanently and every agent
    # reads as "not running".
    record_instance_start("alpha", host="host-a", a2a_port=8001)
    open_instances_store()._connection.close()
    # Act
    row = last_known_instance("alpha")
    # Assert — served from a reopened handle, not from the dead one.
    assert row is not None and row["a2a_port"] == 8001


def test_a_dead_handle_is_evicted_for_writes_too(pg_schema: str) -> None:
    # Arrange — reads recovering while writes stayed broken would be the
    # worst shape: the fleet would look healthy and stop being tombstoned.
    instance_id = record_instance_start("alpha", host="host-a", a2a_port=8001)
    open_instances_store()._connection.close()
    # Act
    ended = record_instance_stop(instance_id)
    # Assert
    assert ended is True
