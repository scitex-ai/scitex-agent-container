"""``instances`` on the shared PostgreSQL store (2026-08-28).

Replaces the family-tree-column tests these lines used to hold. Those
asserted the SQLite DDL and the ``ALTER TABLE`` migration that added
``bound_port`` / ``remote`` / ``spawned_by``; both are gone with the table,
so re-pointing them at the store would have kept the NAMES while asserting
something else. What survives is the BEHAVIOUR they were protecting, plus
the invariants the store made this module responsible for enforcing itself.

Three of those are worth naming, because each replaced a guarantee the
database used to give for free:

* ``UPDATE ... WHERE ended_at IS NULL`` did the refusing. ``put`` refuses
  nothing, so :func:`end_instance` reads first — and a missing id must be a
  ``False``, never an insert.
* ``ORDER BY started_at DESC, id DESC`` was in the SQL of two resolvers.
  ``started_at`` is second-resolution, so the ``id`` tiebreak decides which
  agent a message reaches when two starts share a second.
* An IMMUTABLE field freezes at its FIRST STAMPED VALUE, and writing
  ``None`` counts as a stamp. A start that wrote ``ended_at=None`` would
  make every later tombstone a silently rejected MergeConflict.

Needs a real PostgreSQL: ``pg_schema`` is the shared opt-in fixture, which
skips where no cluster exists and FAILS where a configured one is broken.
The three tests that assert SQLite no longer carries the table take no
fixture — they are about the absence, and an absence must be checkable on a
host with no database at all.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
from pathlib import Path

import pytest

from scitex_agent_container._state.state_db_instances import (
    end_instance,
    last_known_instance,
    list_active_instances,
    live_instance_for_name,
    read_instance,
    record_instance_activity,
    record_instance_start,
    record_instance_stop,
)


def seed_instance(instance_id: str, **values) -> str:
    """Write one record with a CHOSEN id and ``started_at``.

    ``record_instance_start`` mints its own uuid7 and stamps ``now``, and
    ``started_at`` is IMMUTABLE — so a test that needs two records ordered a
    particular way cannot start them and then back-date one. The rewrite is a
    silently rejected MergeConflict, and the test then passes on the ``id``
    tiebreak instead of on the property it names.

    This writes through the same ``Store.put`` the production writer uses, so
    it exercises the real schema; only the two values a test must control are
    supplied rather than generated.
    """
    from scitex_dev.store import NEW_RECORD

    from scitex_agent_container._state.state_db_instances_store import (
        ACTOR,
        run_with_reconnect,
        strip_unset,
    )

    payload = strip_unset(dict(values))
    payload.setdefault("remote", False)
    payload["id"] = instance_id
    run_with_reconnect(
        lambda store: store.put(payload, expected_revision=NEW_RECORD, actor=ACTOR)
    )
    return instance_id


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db location, exported via env (explicit save/restore).

    Still needed by the three absence tests: ``init_schema`` writes a real
    file and they read ``sqlite_master`` back out of it.
    """
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


# ---------------------------------------------------------------------------
# SQLite no longer carries the table — the absence, asserted.
# ---------------------------------------------------------------------------


def test_a_fresh_state_db_has_no_instances_table(db_path: Path) -> None:
    # Arrange — the fresh-DB proof. A leftover CREATE TABLE would give every
    # generic reader an EMPTY table to answer from, which reads as "no agent
    # has ever run here" rather than as "you are asking the wrong database".
    from scitex_agent_container._state.state_db import init_schema

    # Act
    init_schema()
    # Assert
    with sqlite3.connect(db_path) as conn:
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "instances" not in names


def test_known_tables_no_longer_offers_instances() -> None:
    # Arrange — every reader of this tuple is GENERIC (``table_counts``
    # behind ``sac db show``, export/import, the ``--table`` choice list), so
    # a name left behind would make ``sac db show`` print ``instances 0``.
    from scitex_agent_container._state.state_db import KNOWN_TABLES

    # Act
    offered = "instances" in KNOWN_TABLES
    # Assert
    assert offered is False


def test_the_legacy_family_tree_migration_is_gone() -> None:
    # Arrange — it ALTERed a table that no longer exists, so it could only
    # ever return early. A migration that can never fire is not a safety net;
    # it is a claim that a schema step still happens.
    import scitex_agent_container._state.state_db_migrations as migrations

    # Act
    present = hasattr(migrations, "migrate_instances_add_family_tree_cols")
    # Assert
    assert present is False


