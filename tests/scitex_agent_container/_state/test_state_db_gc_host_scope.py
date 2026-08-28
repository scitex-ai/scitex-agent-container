"""The GC sweep on the shared store — and the branch that had to be scoped.

THE HAZARD THIS FILE EXISTS FOR
===============================
Under per-host SQLite the heartbeat-staleness branch scoped ONLY by
``remote=0``, and that was sufficient BY ACCIDENT: each host owned its own
file, so "not a cross-host mirror row" also meant "written here". The move to
the SHARED store removes both halves of that accident — a peer's own local
row is ``remote=0`` too, and every host can now see it.

Left unscoped, one host running ``sac db tick`` would tombstone LIVE agents
FLEET-WIDE, judging their heartbeat freshness against a rule it has no
standing to apply. Nothing would raise: the victims simply stop being
returned by ``list_active_instances``, which every reader takes as "not
running", and ``sac agents send`` starts answering "agent not running" for
agents that are fine.

So the branch now carries ``host == canonical_host`` as well, and keeps
``remote`` — they are different claims (``remote`` is written by the
dispatcher, ``host`` by the observer) and losing either would be a silent
widening.

WHAT THE SWEEP WRITES, AND WHAT IT DELIBERATELY DOES NOT
========================================================
It fills ``ended_at``/``exit_reason`` — the FIRST fill of two IMMUTABLE
fields, which the store permits because immutability starts once there IS a
value. It never ``hide()``s. A hidden record reads as ABSENT, and
``last_known_instance`` returning an ended record is the evidence
``_reconcile/_rule`` and ``_restart_verify`` decide from.

Needs a real PostgreSQL: ``pg_schema`` is the shared opt-in fixture. The
host under test is derived (``f"not-{canonical}"``) rather than written as a
literal, because a literal hostname self-defeats on the runner that happens
to be named that.
"""

from __future__ import annotations

import os

from scitex_agent_container._state.state_db_gc import (
    EXIT_REASON_PID_ABSENT_AT_SWEEP,
    gc_dead_instances,
)
from scitex_agent_container._state.state_db_hostname import resolve_host
from scitex_agent_container._state.state_db_instances import (
    list_active_instances,
    read_instance,
    record_instance_activity,
    record_instance_start,
)


def _local() -> str:
    return resolve_host(None)


def _foreign() -> str:
    return f"not-{_local()}"


def _stale_ts() -> str:
    return "2000-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# branch 3 — heartbeat staleness, and the host scope that had to be added
# ---------------------------------------------------------------------------


def test_a_stale_local_record_is_swept(pg_schema: str) -> None:
    # Arrange — the branch working as intended on its own host.
    instance_id = record_instance_start("alpha", host=_local(), pid=os.getpid())
    record_instance_activity(instance_id, ts=_stale_ts())
    # Act
    counters = gc_dead_instances(heartbeat_stale_seconds=60)
    # Assert
    assert counters["gc_stale"] == 1


