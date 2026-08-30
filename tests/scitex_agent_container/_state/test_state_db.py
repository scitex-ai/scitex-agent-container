"""Tests for scitex_agent_container._state.state_db (F-CS11).

Covers:
- import_legacy_registry: lifts JSON shards into ``instances`` rows
  with ``exit_reason='reboot-swept'``; idempotent on re-run; tolerates
  malformed shards.
- ``sac db migrate`` / ``clean`` / ``tick`` end-to-end via CliRunner.
- record_instance_start / _stop and gc_dead_instances, through the
  PostgreSQL instance store.

WHAT THIS FILE STOPPED COVERING, AND WHY THE TESTS WENT RATHER THAN THE
ASSERTIONS: ``init_schema`` / ``table_counts`` were exercised here until the
storage engine was deleted from ``state_db``. Both had already been hollowed
out — the two table-by-table cases were parametrized over ``KNOWN_TABLES``,
which had shrunk to an empty tuple, so pytest was collecting ZERO cases from
them and reporting the file green on the strength of tests that no longer
existed. The remaining two asserted that a file appeared on disk and that
calling the creator twice did not raise. Neither is a fact about sac any
more; there is no file and no creator.

``sac db show`` and ``sac db query`` were covered here until 2026-08-29, when
both verbs were deleted with the read surface they wrapped.

TQ cleanup (state_db slice): every test carries AAA markers and exactly
one assertion. Test names spell out the behaviour being verified
(TQ003-compatible).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.state_db import import_legacy_registry


@pytest.fixture
def db_path(tmp_path: Path, pg_schema: str):
    """Isolated state.db location, exported via env so the CLI picks it up.

    PA-306: explicit env save/restore (no monkeypatch fixture).
    """
    import os

    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    # Reload the module-level constant without bouncing the import.
    import importlib

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
# init_schema / table_counts
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# import_legacy_registry — legacy JSON shards → ``instances`` rows
# ---------------------------------------------------------------------------


def _write_legacy_shard(reg: Path, name: str, **overrides) -> None:
    reg.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "config": f"/dev/null/{name}.yaml",
        "pid": 1234,
        "started_at": "2026-05-05T03:29:41Z",
        "screen": name,
    }
    payload.update(overrides)
    (reg / f"{name}.json").write_text(json.dumps(payload))


def test_import_legacy_registry_returns_imported_count_for_two_shards(
    db_path: Path, tmp_path: Path
):
    # Arrange
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "polish-clew")
    _write_legacy_shard(reg, "polish-sac", started_at="2026-05-05T03:29:42Z")
    # Act
    result = import_legacy_registry(reg, host="ywata-note-win")
    # Assert
    assert result == {"imported": 2, "skipped": 0}


def _swept_rows() -> list[dict]:
    """Every migrated legacy row, read through the PRODUCTION reader.

    ``instances`` moved to the shared store on 2026-08-28, so there is no
    table to SELECT from — and reading back through the reader the
    application uses is the stronger check anyway: a write the store accepted
    but cannot serve counts as absent here, which a raw SELECT could not
    catch.
    """
    from scitex_agent_container._state.state_db_instances import scan_instances
    from scitex_agent_container._state.state_db_instances_store import (
        instance_as_dict,
        run_with_reconnect,
    )

    return [instance_as_dict(r) for r in run_with_reconnect(scan_instances)]


def test_import_legacy_registry_writes_one_instances_row_per_shard(
    db_path: Path, tmp_path: Path
):
    # Arrange
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "polish-clew")
    _write_legacy_shard(reg, "polish-sac")
    # Act
    import_legacy_registry(reg, host="ywata-note-win")
    rows = _swept_rows()
    # Assert
    assert {r["name"] for r in rows} == {"polish-clew", "polish-sac"}


@pytest.mark.parametrize(
    "field, expected",
    [
        ("host", "ywata-note-win"),
        ("exit_reason", "reboot-swept"),
    ],
)
def test_import_legacy_registry_sets_invariant_field_on_swept_rows(
    db_path: Path, tmp_path: Path, field: str, expected: str
):
    # Arrange
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "polish-clew")
    # Act
    import_legacy_registry(reg, host="ywata-note-win")
    row = _swept_rows()[0]
    # Assert
    assert row[field] == expected


@pytest.mark.parametrize("field", ["started_at", "ended_at"])
def test_import_legacy_registry_populates_timestamp_field_on_swept_rows(
    db_path: Path, tmp_path: Path, field: str
):
    # Arrange
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "polish-clew")
    # Act
    import_legacy_registry(reg, host="ywata-note-win")
    row = _swept_rows()[0]
    # Assert
    assert row[field]


def test_import_legacy_registry_second_run_imports_zero_new_shards(
    db_path: Path, tmp_path: Path
):
    # Arrange
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "polish-clew")
    import_legacy_registry(reg, host="ywata-note-win")
    # Act
    second = import_legacy_registry(reg, host="ywata-note-win")
    # Assert
    assert second == {"imported": 0, "skipped": 1}


def test_import_legacy_registry_skips_malformed_json_and_imports_valid_shard(
    db_path: Path, tmp_path: Path
):
    # Arrange
    reg = tmp_path / "registry"
    reg.mkdir()
    (reg / "bad.json").write_text("not json {")
    _write_legacy_shard(reg, "good")
    # Act
    result = import_legacy_registry(reg, host="h")
    # Assert
    assert result == {"imported": 1, "skipped": 1}


def test_import_legacy_registry_returns_zero_counts_when_registry_dir_missing(
    db_path: Path, tmp_path: Path
):
    # Arrange
    missing = tmp_path / "does-not-exist"
    # Act
    result = import_legacy_registry(missing, host="h")
    # Assert
    assert result == {"imported": 0, "skipped": 0}


# ---------------------------------------------------------------------------
# CLI surface — `sac db migrate`
# ---------------------------------------------------------------------------


def test_db_migrate_via_cli_reports_one_imported_for_single_shard(
    db_path: Path, tmp_path: Path
):
    # Arrange
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "diag-test")
    from scitex_agent_container.cli_pkg.db_group import db_migrate

    runner = CliRunner()
    # Act
    result = runner.invoke(
        db_migrate,
        ["--registry-dir", str(reg), "--host", "ywata-note-win", "--json"],
    )
    body = json.loads(result.stdout)
    # Assert
    assert body["imported"] == 1


def test_the_swept_row_is_readable_through_the_production_reader(
    db_path: Path, tmp_path: Path
):
    """The other half of the refusal above: the ROW is still there.

    Refusing ``--table instances`` would be worthless if the data had gone
    with the table. It has not — ``import_legacy_registry`` writes it into
    the store, and this reads it back through the reader the application
    uses. Both halves are needed: the refusal alone would pass on a
    migration that lost every row.
    """
    # Arrange
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "diag-test")
    import_legacy_registry(reg, host="h")
    # Act
    rows = _swept_rows()
    # Assert
    assert [r["exit_reason"] for r in rows] == ["reboot-swept"]


# ---------------------------------------------------------------------------
# F-CS11 phase 2 — write helpers + gc_dead_instances
# ---------------------------------------------------------------------------


def test_record_instance_start_returns_uuid_like_string(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    # Act
    iid = record_instance_start(
        "diag-test", pid=1234, screen="diag-test", host="ywata-note-win"
    )
    # Assert
    assert iid and len(iid) >= 32


def test_record_instance_start_inserts_single_active_instance_row(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    # Act
    record_instance_start(
        "diag-test", pid=1234, screen="diag-test", host="ywata-note-win"
    )
    rows = list_active_instances()
    # Assert
    assert len(rows) == 1


@pytest.mark.parametrize(
    "field, expected",
    [
        ("name", "diag-test"),
        ("pid", 1234),
        ("host", "ywata-note-win"),
        ("ended_at", None),
    ],
)
def test_record_instance_start_row_carries_field_from_constructor_args(
    db_path: Path, field: str, expected
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    # Act
    record_instance_start(
        "diag-test", pid=1234, screen="diag-test", host="ywata-note-win"
    )
    row = list_active_instances()[0]
    # Assert
    assert row[field] == expected


def test_record_instance_start_returned_id_matches_active_instance_row(
    db_path: Path,
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    # Act
    iid = record_instance_start(
        "diag-test", pid=1234, screen="diag-test", host="ywata-note-win"
    )
    # Assert
    assert list_active_instances()[0]["id"] == iid


def test_record_instance_start_stamps_started_at_on_the_instances_row(
    db_path: Path,
):
    """A start is RECORDED — the property, re-pointed at where it lives.

    This asserted ``[e["kind"] for e in events] == ["start"]`` against the
    ``events`` table until 2026-08-28. That table was deleted for having
    zero readers, and the deletion argument was precisely that its row
    carried nothing the ``instances`` row did not already carry in the same
    transaction: the event's ``ts`` IS ``instances.started_at``. So the
    assertion moves onto the surviving column rather than leaving with the
    table — the behaviour is real and still worth a test.
    """
    # Arrange
    from scitex_agent_container._state.state_db_instances import (
        read_instance,
        record_instance_start,
    )

    # Act
    iid = record_instance_start("x", host="h")
    row = read_instance(iid)
    # Assert
    assert row["started_at"]


def test_record_instance_stop_clears_the_active_instance_list(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
        record_instance_stop,
    )

    iid = record_instance_start("x", host="h")
    record_instance_stop(iid, exit_reason="stopped")
    # Act
    active_after = list_active_instances()
    # Assert
    assert active_after == []


def test_record_instance_stop_returns_true_on_first_call(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import (
        record_instance_start,
        record_instance_stop,
    )

    iid = record_instance_start("x", host="h")
    # Act
    first = record_instance_stop(iid, exit_reason="stopped")
    # Assert
    assert first is True


def test_record_instance_stop_returns_false_on_second_call_for_idempotency(
    db_path: Path,
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        record_instance_start,
        record_instance_stop,
    )

    iid = record_instance_start("x", host="h")
    record_instance_stop(iid, exit_reason="stopped")
    # Act
    second = record_instance_stop(iid)
    # Assert
    assert second is False


# ---------------------------------------------------------------------------
# ``update_heartbeat`` / ``latest_instance_heartbeat`` — SEVEN tests lived
# here until 2026-08-28, and they were good ones: same-second collapse via
# the ON CONFLICT upsert, the straddle-a-second path, "latest is MAX(seq)"
# proved on BOTH of those paths, the rolling cache on the ``instances`` row,
# and the COALESCE partial update. Every one of them pinned ``ts`` through
# the ``now_fn`` seam so the result was deterministic rather than a race
# with the wall clock.
#
# They are DELETED, NOT RE-POINTED, because their SUBJECT is gone rather
# than merely relocated. ``instance_heartbeats`` was removed from state.db
# together with ``state_db_heartbeats`` — the module that WAS its API — on
# the measurement that neither ``update_heartbeat`` nor
# ``latest_instance_heartbeat`` had a single caller anywhere in ``src/``,
# against 0 rows on every host. There is no surviving table these
# assertions could be aimed at and no surviving function to call: unlike
# the ``events`` test above, whose property (a start is recorded) simply
# moved onto ``instances.started_at``, these describe a determinism
# contract for a write path that no longer exists.
#
# WHAT THE DELETION TAKES WITH IT, so nobody has to rediscover it:
# ``update_heartbeat`` was also the only writer of
# ``instances.last_heartbeat_at`` / ``iter_count`` / ``input_tokens`` /
# ``output_tokens``, and ``state_db_gc``'s heartbeat-staleness rule reads
# the first of those. That branch could not fire before this change either
# — the writer had no callers — so no test here was covering it. Restoring
# it means deciding who beats, and that is a design change, not a test.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# gc_dead_instances — local pid that no longer exists is reaped as crashed.
# Shared env+module-attribute save/restore via a fixture (PA-306 pattern).
# ---------------------------------------------------------------------------


@pytest.fixture
def dead_pid_environment():
    """Pin host + suppress proc-btime so a fake pid reaps as crashed.

    PA-306: explicit env / module attribute save/restore, no monkeypatch.
    """
    import os

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


def test_gc_dead_instances_reports_at_least_one_crashed_for_dead_local_pid(
    db_path: Path, dead_pid_environment
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


def test_gc_dead_instances_removes_dead_instance_from_active_list(
    db_path: Path, dead_pid_environment
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


def test_gc_dead_instances_persists_exit_reason_crashed_on_instance_row(
    db_path: Path, dead_pid_environment
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        gc_dead_instances,
        record_instance_start,
    )
    from scitex_agent_container._state.state_db_instances import read_instance

    iid = record_instance_start("dead-agent", pid=999_999_999, host="test-host")
    # Act
    gc_dead_instances()
    row = read_instance(iid)
    # Assert
    assert row["exit_reason"] == "pid_absent_at_sweep"


def test_sweep_writes_a_reason_naming_the_check_not_a_cause(
    db_path: Path, dead_pid_environment
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
        record_instance_start,
    )
    from scitex_agent_container._state.state_db_instances import read_instance

    iid = record_instance_start("dead-agent", pid=999_999_999, host="test-host")
    # Act
    gc_dead_instances()
    row = read_instance(iid)
    # Assert
    assert "crashed" not in row["exit_reason"]


def test_one_sweep_stamps_every_reaped_row_with_the_same_ended_at(
    db_path: Path, dead_pid_environment
):
    """THE TRAP, pinned: a shared second is the sweep's clock, not a co-death.

    Measured 2026-08-12 — eleven rows shared ``17:54:26Z`` and were read as a
    simultaneous kill. They had died 10h46m earlier, at different moments.
    This test exists so nobody can "fix" the shared timestamp by accident and
    so the behaviour is documented as intended rather than incidental.
    """
    # Arrange
    from scitex_agent_container._state.state_db import (
        gc_dead_instances,
        record_instance_start,
    )

    for name in ("dead-a", "dead-b", "dead-c"):
        record_instance_start(name, pid=999_999_999, host="test-host")
    # Act
    gc_dead_instances()
    stamps = [
        r["ended_at"]
        for r in _swept_rows()
        if r["exit_reason"] == "pid_absent_at_sweep"
    ]
    # Assert
    assert len(set(stamps)) == 1


def test_db_clean_via_cli_reports_at_least_one_crashed_in_json_body(
    db_path: Path, dead_pid_environment
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
    db_path: Path,
):
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_tick

    runner = CliRunner()
    # Act
    result = runner.invoke(db_tick, [])
    # Assert — tick is silent on success: exit 0 AND empty stdout.
    assert (result.exit_code, result.output) == (0, "")
