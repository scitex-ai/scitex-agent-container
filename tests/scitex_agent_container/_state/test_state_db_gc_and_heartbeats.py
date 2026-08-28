"""``update_heartbeat`` and ``gc_dead_instances`` — across two engines.

Extracted from ``test_state_db.py`` on 2026-08-28 alongside
``test_state_db_lifecycle_writes.py``; see that file for the split's
reasoning. What lands HERE is the part that is not purely one backend:

  * ``update_heartbeat`` is the ONE function that now writes to BOTH
    engines — the ``instance_heartbeats`` time series is still SQLite,
    the rolling cache on the instances record is PostgreSQL. Its tests
    take both fixtures, which is the honest shape of what it does.
  * ``gc_dead_instances`` retires a lease whose pid is gone, and the
    ``sac db clean / tick`` CLI drives it.

WHAT THE MIGRATION KILLED: the four ``update_heartbeat`` tests that
asserted against ``SELECT * FROM instances`` now read
``list_active_instances()``. The COALESCE property they were written for
is unchanged — ``Store.put`` is a partial update, so omitting a field IS
the coalesce.

PA-306: no mocks. Isolation is a throwaway PostgreSQL schema plus a real
on-disk SQLite file under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db location, exported via env so the CLI picks it up.

    Still needed even though the instances records have left: the
    ``instance_heartbeats`` half of ``update_heartbeat`` is SQLite, and
    the ``sac db`` CLI opens the file whatever else it touches.

    PA-306: explicit env save/restore (no monkeypatch fixture).
    """
    import importlib

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
# update_heartbeat — the one writer that now spans two engines
# ---------------------------------------------------------------------------


def test_update_heartbeat_collapses_two_same_second_beats_into_one_row(
    pg_schema: str,
    db_path: Path,
):
    # Arrange — pin both beats to one ``ts`` via the now_fn seam so the
    # same-second collapse is deterministic (not a wall-clock coincidence).
    from scitex_agent_container._state.state_db import (
        open_db,
        record_instance_start,
        update_heartbeat,
    )

    iid = record_instance_start("x", host="h")
    fixed_ts = "2026-05-22T00:00:00Z"
    # Act
    update_heartbeat(
        iid, iter=1, input_tokens=10, output_tokens=20, now_fn=lambda: fixed_ts
    )
    update_heartbeat(
        iid, iter=2, input_tokens=30, output_tokens=40, now_fn=lambda: fixed_ts
    )
    with open_db() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM instance_heartbeats WHERE instance_id=?",
            (iid,),
        ).fetchone()["n"]
    # Assert — single row thanks to ON CONFLICT (instance_id, ts@1-sec).
    assert n == 1


def test_update_heartbeat_keeps_two_rows_when_beats_straddle_a_second(
    pg_schema: str,
    db_path: Path,
):
    # Arrange — distinct ``ts`` (clock ticked between beats) → no merge.
    from scitex_agent_container._state.state_db import (
        open_db,
        record_instance_start,
        update_heartbeat,
    )

    iid = record_instance_start("x", host="h")
    # Act
    update_heartbeat(iid, iter=1, now_fn=lambda: "2026-05-22T00:00:00Z")
    update_heartbeat(iid, iter=2, now_fn=lambda: "2026-05-22T00:00:01Z")
    with open_db() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM instance_heartbeats WHERE instance_id=?",
            (iid,),
        ).fetchone()["n"]
    # Assert — two rows; "latest" stays unambiguous via the seq PK.
    assert n == 2