# ---------------------------------------------------------------------------
# the declared schema — what moved, what was dropped, what was folded
# ---------------------------------------------------------------------------


def test_the_declared_schema_carries_the_three_columns_production_reads() -> None:
    # Arrange — ``screen`` (restart verify), ``workdir`` (rewritten by the
    # rename verb) and ``remote`` (the authoritative locality flag) were
    # absent from the plan-era declaration while real code read them.
    from scitex_agent_container._state.state_db_instances_store import (
        instances_schema,
    )

    # Act
    fields = set(instances_schema().fields)
    # Assert
    assert {"screen", "workdir", "remote"} <= fields


def test_the_declared_schema_dropped_the_three_columns_nothing_reads() -> None:
    # Arrange — ``definition_id`` FKs a table nothing INSERTs into, ``scope``
    # was the literal 'global' on every row, ``ppid`` had no call site.
    from scitex_agent_container._state.state_db_instances_store import (
        instances_schema,
    )

    # Act
    fields = set(instances_schema().fields)
    # Assert
    assert fields.isdisjoint({"definition_id", "scope", "ppid"})


def test_bound_port_is_folded_rather_than_declared_twice() -> None:
    # Arrange — two columns holding one fact is how the two drift, and they
    # had: ``state_db_forward`` records a live row where the split answered
    # "where do I send this" two different ways.
    from scitex_agent_container._state.state_db_instances_store import (
        instances_schema,
    )

    # Act
    fields = set(instances_schema().fields)
    # Assert
    assert "bound_port" not in fields


def test_the_module_schema_matches_the_plugin_declaration_field_for_field() -> None:
    # Arrange — the plugin says what sac's rows MEAN and the opener is what
    # actually creates them. A drift between the two is invisible until a
    # merge resolves a field by a rule nobody declared.
    from scitex_agent_container._store_plugin import INSTANCES
    from scitex_agent_container._state.state_db_instances_store import (
        instances_schema,
    )

    opened = instances_schema()
    # Act
    drift = {
        name
        for name in set(INSTANCES.fields) | set(opened.fields)
        if INSTANCES.fields.get(name) != opened.fields.get(name)
    }
    # Assert
    assert drift == set()


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def test_a_start_records_the_port_under_both_keys(pg_schema: str) -> None:
    # Arrange — seven readers prefer ``bound_port``; the store keeps one
    # field and the codec mirrors it, so no reader changes shape.
    record_instance_start("alpha", host="host-a", a2a_port=8001)
    # Act
    row = last_known_instance("alpha")
    # Assert
    assert (row["a2a_port"], row["bound_port"]) == (8001, 8001)


def test_a_start_given_only_bound_port_still_records_an_address(
    pg_schema: str,
) -> None:
    # Arrange — the fold is COALESCE(a2a_port, bound_port), so a caller that
    # knows only the bound value still populates the one field.
    record_instance_start("alpha", host="host-a", bound_port=8123)
    # Act
    row = last_known_instance("alpha")
    # Assert
    assert row["a2a_port"] == 8123


def test_remote_defaults_to_zero(pg_schema: str) -> None:
    # Arrange — sqlite3.Row handed callers an int, and a test asserting == 1
    # is asserting the shape it was given; the move must not change it.
    record_instance_start("alpha", host="host-a")
    # Act
    row = last_known_instance("alpha")
    # Assert
    assert row["remote"] == 0


def test_a_cross_host_start_records_remote(pg_schema: str) -> None:
    # Arrange — the dispatcher's lead-side row. This flag, not a hostname
    # compare, is what five readers and the GC branch on.
    record_instance_start("alpha", host="peer-b", remote=True)
    # Act
    row = last_known_instance("alpha")
    # Assert
    assert row["remote"] == 1


def test_a_start_records_spawned_by(pg_schema: str) -> None:
    # Arrange — the lineage edge ``_lifecycle/_status`` reads.
    record_instance_start("alpha", host="host-a", spawned_by="lead")
    # Act
    row = last_known_instance("alpha")
    # Assert
    assert row["spawned_by"] == "lead"


def test_a_start_records_the_screen_the_restart_verifier_needs(
    pg_schema: str,
) -> None:
    # Arrange — without this column ``_restart_verify`` abstains on every
    # agent it is asked to verify.
    record_instance_start("alpha", host="host-a", screen="sac-alpha")
    # Act
    row = last_known_instance("alpha")
    # Assert
    assert row["screen"] == "sac-alpha"