def test_a_stale_local_record_is_marked_gc_stale(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start("alpha", host=_local(), pid=os.getpid())
    record_instance_activity(instance_id, ts=_stale_ts())
    # Act
    gc_dead_instances(heartbeat_stale_seconds=60)
    # Assert
    assert read_instance(instance_id)["exit_reason"] == "gc-stale"


def test_a_stale_record_on_another_host_is_not_swept(pg_schema: str) -> None:
    # Arrange — THE PIN. Under per-host SQLite this row was unreachable; on
    # the shared store it is one scan away, and it is ``remote=0`` because
    # the peer wrote it about ITSELF. Sweeping it tombstones a live agent on
    # a machine this sweep has never looked at.
    instance_id = record_instance_start("beta", host=_foreign(), pid=os.getpid())
    record_instance_activity(instance_id, ts=_stale_ts())
    # Act
    gc_dead_instances(heartbeat_stale_seconds=60)
    # Assert
    assert read_instance(instance_id)["ended_at"] is None


def test_a_stale_record_on_another_host_is_not_counted(pg_schema: str) -> None:
    # Arrange — the counter must agree with the write. A sweep that reported
    # 1 while writing 0 (or the reverse) is the "success value that is also
    # the didn't-happen value" this module's docstring is about.
    instance_id = record_instance_start("beta", host=_foreign(), pid=os.getpid())
    record_instance_activity(instance_id, ts=_stale_ts())
    # Act
    counters = gc_dead_instances(heartbeat_stale_seconds=60)
    # Assert
    assert counters["gc_stale"] == 0


def test_a_stale_remote_record_on_this_host_is_not_swept(pg_schema: str) -> None:
    # Arrange — the ORIGINAL guard, kept. The lead writes ``remote=1`` rows
    # naming a peer; we have no local liveness signal for those agents, so
    # ``sac agents list`` ssh-probes the peer rather than tombstoning here.
    instance_id = record_instance_start("beta", host=_local(), remote=True)
    record_instance_activity(instance_id, ts=_stale_ts())
    # Act
    gc_dead_instances(heartbeat_stale_seconds=60)
    # Assert
    assert read_instance(instance_id)["ended_at"] is None


def test_a_fresh_local_record_is_not_swept(pg_schema: str) -> None:
    # Arrange — the negative control: the branch must not sweep on the mere
    # presence of a heartbeat.
    instance_id = record_instance_start("alpha", host=_local(), pid=os.getpid())
    record_instance_activity(instance_id, ts="2999-01-01T00:00:00Z")
    # Act
    gc_dead_instances(heartbeat_stale_seconds=60)
    # Assert
    assert read_instance(instance_id)["ended_at"] is None


def test_a_record_with_no_heartbeat_is_not_swept_by_this_branch(
    pg_schema: str,
) -> None:
    # Arrange — a NULL heartbeat is "we have no sample", not "the sample is
    # old". The SQLite branch said ``last_heartbeat_at IS NOT NULL``.
    instance_id = record_instance_start("alpha", host=_local(), pid=os.getpid())
    # Act
    counters = gc_dead_instances(heartbeat_stale_seconds=60)
    # Assert
    assert counters["gc_stale"] == 0


def test_a_malformed_heartbeat_is_tolerated_rather_than_swept(
    pg_schema: str,
) -> None:
    # Arrange — an unparseable stamp is not evidence of death.
    instance_id = record_instance_start("alpha", host=_local(), pid=os.getpid())
    record_instance_activity(instance_id, ts="not-a-timestamp")
    # Act
    gc_dead_instances(heartbeat_stale_seconds=60)
    # Assert
    assert read_instance(instance_id)["ended_at"] is None


# ---------------------------------------------------------------------------
# branch 2 — pid liveness
# ---------------------------------------------------------------------------


def test_a_local_record_whose_pid_is_gone_is_swept(pg_schema: str) -> None:
    # Arrange — pid 2**22 - 1 is above the default pid_max on Linux, so it
    # cannot name a live process; asking about it is the check this branch
    # performs, and nothing else.
    instance_id = record_instance_start("alpha", host=_local(), pid=4194303)
    # Act
    gc_dead_instances()
    # Assert
    assert (
        read_instance(instance_id)["exit_reason"] == EXIT_REASON_PID_ABSENT_AT_SWEEP
    )


def test_the_pid_branch_ignores_another_hosts_record(pg_schema: str) -> None:
    # Arrange — a peer's pid number means nothing in THIS pid namespace, and
    # probing it locally could vouch for a dead agent as alive (or, here,
    # tombstone a live one). Host-scoped by construction.
    instance_id = record_instance_start("beta", host=_foreign(), pid=4194303)
    # Act
    gc_dead_instances()
    # Assert
    assert read_instance(instance_id)["ended_at"] is None


def test_a_live_pid_is_not_swept(pg_schema: str) -> None:
    # Arrange — the negative control, using this very process.
    instance_id = record_instance_start("alpha", host=_local(), pid=os.getpid())
    # Act
    gc_dead_instances()
    # Assert
    assert read_instance(instance_id)["ended_at"] is None


def test_a_null_pid_is_not_swept(pg_schema: str) -> None:
    # Arrange — the three cross-host writers leave ``pid`` NULL deliberately.
    # "No pid recorded" is not "the pid is gone".
    instance_id = record_instance_start("alpha", host=_local())
    # Act
    gc_dead_instances()
    # Assert
    assert read_instance(instance_id)["ended_at"] is None


def test_the_crashed_alias_carries_the_same_number(pg_schema: str) -> None:
    # Arrange — a DEPRECATED alias kept so a consumer reading the old key
    # keeps working rather than silently seeing zero swept rows.
    record_instance_start("alpha", host=_local(), pid=4194303)
    # Act
    counters = gc_dead_instances()
    # Assert
    assert counters["crashed"] == counters[EXIT_REASON_PID_ABSENT_AT_SWEEP]


# ---------------------------------------------------------------------------
# branch 1 — boot epoch
# ---------------------------------------------------------------------------


def _seed(instance_id: str, **values) -> str:
    """Write one record with a CHOSEN ``started_at``.

    ``record_instance_start`` stamps ``now``, and ``started_at`` is IMMUTABLE
    — a back-date is a silently rejected MergeConflict, so a test that
    started a record and then rewrote the stamp would assert nothing. Written
    through the production ``Store.put`` with the production schema; only the
    values the branch is about are supplied.
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


def test_a_record_started_before_boot_is_reboot_swept(pg_schema: str) -> None:
    # Arrange — ``started_at`` in the year 2000 predates any live host's
    # boot, so this branch fires wherever /proc/stat exists.
    from scitex_agent_container._state.state_db_gc import _proc_btime

    if _proc_btime() is None:
        # No boot detection on this platform; the branch is a documented
        # no-op there, so there is nothing to assert about it.
        return
    _seed(
        "old-lifetime",
        name="alpha",
        host=_local(),
        started_at="2000-01-01T00:00:00Z",
    )
    # Act
    gc_dead_instances()
    # Assert
    assert read_instance("old-lifetime")["exit_reason"] == "reboot-swept"


def test_the_reboot_branch_ignores_another_hosts_record(pg_schema: str) -> None:
    # Arrange — THIS host's boot time says nothing about when a PEER last
    # rebooted, and on the shared store the peer's row is one scan away.
    from scitex_agent_container._state.state_db_gc import _proc_btime

    if _proc_btime() is None:
        return
    _seed(
        "peer-lifetime",
        name="beta",
        host=_foreign(),
        started_at="2000-01-01T00:00:00Z",
    )
    # Act
    gc_dead_instances()
    # Assert
    assert read_instance("peer-lifetime")["ended_at"] is None


def test_the_reboot_branch_stamps_the_boot_time_not_the_sweep_time(
    pg_schema: str,
) -> None:
    # Arrange — the ONE branch that can honestly name a moment of death: the
    # reboot IS when every one of these processes stopped existing. The other
    # two write the moment we LOOKED, which is why their ``exit_reason`` says
    # "at sweep".
    from scitex_agent_container._state.state_db_gc import _proc_btime

    boot = _proc_btime()
    if boot is None:
        return
    _seed(
        "old-lifetime",
        name="alpha",
        host=_local(),
        started_at="2000-01-01T00:00:00Z",
    )
    # Act
    gc_dead_instances()
    # Assert
    assert read_instance("old-lifetime")["ended_at"] == boot


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


def test_a_dry_run_counts_without_writing(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start("alpha", host=_local(), pid=4194303)
    # Act
    counters = gc_dead_instances(dry_run=True)
    # Assert
    assert counters[EXIT_REASON_PID_ABSENT_AT_SWEEP] == 1


def test_a_dry_run_leaves_the_record_active(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start("alpha", host=_local(), pid=4194303)
    # Act
    gc_dead_instances(dry_run=True)
    # Assert
    assert read_instance(instance_id)["ended_at"] is None


def test_a_swept_record_leaves_the_active_listing(pg_schema: str) -> None:
    # Arrange — the end-to-end consequence: this is what every reader sees.
    record_instance_start("alpha", host=_local(), pid=4194303)
    # Act
    gc_dead_instances()
    # Assert
    assert list_active_instances() == []


def test_a_swept_record_is_still_readable_as_evidence(pg_schema: str) -> None:
    # Arrange — the sweep TOMBSTONES, it does not hide. ``_reconcile/_rule``
    # reads ``exit_reason`` off this record to decide whether to restart, and
    # a missing record is a DIFFERENT verdict (NEVER_STARTED).
    record_instance_start("alpha", host=_local(), pid=4194303)
    # Act
    gc_dead_instances()
    # Assert
    assert last_known("alpha") is not None


def last_known(name: str):
    from scitex_agent_container._state.state_db_instances import last_known_instance

    return last_known_instance(name)