@pytest.mark.parametrize(
    "column, expected",
    [("iter", 2), ("input_tokens", 30), ("output_tokens", 40)],
)
@pytest.mark.parametrize(
    "second_ts",
    ["2026-05-22T00:00:00Z", "2026-05-22T00:00:01Z"],
    ids=["same-second", "straddle-second"],
)
def test_update_heartbeat_latest_row_holds_latest_value(
    pg_schema: str, db_path: Path, column: str, expected: int, second_ts: str
):
    # Arrange — exercise BOTH the merge path (same ts) and the straddle
    # path (clock ticked); "latest" must resolve to the iter=2 beat in
    # either case. ``ts`` is pinned via now_fn so the test is
    # deterministic instead of racing the wall clock.
    from scitex_agent_container._state.state_db import (
        latest_instance_heartbeat,
        record_instance_start,
        update_heartbeat,
    )

    iid = record_instance_start("x", host="h")
    update_heartbeat(
        iid,
        iter=1,
        input_tokens=10,
        output_tokens=20,
        now_fn=lambda: "2026-05-22T00:00:00Z",
    )
    update_heartbeat(
        iid,
        iter=2,
        input_tokens=30,
        output_tokens=40,
        now_fn=lambda: second_ts,
    )
    # Act — latest is MAX(seq), not an arbitrary fetchone().
    hb_row = latest_instance_heartbeat(iid)
    # Assert
    assert hb_row[column] == expected


@pytest.mark.parametrize(
    "field, expected",
    [
        ("iter_count", 2),
        ("input_tokens", 30),
        ("output_tokens", 40),
    ],
)
def test_update_heartbeat_caches_rolling_value_on_the_instance_record(
    pg_schema: str, db_path: Path, field: str, expected: int
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
        update_heartbeat,
    )

    iid = record_instance_start("x", host="h")
    update_heartbeat(iid, iter=1, input_tokens=10, output_tokens=20)
    update_heartbeat(iid, iter=2, input_tokens=30, output_tokens=40)
    # Act
    inst = list_active_instances()[0]
    # Assert
    assert inst[field] == expected


def test_update_heartbeat_caches_the_last_heartbeat_stamp(
    pg_schema: str,
    db_path: Path,
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
        update_heartbeat,
    )

    iid = record_instance_start("x", host="h")
    update_heartbeat(iid, iter=1, input_tokens=10, output_tokens=20)
    # Act
    inst = list_active_instances()[0]
    # Assert
    assert inst["last_heartbeat_at"] is not None


@pytest.mark.parametrize(
    "field, expected",
    [
        ("iter_count", 5),
        ("input_tokens", 100),
        ("output_tokens", 200),
    ],
)
def test_update_heartbeat_partial_update_preserves_the_previous_field(
    pg_schema: str, db_path: Path, field: str, expected: int
):
    # Arrange — the SQLite statement spelled this COALESCE(?, col); a
    # partial ``Store.put`` spells it by omitting the field. Same property,
    # and it is the one most likely to be lost in translation.
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
        update_heartbeat,
    )

    iid = record_instance_start("x", host="h")
    update_heartbeat(iid, iter=5, input_tokens=100, output_tokens=200)
    # Act — second call only updates pane_state; other fields must NOT reset.
    update_heartbeat(iid, pane_state="alive")
    inst = list_active_instances()[0]
    # Assert
    assert inst[field] == expected


# ---------------------------------------------------------------------------
# gc_dead_instances — a local pid that no longer exists is reaped.
# Shared env+module-attribute save/restore via a fixture (PA-306 pattern).
# ---------------------------------------------------------------------------


@pytest.fixture
def dead_pid_environment():
    """Pin host + suppress proc-btime so a fake pid reaps as absent.

    PA-306: explicit env / module attribute save/restore, no monkeypatch.
    """
    from scitex_agent_container._state import state_db

    saved_host = os.environ.get("SAC_HOST")
    saved_btime = state_db._proc_btime
    os.environ["SAC_HOST"] = "test-host"
    state_db._proc_btime = lambda: None  # type: ignore[assignment]
    try:
        yield
    finally:
        state_db._proc_btime = saved_btime  # type: ignore[assignment]
        if saved_host is None:
            os.environ.pop("SAC_HOST", None)
        else:
            os.environ["SAC_HOST"] = saved_host


def test_gc_reports_at_least_one_crashed_for_a_dead_local_pid(
    pg_schema: str, dead_pid_environment
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        gc_dead_instances,
        record_instance_start,
    )

    record_instance_start("dead-agent", pid=999_999_999, host="test-host")
    # Act
    counters = gc_dead_instances()
    # Assert
    assert counters["crashed"] >= 1