def test_a_start_records_the_workdir_the_rename_verb_rewrites(
    pg_schema: str,
) -> None:
    # Arrange — never read back by production, but the rename verb EDITS it,
    # so dropping the field would have retired live coverage silently.
    record_instance_start("alpha", host="host-a", workdir="/home/u/proj/alpha")
    # Act
    row = last_known_instance("alpha")
    # Assert
    assert row["workdir"] == "/home/u/proj/alpha"


def test_a_dropped_column_is_not_re_emitted_as_none(pg_schema: str) -> None:
    # Arrange — a key present with a plausible NULL is exactly how a dropped
    # concept survives as a thing people write code against.
    record_instance_start("alpha", host="host-a")
    # Act
    row = last_known_instance("alpha")
    # Assert
    assert "scope" not in row


def test_a_dropped_parameter_is_a_typeerror_rather_than_ignored(
    pg_schema: str,
) -> None:
    # Arrange — a silently ignored keyword is how a caller comes to believe
    # it recorded something.
    rejected = False
    # Act
    try:
        record_instance_start("alpha", host="host-a", scope="global")
    except TypeError:
        rejected = True
    # Assert
    assert rejected is True


# ---------------------------------------------------------------------------
# stop — the refusal that moved out of the WHERE clause
# ---------------------------------------------------------------------------


def test_stopping_an_unknown_id_returns_false(pg_schema: str) -> None:
    # Arrange — the SQLite ``rowcount == 0``. A death with no recorded birth
    # is a real signal and must not be papered over.
    # Act
    ended = record_instance_stop("no-such-id")
    # Assert
    assert ended is False


def test_stopping_an_unknown_id_fabricates_no_record(pg_schema: str) -> None:
    # Arrange — ``put`` has no UPDATE-only form, so without the explicit
    # read-first this would INSERT a lifetime that never happened.
    record_instance_stop("no-such-id")
    # Act
    rows = list_active_instances()
    # Assert
    assert rows == []


