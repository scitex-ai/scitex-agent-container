"""``instances`` on PostgreSQL — and a stop that actually un-leases.

This table is the fleet's LEASE, not merely its history: ``ended_at IS
NULL`` is what tells ``sac agents start`` an agent is already running,
what tells the forwarder where to POST, and what tells the reconciler not
to restart something alive. So the tests that matter here are the FLIPS —
not "a start can be written" but "after a stop, the predicate the routers
call says NO", and its mirror, "after a stop the record is still THERE".
A migration that stored starts perfectly and forgot to un-lease would
read as green on every round-trip assertion and would leave every stopped
agent permanently un-startable.

FOUR TESTS THAT WOULD HAVE BEEN DELETED WITH THIS FILE'S PREDECESSOR, and
were, with the reason stated rather than edited until they passed:

  * ``test_fresh_instances_table_has_bound_port_column``
  * ``test_fresh_instances_table_has_remote_column``
  * ``test_fresh_instances_table_has_spawned_by_column``
  * ``test_migration_adds_bound_port_to_pre_existing_table``
  * ``test_migration_preserves_legacy_row_on_pre_existing_table``
  * ``test_migration_is_idempotent_on_replay``

Every one asserted against ``PRAGMA table_info(instances)`` after
``init_schema()`` — i.e. against SQLite DDL and a SQLite-native ``ALTER
TABLE ... ADD COLUMN`` migration. Both are gone: there is no ``instances``
table in state.db and no ``migrate_instances_add_family_tree_cols``. The
PROPERTY they were defending — that a start records ``bound_port`` /
``remote`` / ``spawned_by`` — survives and is tested below through the
public writer, which is where it was always the more useful assertion.
Rewriting them to inspect the PostgreSQL catalogue instead would have
tested the store's own DDL rather than sac's behaviour.

The old module-scoped ``db_path`` fixture went with them. Under one
shared PostgreSQL store there is no per-path isolation to have, so a
fixture handing out a temp file would have named something nothing reads
while the writes went to the live fleet registry.

Needs a real PostgreSQL: ``pg_schema`` is the shared opt-in fixture, which
skips where no cluster exists and FAILS where a configured one is broken.

NO MONKEYPATCH (PA-306 §3): the module is exercised through its real
public surface, and isolation comes from the fixture pointing
SCITEX_STORE_DSN at a throwaway schema.
"""

from __future__ import annotations

from scitex_agent_container._state.state_db_instances import (
    all_instances,
    end_instance,
    last_known_instance,
    latest_active_instance,
    list_active_instances,
    put_instance_record,
    record_instance_start,
    record_instance_stop,
    touch_instance_counters,
)


def _row(name: str) -> dict:
    """The one live record for ``name``."""
    return [r for r in list_active_instances() if r["name"] == name][0]


# ---------------------------------------------------------------------------
# the flip: a stop must un-lease
# ---------------------------------------------------------------------------


def test_a_started_instance_is_live(pg_schema: str) -> None:
    # Arrange
    record_instance_start(name="alpha", host="h1", a2a_port=19001)
    # Act
    live = [r["name"] for r in list_active_instances()]
    # Assert
    assert live == ["alpha"]