def test_gc_removes_a_dead_instance_from_the_active_list(
    pg_schema: str, dead_pid_environment
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        gc_dead_instances,
        list_active_instances,
        record_instance_start,
    )

    record_instance_start("dead-agent", pid=999_999_999, host="test-host")
    # Act
    gc_dead_instances()
    # Assert
    assert list_active_instances(host="test-host") == []


def test_gc_persists_the_exit_reason_on_the_instance_record(
    pg_schema: str, dead_pid_environment
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        gc_dead_instances,
        last_known_instance,
        record_instance_start,
    )

    record_instance_start("dead-agent", pid=999_999_999, host="test-host")
    # Act
    gc_dead_instances()
    # Assert
    assert last_known_instance("dead-agent")["exit_reason"] == "pid_absent_at_sweep"


def test_the_sweep_writes_a_reason_naming_the_check_not_a_cause(
    pg_schema: str, dead_pid_environment
):
    """The value must not assert a fate the check never established.

    ``os.kill(pid, 0)`` raising ESRCH supports exactly one claim: the pid was
    absent when we looked. The old value said ``crashed``, and three readers
    believed it — reasoning about what could kill eleven processes in one
    second when nothing had.
    """
    # Arrange
    from scitex_agent_container._state.state_db import (
        gc_dead_instances,
        last_known_instance,
        record_instance_start,
    )

    record_instance_start("dead-agent", pid=999_999_999, host="test-host")
    # Act
    gc_dead_instances()
    # Assert
    assert "crashed" not in last_known_instance("dead-agent")["exit_reason"]


def test_one_sweep_stamps_every_reaped_record_with_the_same_ended_at(
    pg_schema: str, dead_pid_environment
):
    """THE TRAP, pinned: a shared second is the sweep's clock, not a co-death.

    Measured 2026-08-12 — eleven rows shared ``17:54:26Z`` and were read as a
    simultaneous kill. They had died 10h46m earlier, at different moments.
    This test exists so nobody can "fix" the shared timestamp by accident and
    so the behaviour is documented as intended rather than incidental.
    """
    # Arrange
    from scitex_agent_container._state.state_db import (
        all_instances,
        gc_dead_instances,
        record_instance_start,
    )

    for name in ("dead-a", "dead-b", "dead-c"):
        record_instance_start(name, pid=999_999_999, host="test-host")
    # Act
    gc_dead_instances()
    # Assert
    stamps = {
        r["ended_at"]
        for r in all_instances()
        if r["exit_reason"] == "pid_absent_at_sweep"
    }
    assert len(stamps) == 1


def test_a_dry_run_sweep_leaves_the_lease_alone(
    pg_schema: str, dead_pid_environment
):
    # Arrange — the counters must report what WOULD be swept without
    # un-leasing a record; a dry run that retires an agent is not a dry run.
    from scitex_agent_container._state.state_db import (
        gc_dead_instances,
        list_active_instances,
        record_instance_start,
    )

    record_instance_start("dead-agent", pid=999_999_999, host="test-host")
    # Act
    gc_dead_instances(dry_run=True)
    # Assert
    assert len(list_active_instances(host="test-host")) == 1


def test_db_clean_via_cli_reports_at_least_one_crashed_in_json_body(
    pg_schema: str, db_path: Path, dead_pid_environment
):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container.cli_pkg.db_group import db_clean

    record_instance_start("dead", pid=999_999_999, host="test-host")
    runner = CliRunner()
    # Act
    result = runner.invoke(db_clean, ["--json"])
    body = json.loads(result.stdout)
    # Assert
    assert body["crashed"] >= 1


def test_db_tick_via_cli_emits_no_human_readable_output_on_success(
    pg_schema: str,
    db_path: Path,
):
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_tick

    runner = CliRunner()
    # Act
    result = runner.invoke(db_tick, [])
    # Assert — tick is silent on success: exit 0 AND empty stdout.
    assert (result.exit_code, result.output) == (0, "")
