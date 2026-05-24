"""Tests for scitex_agent_container._state.state_db (F-CS11).

Covers:
- init_schema: idempotent creation of all four registry tables + attempts.
- table_counts: returns zero counts on a fresh db.
- import_legacy_registry: lifts JSON shards into ``instances`` rows
  with ``exit_reason='reboot-swept'``; idempotent on re-run; tolerates
  malformed shards.
- ``sac db show`` / ``sac db migrate`` / ``sac db query`` end-to-end
  via CliRunner.

TQ cleanup (state_db slice): every test carries AAA markers and exactly
one assertion. Same-setup invariants (e.g. all-tables-empty on a fresh
db) collapse into ``pytest.parametrize`` over ``KNOWN_TABLES``. Test
names spell out the behaviour being verified (TQ003-compatible).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.state_db import (
    KNOWN_TABLES,
    import_legacy_registry,
    init_schema,
    table_counts,
)


@pytest.fixture
def db_path(tmp_path: Path):
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


def test_init_schema_creates_state_db_file_on_disk(db_path: Path):
    # Arrange — fresh tmp_path, no db yet.
    pre_existed = db_path.exists()
    # Act
    init_schema()
    # Assert
    assert (not pre_existed) and db_path.exists()


@pytest.mark.parametrize("table", sorted(KNOWN_TABLES))
def test_init_schema_creates_each_known_registry_table(db_path: Path, table: str):
    # Arrange
    init_schema()
    # Act
    with sqlite3.connect(db_path) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    # Assert
    assert table in names


def test_init_schema_called_twice_does_not_raise_on_idempotent_replay(
    db_path: Path,
):
    # Arrange
    init_schema()
    # Act — second call must be a no-op on an already-initialised db.
    init_schema()
    # Assert — reaching this line means the second call did not raise.
    assert db_path.exists()


@pytest.mark.parametrize("table", sorted(KNOWN_TABLES))
def test_table_counts_returns_zero_for_each_table_on_fresh_db(
    db_path: Path, table: str
):
    # Arrange
    # (fixture already prepared an empty db env)
    # Act
    counts = table_counts()
    # Assert
    assert counts[table] == 0


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


def test_import_legacy_registry_writes_one_instances_row_per_shard(
    db_path: Path, tmp_path: Path
):
    # Arrange
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "polish-clew")
    _write_legacy_shard(reg, "polish-sac")
    # Act
    import_legacy_registry(reg, host="ywata-note-win")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM instances").fetchall()]
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
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM instances").fetchone())
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
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM instances").fetchone())
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
# CLI surface — `sac db show / query / migrate`
# ---------------------------------------------------------------------------


def test_db_show_json_exposes_known_tables_set(db_path: Path):
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_show

    runner = CliRunner()
    # Act
    result = runner.invoke(db_show, ["--json"])
    body = json.loads(result.output)
    # Assert
    assert set(body["known_tables"]) == set(KNOWN_TABLES)


@pytest.mark.parametrize("table", sorted(KNOWN_TABLES))
def test_db_show_json_reports_zero_count_per_table_on_fresh_db(
    db_path: Path, table: str
):
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_show

    runner = CliRunner()
    # Act
    result = runner.invoke(db_show, ["--json"])
    body = json.loads(result.output)
    # Assert
    assert body["tables"][table] == 0


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
    body = json.loads(result.output)
    # Assert
    assert body["imported"] == 1


def test_db_query_via_cli_returns_imported_row_name(db_path: Path, tmp_path: Path):
    # Arrange
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "diag-test")
    import_legacy_registry(reg, host="h")
    from scitex_agent_container.cli_pkg.db_group import db_query

    runner = CliRunner()
    # Act
    result = runner.invoke(db_query, ["--table", "instances", "--limit", "5", "--json"])
    rows = json.loads(result.output)
    # Assert
    assert [r["name"] for r in rows] == ["diag-test"]


def test_db_query_via_cli_returns_imported_row_with_reboot_swept_exit_reason(
    db_path: Path, tmp_path: Path
):
    # Arrange
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "diag-test")
    import_legacy_registry(reg, host="h")
    from scitex_agent_container.cli_pkg.db_group import db_query

    runner = CliRunner()
    # Act
    result = runner.invoke(db_query, ["--table", "instances", "--limit", "5", "--json"])
    rows = json.loads(result.output)
    # Assert
    assert rows[0]["exit_reason"] == "reboot-swept"


def test_db_query_via_cli_rejects_unknown_table_with_nonzero_exit(
    db_path: Path,
):
    # Arrange
    from scitex_agent_container.cli_pkg.db_group import db_query

    runner = CliRunner()
    # Act
    result = runner.invoke(db_query, ["--table", "nope"])
    # Assert
    assert result.exit_code != 0


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


def test_record_instance_start_writes_a_start_kind_event_row(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import (
        open_db,
        record_instance_start,
    )

    # Act
    iid = record_instance_start("x", host="h")
    with open_db() as conn:
        events = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM events WHERE instance_id=?", (iid,)
            ).fetchall()
        ]
    # Assert
    assert [e["kind"] for e in events] == ["start"]


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


def test_update_heartbeat_collapses_two_same_second_beats_into_one_row(
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
    db_path: Path, column: str, expected: int, second_ts: str
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
    "column, expected",
    [
        ("iter_count", 2),
        ("input_tokens", 30),
        ("output_tokens", 40),
    ],
)
def test_update_heartbeat_caches_rolling_value_on_instances_row(
    db_path: Path, column: str, expected: int
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        open_db,
        record_instance_start,
        update_heartbeat,
    )

    iid = record_instance_start("x", host="h")
    update_heartbeat(iid, iter=1, input_tokens=10, output_tokens=20)
    update_heartbeat(iid, iter=2, input_tokens=30, output_tokens=40)
    # Act
    with open_db() as conn:
        inst = dict(
            conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
        )
    # Assert
    assert inst[column] == expected


def test_update_heartbeat_caches_last_heartbeat_at_timestamp_on_instance_row(
    db_path: Path,
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        open_db,
        record_instance_start,
        update_heartbeat,
    )

    iid = record_instance_start("x", host="h")
    update_heartbeat(iid, iter=1, input_tokens=10, output_tokens=20)
    # Act
    with open_db() as conn:
        inst = dict(
            conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
        )
    # Assert
    assert inst["last_heartbeat_at"] is not None


@pytest.mark.parametrize(
    "column, expected",
    [
        ("iter_count", 5),
        ("input_tokens", 100),
        ("output_tokens", 200),
    ],
)
def test_update_heartbeat_partial_update_preserves_previous_field_via_coalesce(
    db_path: Path, column: str, expected: int
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        open_db,
        record_instance_start,
        update_heartbeat,
    )

    iid = record_instance_start("x", host="h")
    update_heartbeat(iid, iter=5, input_tokens=100, output_tokens=200)
    # Act — second call only updates pane_state; other fields must NOT reset.
    update_heartbeat(iid, pane_state="alive")
    with open_db() as conn:
        inst = dict(
            conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
        )
    # Assert
    assert inst[column] == expected


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
        open_db,
        record_instance_start,
    )

    iid = record_instance_start("dead-agent", pid=999_999_999, host="test-host")
    # Act
    gc_dead_instances()
    with open_db() as conn:
        row = conn.execute(
            "SELECT exit_reason FROM instances WHERE id=?", (iid,)
        ).fetchone()
    # Assert
    assert row["exit_reason"] == "crashed"


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
    body = json.loads(result.output)
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


# ---------------------------------------------------------------------------
# F-CS14 — export / import (cross-host orochi pull)
# ---------------------------------------------------------------------------


def _expected_schema_version():
    from scitex_agent_container._state.state_db import EXPORT_SCHEMA_VERSION

    return EXPORT_SCHEMA_VERSION


@pytest.mark.parametrize(
    "field, expected_factory",
    [
        ("schema", _expected_schema_version),
        ("host", lambda: "src-host"),
        ("since", lambda: None),
    ],
)
def test_export_state_payload_carries_self_describing_field(
    db_path: Path, field: str, expected_factory
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    record_instance_start("polish-clew", host="src-host")
    # Act
    payload = export_state(host="src-host")
    # Assert
    assert payload[field] == expected_factory()


def test_export_state_payload_includes_exported_at_timestamp(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    record_instance_start("polish-clew", host="src-host")
    # Act
    payload = export_state(host="src-host")
    # Assert
    assert "exported_at" in payload


def test_export_state_payload_tables_key_covers_all_known_tables(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    record_instance_start("polish-clew", host="src-host")
    # Act
    payload = export_state(host="src-host")
    # Assert
    assert set(payload["tables"]) == set(KNOWN_TABLES)


def test_export_state_payload_instances_table_contains_recorded_row_name(
    db_path: Path,
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    record_instance_start("polish-clew", host="src-host")
    # Act
    payload = export_state(host="src-host")
    # Assert
    assert [r["name"] for r in payload["tables"]["instances"]] == ["polish-clew"]


def test_export_state_filters_out_instance_rows_older_than_since_cutoff(
    db_path: Path,
):
    # Arrange — bracket the cutoff with sleeps so the second boundary is clean.
    import datetime as _dt
    import time as _time

    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    record_instance_start("old", host="h")
    _time.sleep(1.05)
    cut = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _time.sleep(1.05)
    record_instance_start("new", host="h")
    # Act
    payload = export_state(since=cut, host="h")
    # Assert
    assert [r["name"] for r in payload["tables"]["instances"]] == ["new"]


@pytest.fixture
def switch_to_sink_db(tmp_path: Path):
    """Factory fixture: caller invokes after preparing source-db state.

    Yields a callable ``switch()`` that swaps the env-rooted state.db to a
    fresh sink path under tmp_path, reloads the module, and returns the
    reloaded ``state_db`` module for use against the sink. On test
    teardown the env is restored and the module reloaded back to the
    pre-switch path. This sequencing matters: tests need to record rows
    on the SOURCE db, snapshot them, and only then point the env at the
    sink — otherwise the "import" runs against a db that already holds
    the rows it's trying to insert.
    """
    import importlib
    import os

    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    switched = False

    def switch():
        nonlocal switched
        os.environ[key] = str(tmp_path / "sink.db")
        import scitex_agent_container._state.state_db as mod

        importlib.reload(mod)
        switched = True
        return mod

    try:
        yield switch
    finally:
        if switched:
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved
            import scitex_agent_container._state.state_db as mod

            importlib.reload(mod)


def test_import_state_into_fresh_db_inserts_each_source_instance_row(
    db_path: Path, switch_to_sink_db
):
    # Arrange — write 2 rows on the source db and snapshot them.
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    record_instance_start("a", host="src")
    record_instance_start("b", host="src")
    payload = export_state(host="src")
    sink_mod = switch_to_sink_db()
    # Act — import into the fresh sink (different env path).
    inserted = sink_mod.import_state(payload)
    # Assert
    assert inserted["instances"] == 2


def test_import_state_replayed_on_same_payload_inserts_zero_rows(
    db_path: Path, switch_to_sink_db
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    record_instance_start("a", host="src")
    record_instance_start("b", host="src")
    payload = export_state(host="src")
    sink_mod = switch_to_sink_db()
    sink_mod.import_state(payload)
    # Act
    inserted_again = sink_mod.import_state(payload)
    # Assert
    assert inserted_again["instances"] == 0


def test_import_state_round_trip_lands_exactly_the_source_uuids_on_sink(
    db_path: Path, switch_to_sink_db
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    iid1 = record_instance_start("a", host="src")
    iid2 = record_instance_start("b", host="src")
    payload = export_state(host="src")
    sink_mod = switch_to_sink_db()
    # Act
    sink_mod.import_state(payload)
    with sink_mod.open_db() as conn:
        rows = sorted(
            r["id"] for r in conn.execute("SELECT id FROM instances").fetchall()
        )
    # Assert
    assert rows == sorted([iid1, iid2])


def test_import_state_rejects_payload_with_unknown_schema_version(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import import_state

    bad_payload = {"schema": 999, "tables": {}}
    # Act
    ctx = pytest.raises(ValueError, match="schema")
    # Assert
    with ctx:
        import_state(bad_payload)


def test_db_export_via_cli_emits_json_with_recorded_instance_for_host(
    db_path: Path,
):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container.cli_pkg.db_group import db_export

    record_instance_start("x", host="h")
    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--host", "h"])
    payload = json.loads(result.output)
    # Assert
    assert len(payload["tables"]["instances"]) == 1


def test_db_export_via_cli_emits_payload_with_requested_host_key(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container.cli_pkg.db_group import db_export

    record_instance_start("x", host="h")
    runner = CliRunner()
    # Act
    result = runner.invoke(db_export, ["--host", "h"])
    payload = json.loads(result.output)
    # Assert
    assert payload["host"] == "h"


def test_db_import_via_cli_reads_stdin_and_inserts_one_instance_row(
    db_path: Path, switch_to_sink_db
):
    # Arrange — snapshot source, point CLI at the fresh sink db.
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    record_instance_start("x", host="h")
    payload = export_state(host="h")
    switch_to_sink_db()
    from scitex_agent_container.cli_pkg.db_group import db_import

    runner = CliRunner()
    # Act
    result = runner.invoke(db_import, ["-", "--json"], input=json.dumps(payload))
    body = json.loads(result.output)
    # Assert
    assert body["inserted"]["instances"] == 1


def test_db_import_via_cli_echoes_payload_host_back_in_json_body(
    db_path: Path, switch_to_sink_db
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    record_instance_start("x", host="h")
    payload = export_state(host="h")
    switch_to_sink_db()
    from scitex_agent_container.cli_pkg.db_group import db_import

    runner = CliRunner()
    # Act
    result = runner.invoke(db_import, ["-", "--json"], input=json.dumps(payload))
    body = json.loads(result.output)
    # Assert
    assert body["host"] == "h"