def test_a_stopped_instance_is_no_longer_live(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start(name="alpha", host="h1")
    # Act
    record_instance_stop(instance_id)
    # Assert — the whole point of the module.
    assert list_active_instances() == []


def test_stop_reports_true_when_a_live_lease_was_ended(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start(name="alpha", host="h1")
    # Act
    ended = record_instance_stop(instance_id)
    # Assert
    assert ended is True


def test_stop_reports_false_the_second_time(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start(name="alpha", host="h1")
    record_instance_stop(instance_id)
    # Act
    again = record_instance_stop(instance_id)
    # Assert
    assert again is False


def test_stop_reports_false_for_an_unknown_instance(pg_schema: str) -> None:
    # Arrange
    record_instance_start(name="alpha", host="h1")
    # Act
    ended = record_instance_stop("no-such-instance-id")
    # Assert
    assert ended is False


# ---------------------------------------------------------------------------
# a stop un-leases WITHOUT forgetting
# ---------------------------------------------------------------------------


def test_a_stopped_instance_is_still_on_record(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start(name="alpha", host="spartan-bm043")
    record_instance_stop(instance_id, exit_reason="superseded")
    # Act
    row = last_known_instance("alpha")
    # Assert — a hide() would have made this None on a default read.
    assert row["host"] == "spartan-bm043"


def test_a_stopped_instance_keeps_its_exit_reason(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start(name="alpha", host="h1")
    # Act
    record_instance_stop(instance_id, exit_reason="superseded")
    # Assert
    assert last_known_instance("alpha")["exit_reason"] == "superseded"


def test_a_recorded_death_cannot_be_rewritten(pg_schema: str) -> None:
    # Arrange — two reapers racing on one record; ended_at is IMMUTABLE, so
    # the FIRST verdict stands and the second is dropped rather than
    # silently overwriting the history.
    instance_id = record_instance_start(name="alpha", host="h1")
    end_instance(instance_id, ended_at="2026-01-01T00:00:00Z", exit_reason="first")
    # Act
    end_instance(instance_id, ended_at="2026-02-02T00:00:00Z", exit_reason="second")
    # Assert
    assert last_known_instance("alpha")["exit_reason"] == "first"


def test_end_instance_reports_false_on_an_already_dead_record(
    pg_schema: str,
) -> None:
    # Arrange — the merge RECORDS an immutability conflict rather than
    # raising, so a caller that only watched for an exception would report a
    # re-stop as fresh. end_instance reads the stamp instead.
    instance_id = record_instance_start(name="alpha", host="h1")
    end_instance(instance_id, ended_at="2026-01-01T00:00:00Z", exit_reason="first")
    # Act
    again = end_instance(
        instance_id, ended_at="2026-02-02T00:00:00Z", exit_reason="second"
    )
    # Assert
    assert again is False


# ---------------------------------------------------------------------------
# the family-tree fields (the property the deleted DDL tests defended)
# ---------------------------------------------------------------------------


def test_a_start_persists_the_remote_flag(pg_schema: str) -> None:
    # Arrange / no fixture state needed beyond the schema
    # Act
    record_instance_start(name="rem-1", host="peer-x", a2a_port=19001, remote=True)
    # Assert
    assert _row("rem-1")["remote"] == 1


def test_a_local_start_defaults_remote_to_zero(pg_schema: str) -> None:
    # Arrange
    name = "loc-1"
    # Act
    record_instance_start(name=name, host="this-host", a2a_port=19003)
    # Assert
    assert _row(name)["remote"] == 0


def test_a_start_persists_spawned_by(pg_schema: str) -> None:
    # Arrange
    parent = "parent-agent"
    # Act
    record_instance_start(name="sb-1", host="peer-x", spawned_by=parent)
    # Assert
    assert _row("sb-1")["spawned_by"] == parent


def test_bound_port_defaults_to_the_a2a_port(pg_schema: str) -> None:
    # Arrange — the caller knows only the resolved a2a_port.
    port = 19042
    # Act
    record_instance_start(name="bp-1", host="peer-x", a2a_port=port)
    # Assert
    assert _row("bp-1")["bound_port"] == port


def test_an_explicit_bound_port_wins_over_the_default(pg_schema: str) -> None:
    # Arrange
    explicit = 19077
    # Act
    record_instance_start(
        name="bp-2", host="peer-x", a2a_port=None, bound_port=explicit
    )
    # Assert
    assert _row("bp-2")["bound_port"] == explicit


def test_an_unwritten_field_still_appears_in_the_row(pg_schema: str) -> None:
    # Arrange — the store omits never-written fields from row.values, but
    # consumers were handed SELECT * and index rather than .get().
    record_instance_start(name="alpha", host="h1")
    # Act
    row = _row("alpha")
    # Assert
    assert row["exit_reason"] is None


# ---------------------------------------------------------------------------
# ordering: started_at, NOT the write order
# ---------------------------------------------------------------------------


def test_last_known_instance_ranks_by_started_at_not_by_write_order(
    pg_schema: str,
) -> None:
    # Arrange — write the NEWER start first, the older one second, the way
    # import_state carries a peer's timestamp verbatim. Ordering by the HLC
    # (i.e. by when this host learned of the record) would answer with the
    # second write; ordering by started_at answers with the newer start.
    put_instance_record(
        {
            "id": "id-newer",
            "name": "clew",
            "host": "spartan-bm001",
            "scope": "global",
            "started_at": "2026-08-02T00:00:00Z",
        }
    )
    put_instance_record(
        {
            "id": "id-older",
            "name": "clew",
            "host": "spartan-bm043",
            "scope": "global",
            "started_at": "2026-08-01T00:00:00Z",
        }
    )
    # Act
    row = last_known_instance("clew")
    # Assert
    assert row["host"] == "spartan-bm001"


def test_last_known_instance_is_none_for_an_unseen_name(pg_schema: str) -> None:
    # Arrange
    record_instance_start(name="other", host="lead-host")
    # Act
    row = last_known_instance("never-seen")
    # Assert
    assert row is None


def test_all_instances_includes_the_ended_records(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start(name="alpha", host="h1")
    record_instance_stop(instance_id)
    # Act
    names = [r["name"] for r in all_instances()]
    # Assert
    assert names == ["alpha"]


def test_the_host_filter_excludes_other_hosts(pg_schema: str) -> None:
    # Arrange
    record_instance_start(name="here", host="h1")
    record_instance_start(name="there", host="h2")
    # Act
    names = [r["name"] for r in list_active_instances(host="h1")]
    # Assert
    assert names == ["here"]


def test_latest_active_instance_ignores_a_stopped_record(pg_schema: str) -> None:
    # Arrange — the address lookup behind the routers must not hand back a
    # dead agent's port.
    instance_id = record_instance_start(name="alpha", host="h1", a2a_port=19001)
    record_instance_stop(instance_id)
    # Act
    row = latest_active_instance("alpha")
    # Assert
    assert row is None


# ---------------------------------------------------------------------------
# the rolling counters (the COALESCE columns)
# ---------------------------------------------------------------------------


def test_a_beat_refreshes_the_last_heartbeat_stamp(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start(name="alpha", host="h1")
    # Act
    touch_instance_counters(instance_id, last_heartbeat_at="2026-08-28T00:00:00Z")
    # Assert
    assert _row("alpha")["last_heartbeat_at"] == "2026-08-28T00:00:00Z"


def test_a_none_counter_leaves_the_previous_value_alone(pg_schema: str) -> None:
    # Arrange — the SQLite statement was COALESCE(?, iter_count); omitting
    # the field is how a partial put spells the same thing.
    instance_id = record_instance_start(name="alpha", host="h1")
    touch_instance_counters(
        instance_id, last_heartbeat_at="2026-08-28T00:00:00Z", iter=7
    )
    # Act
    touch_instance_counters(instance_id, last_heartbeat_at="2026-08-28T00:00:01Z")
    # Assert
    assert _row("alpha")["iter_count"] == 7


def test_a_later_counter_overwrites_the_earlier_one(pg_schema: str) -> None:
    # Arrange — LAST_WRITER_WINS, not MAX: a runner reporting a LOWER number
    # must be visible rather than silently frozen out.
    instance_id = record_instance_start(name="alpha", host="h1")
    touch_instance_counters(
        instance_id, last_heartbeat_at="2026-08-28T00:00:00Z", iter=7
    )
    # Act
    touch_instance_counters(
        instance_id, last_heartbeat_at="2026-08-28T00:00:01Z", iter=2
    )
    # Assert
    assert _row("alpha")["iter_count"] == 2


def test_a_beat_for_an_unknown_instance_writes_nothing(pg_schema: str) -> None:
    # Arrange — the SQLite UPDATE ... WHERE id=? simply affected zero rows.
    record_instance_start(name="alpha", host="h1")
    # Act
    touch_instance_counters("no-such-id", last_heartbeat_at="2026-08-28T00:00:00Z")
    # Assert
    assert len(all_instances()) == 1


# ---------------------------------------------------------------------------
# the bulk-import door
# ---------------------------------------------------------------------------


def test_put_instance_record_preserves_the_original_started_at(
    pg_schema: str,
) -> None:
    # Arrange — restamping here would rewrite every migrated agent's age to
    # the migration moment, which is the one thing the column is read for.
    original = "2020-01-01T00:00:00Z"
    # Act
    put_instance_record(
        {
            "id": "imported-1",
            "name": "legacy",
            "host": "h1",
            "scope": "global",
            "started_at": original,
        }
    )
    # Assert
    assert last_known_instance("legacy")["started_at"] == original


def test_put_instance_record_is_run_twice_safe(pg_schema: str) -> None:
    # Arrange
    values = {
        "id": "imported-1",
        "name": "legacy",
        "host": "h1",
        "scope": "global",
        "started_at": "2020-01-01T00:00:00Z",
    }
    put_instance_record(values)
    # Act
    second = put_instance_record(values)
    # Assert
    assert second is False


def test_an_imported_record_can_still_be_retired(pg_schema: str) -> None:
    # Arrange — the import must not stamp ended_at=None, which would freeze
    # the IMMUTABLE field at None and make the record un-endable forever.
    put_instance_record(
        {
            "id": "imported-1",
            "name": "legacy",
            "host": "h1",
            "scope": "global",
            "started_at": "2020-01-01T00:00:00Z",
            "ended_at": None,
            "exit_reason": None,
        }
    )
    # Act
    ended = end_instance(
        "imported-1", ended_at="2026-08-28T00:00:00Z", exit_reason="gc-stale"
    )
    # Assert
    assert ended is True


def test_put_instance_record_refuses_a_record_with_no_id(pg_schema: str) -> None:
    # Arrange
    refused = None
    # Act
    try:
        put_instance_record({"name": "legacy", "host": "h1"})
    except ValueError as exc:
        refused = exc
    # Assert
    assert refused is not None