def test_stopping_twice_returns_false_the_second_time(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start("alpha", host="host-a")
    record_instance_stop(instance_id)
    # Act
    again = record_instance_stop(instance_id, exit_reason="deleted")
    # Assert
    assert again is False


def test_a_second_stop_does_not_move_the_recorded_end(pg_schema: str) -> None:
    # Arrange — ``ended_at``/``exit_reason`` are IMMUTABLE, and the read-first
    # refusal means the second call never even attempts the write. Both
    # layers point the same way; this pins the OUTCOME.
    instance_id = record_instance_start("alpha", host="host-a")
    record_instance_stop(instance_id, exit_reason="stopped")
    # Act
    record_instance_stop(instance_id, exit_reason="deleted")
    # Assert
    assert read_instance(instance_id)["exit_reason"] == "stopped"


def test_a_stop_ends_the_record_rather_than_hiding_it(pg_schema: str) -> None:
    # Arrange — hiding would make the record read as ABSENT to
    # ``last_known_instance``, and three readers use an ended record as
    # evidence. "Existed and stopped" must stay different from "never was".
    instance_id = record_instance_start("alpha", host="host-a")
    record_instance_stop(instance_id)
    # Act
    row = last_known_instance("alpha")
    # Assert
    assert row is not None and row["ended_at"] is not None


def test_end_instance_writes_the_supplied_stamp_verbatim(pg_schema: str) -> None:
    # Arrange — the GC's reboot branch passes the BOOT time, which is the one
    # branch that can honestly name a moment of death.
    instance_id = record_instance_start("alpha", host="host-a")
    # Act
    end_instance(instance_id, exit_reason="reboot-swept", ended_at="2020-01-01T00:00:00Z")
    # Assert
    assert read_instance(instance_id)["ended_at"] == "2020-01-01T00:00:00Z"


def test_an_unrelated_later_write_does_not_freeze_ended_at(pg_schema: str) -> None:
    # Arrange — THE hazard this port had to measure. An IMMUTABLE field is
    # frozen by its first STAMPED value and writing None counts as a stamp,
    # so any payload carrying ``ended_at=None`` would make every later
    # tombstone a silently rejected MergeConflict. Every write path strips
    # unset fields; this is the pin.
    instance_id = record_instance_start("alpha", host="host-a")
    record_instance_activity(instance_id, ts="2026-01-01T00:00:00Z")
    # Act
    ended = record_instance_stop(instance_id)
    # Assert
    assert ended is True


# ---------------------------------------------------------------------------
# restart supersedes
# ---------------------------------------------------------------------------


def test_a_restart_mints_a_new_id(pg_schema: str) -> None:
    # Arrange
    first = record_instance_start("alpha", host="host-a", a2a_port=8001)
    record_instance_stop(first, exit_reason="restarted")
    # Act
    second = record_instance_start("alpha", host="host-a", a2a_port=8002)
    # Assert
    assert second != first


def test_after_a_restart_both_lifetimes_stay_readable(pg_schema: str) -> None:
    # Arrange — ``id`` is in the identity precisely so repeated observations
    # of one agent name stay SEPARATE lifetimes rather than overwriting.
    first = record_instance_start("alpha", host="host-a", a2a_port=8001)
    record_instance_stop(first, exit_reason="restarted")
    second = record_instance_start("alpha", host="host-a", a2a_port=8002)
    # Act
    both = (read_instance(first), read_instance(second))
    # Assert
    assert all(row is not None for row in both)


def test_after_a_restart_only_the_new_lifetime_is_active(pg_schema: str) -> None:
    # Arrange
    first = record_instance_start("alpha", host="host-a", a2a_port=8001)
    record_instance_stop(first, exit_reason="restarted")
    second = record_instance_start("alpha", host="host-a", a2a_port=8002)
    # Act
    active = [r["id"] for r in list_active_instances()]
    # Assert
    assert active == [second]


def test_after_a_restart_last_known_returns_the_newer_lifetime(
    pg_schema: str,
) -> None:
    # Arrange — the two lifetimes are SEEDED a second apart rather than
    # started back to back. MEASURED: ``new_uuid7`` falls back to uuid4 on
    # every Python before 3.14 (including the 3.12 in the sac image), so two
    # starts inside one second tie on ``started_at`` and the ``id`` tiebreak
    # resolves them RANDOMLY. Starting them back to back would therefore have
    # made this test a coin flip that happened to be green — see
    # ``test_the_id_tiebreak_is_deterministic_not_chronological`` below, which
    # is where that property is stated rather than accidentally relied upon.
    seed_instance(
        "first-lifetime",
        name="alpha",
        host="host-a",
        a2a_port=8001,
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:30Z",
        exit_reason="restarted",
    )
    seed_instance(
        "second-lifetime",
        name="alpha",
        host="host-a",
        a2a_port=8002,
        started_at="2026-01-01T00:00:31Z",
    )
    # Act
    row = last_known_instance("alpha")
    # Assert
    assert row["id"] == "second-lifetime"


def test_the_id_tiebreak_is_deterministic_not_chronological(
    pg_schema: str,
) -> None:
    # Arrange — the honest statement of what the tiebreak buys. Two records
    # sharing a ``started_at`` second must resolve the SAME WAY on every
    # call, which is what stops a resolver flapping between two live records
    # and sending consecutive messages to different agents. It does NOT
    # identify the newer one: ``new_uuid7`` is uuid4 below Python 3.14, so
    # the ids carry no time. Inherited from the SQLite ``ORDER BY started_at
    # DESC, id DESC``, not introduced here.
    same = "2026-01-01T00:00:00Z"
    seed_instance("id-aaa", name="alpha", host="host-a", started_at=same)
    seed_instance("id-zzz", name="alpha", host="host-a", started_at=same)
    # Act
    answers = {live_instance_for_name("alpha")["id"] for _ in range(3)}
    # Assert
    assert answers == {"id-zzz"}


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def test_list_active_instances_excludes_ended(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start("alpha", host="host-a")
    record_instance_stop(instance_id)
    # Act
    rows = list_active_instances()
    # Assert
    assert rows == []


def test_list_active_instances_filters_by_host(pg_schema: str) -> None:
    # Arrange — PER_HOST truth: two hosts' rows are different RECORDS, so the
    # filter is a real partition rather than a convenience.
    record_instance_start("alpha", host="host-a")
    record_instance_start("beta", host="host-b")
    # Act
    names = [r["name"] for r in list_active_instances(host="host-b")]
    # Assert
    assert names == ["beta"]


def test_list_active_instances_orders_newest_first(pg_schema: str) -> None:
    # Arrange — the SQLite ``ORDER BY started_at DESC``. Callers take
    # ``rows[0]`` as "the newest", so the order IS the contract.
    #
    # The two records are SEEDED with distinct ``started_at`` values rather
    # than started and then back-dated: ``started_at`` is IMMUTABLE, so a
    # rewrite is a silently rejected MergeConflict and the test would have
    # passed on the ``id`` tiebreak instead — green for the wrong reason,
    # which is the failure mode this whole port keeps running into.
    seed_instance("b-newer", name="alpha", host="host-a", started_at="2026-01-02T00:00:00Z")
    seed_instance("a-older", name="alpha", host="host-a", started_at="2026-01-01T00:00:00Z")
    # Act
    rows = list_active_instances()
    # Assert
    assert [r["id"] for r in rows] == ["b-newer", "a-older"]


def test_the_id_tiebreak_decides_a_same_second_tie(pg_schema: str) -> None:
    # Arrange — ``started_at`` is second-resolution, so two starts inside one
    # second are a genuine tie, and the tiebreak decides which agent a message
    # REACHES. uuid7 ids are time-ordered, which is what makes ``id DESC`` the
    # correct tiebreak rather than an arbitrary one. Seeded ids are ordered
    # lexically here for the same reason uuid7 is: to make the expectation
    # readable.
    same = "2026-01-01T00:00:00Z"
    seed_instance("id-aaa", name="alpha", host="host-a", started_at=same, a2a_port=8001)
    seed_instance("id-zzz", name="alpha", host="host-a", started_at=same, a2a_port=8002)
    # Act
    row = live_instance_for_name("alpha")
    # Assert
    assert row["id"] == "id-zzz"


def test_both_resolvers_break_a_same_second_tie_the_same_way(
    pg_schema: str,
) -> None:
    # Arrange — ``resolve_node_host`` (locality) and
    # ``resolve_forward_target`` (addressability) answer DIFFERENT questions,
    # and that difference is deliberate. They must still be looking at the
    # SAME record: the asymmetry between them is what
    # ``state_db_forward``'s docstring records as a live routing defect.
    from scitex_agent_container._state.state_db_forward import (
        resolve_forward_target,
    )
    from scitex_agent_container._state.state_db_nodes import resolve_node_host

    same = "2026-01-01T00:00:00Z"
    seed_instance("id-aaa", name="alpha", host="host-a", started_at=same, a2a_port=8001)
    seed_instance("id-zzz", name="alpha", host="host-z", started_at=same, a2a_port=8002)
    # Act
    answers = (resolve_node_host(name="alpha"), resolve_forward_target(name="alpha"))
    # Assert
    assert answers[0] == answers[1] == {"host": "host-z", "a2a_port": 8002}


def test_last_known_instance_returns_none_for_an_unknown_agent(
    pg_schema: str,
) -> None:
    # Arrange — ``None`` means "never observed at all", the one answer three
    # readers must not be handed by mistake.
    record_instance_start("alpha", host="host-a")
    # Act
    row = last_known_instance("ghost")
    # Assert
    assert row is None


def test_last_known_instance_answers_from_an_ended_record(pg_schema: str) -> None:
    # Arrange — the #192 fail-loud evidence path.
    instance_id = record_instance_start("alpha", host="host-a")
    record_instance_stop(instance_id, exit_reason="stopped")
    # Act
    row = last_known_instance("alpha")
    # Assert
    assert row["exit_reason"] == "stopped"


def test_live_instance_for_name_ignores_an_ended_record(pg_schema: str) -> None:
    # Arrange — the resolvers' reader. An ended record is evidence, not an
    # address; routing to one sends messages nowhere.
    instance_id = record_instance_start("alpha", host="host-a", a2a_port=8001)
    record_instance_stop(instance_id)
    # Act
    row = live_instance_for_name("alpha")
    # Assert
    assert row is None


def test_record_instance_activity_refuses_an_unknown_id(pg_schema: str) -> None:
    # Arrange — the same refusal ``end_instance`` makes, for the same reason.
    # Act
    touched = record_instance_activity("no-such-id", ts="2026-01-01T00:00:00Z")
    # Assert
    assert touched is False


def test_record_instance_activity_never_rewinds_the_heartbeat(
    pg_schema: str,
) -> None:
    # Arrange — MergeRule.MAX replaced COALESCE, and the difference is the
    # high-water mark: a sample that arrives LATE must not resurrect or
    # rewind a live agent.
    instance_id = record_instance_start("alpha", host="host-a")
    record_instance_activity(instance_id, ts="2026-01-02T00:00:00Z")
    # Act
    record_instance_activity(instance_id, ts="2026-01-01T00:00:00Z")
    # Assert
    assert read_instance(instance_id)["last_heartbeat_at"] == "2026-01-02T00:00:00Z"
