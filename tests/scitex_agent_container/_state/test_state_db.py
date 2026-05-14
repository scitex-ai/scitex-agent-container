"""Tests for scitex_agent_container._state.state_db (F-CS11).

Covers:
- init_schema: idempotent creation of all four registry tables + attempts.
- table_counts: returns zero counts on a fresh db.
- import_legacy_registry: lifts JSON shards into ``instances`` rows
  with ``exit_reason='reboot-swept'``; idempotent on re-run; tolerates
  malformed shards.
- ``sac db show`` / ``sac db migrate`` / ``sac db query`` end-to-end
  via CliRunner.
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
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated state.db location, exported via env so the CLI picks it up."""
    p = tmp_path / "state.db"
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_STATE_DB", str(p))
    # Reload the module-level constant without bouncing the import.
    import importlib

    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    return p


def test_init_schema_creates_all_tables(db_path: Path):
    init_schema()
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    for table in KNOWN_TABLES:
        assert table in names, f"missing table {table!r} in fresh db"


def test_init_schema_is_idempotent(db_path: Path):
    init_schema()
    init_schema()  # second call must not raise
    counts = table_counts()
    assert all(counts[t] == 0 for t in KNOWN_TABLES)


def test_table_counts_zero_on_fresh_db(db_path: Path):
    counts = table_counts()
    for t in KNOWN_TABLES:
        assert counts[t] == 0


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


def test_import_legacy_registry_imports_shards_as_swept(db_path: Path, tmp_path: Path):
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "polish-clew")
    _write_legacy_shard(reg, "polish-sac", started_at="2026-05-05T03:29:42Z")

    result = import_legacy_registry(reg, host="ywata-note-win")
    assert result == {"imported": 2, "skipped": 0}

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM instances").fetchall()]
    assert len(rows) == 2
    by_name = {r["name"]: r for r in rows}
    assert "polish-clew" in by_name and "polish-sac" in by_name
    for r in rows:
        assert r["host"] == "ywata-note-win"
        assert r["exit_reason"] == "reboot-swept"
        assert r["ended_at"], "swept rows must record an end timestamp"
        assert r["started_at"]


def test_import_legacy_registry_is_idempotent(db_path: Path, tmp_path: Path):
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "polish-clew")

    first = import_legacy_registry(reg, host="ywata-note-win")
    assert first["imported"] == 1
    second = import_legacy_registry(reg, host="ywata-note-win")
    assert second["imported"] == 0
    assert second["skipped"] == 1


def test_import_legacy_registry_skips_malformed_shards(db_path: Path, tmp_path: Path):
    reg = tmp_path / "registry"
    reg.mkdir()
    (reg / "bad.json").write_text("not json {")
    _write_legacy_shard(reg, "good")

    result = import_legacy_registry(reg, host="h")
    assert result["imported"] == 1
    assert result["skipped"] == 1


def test_import_legacy_registry_handles_missing_dir(db_path: Path, tmp_path: Path):
    result = import_legacy_registry(tmp_path / "does-not-exist", host="h")
    assert result == {"imported": 0, "skipped": 0}


# ---------------------------------------------------------------------------
# CLI surface — `sac db show / query / migrate`
# ---------------------------------------------------------------------------


def test_db_show_renders_counts(db_path: Path):
    from scitex_agent_container.cli_pkg.db_group import db_show

    runner = CliRunner()
    result = runner.invoke(db_show, ["--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert set(body["known_tables"]) == set(KNOWN_TABLES)
    assert all(body["tables"][t] == 0 for t in KNOWN_TABLES)


def test_db_migrate_via_cli(db_path: Path, tmp_path: Path):
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "diag-test")

    from scitex_agent_container.cli_pkg.db_group import db_migrate

    runner = CliRunner()
    result = runner.invoke(
        db_migrate, ["--registry-dir", str(reg), "--host", "ywata-note-win", "--json"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["imported"] == 1


def test_db_query_returns_imported_rows(db_path: Path, tmp_path: Path):
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "diag-test")
    import_legacy_registry(reg, host="h")

    from scitex_agent_container.cli_pkg.db_group import db_query

    runner = CliRunner()
    result = runner.invoke(db_query, ["--table", "instances", "--limit", "5", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["name"] == "diag-test"
    assert rows[0]["exit_reason"] == "reboot-swept"


def test_db_query_rejects_unknown_table(db_path: Path):
    from scitex_agent_container.cli_pkg.db_group import db_query

    runner = CliRunner()
    result = runner.invoke(db_query, ["--table", "nope"])
    assert result.exit_code != 0
    # Click's invalid choice message
    assert "invalid" in result.output.lower() or "not one of" in result.output.lower()


# ---------------------------------------------------------------------------
# F-CS11 phase 2 — write helpers + gc_dead_instances
# ---------------------------------------------------------------------------


def test_record_instance_start_returns_uuid_and_inserts_row(db_path: Path):
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    iid = record_instance_start(
        "diag-test", pid=1234, screen="diag-test", host="ywata-note-win"
    )
    assert iid and len(iid) >= 32  # uuid string

    rows = list_active_instances()
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == iid
    assert r["name"] == "diag-test"
    assert r["pid"] == 1234
    assert r["host"] == "ywata-note-win"
    assert r["ended_at"] is None


def test_record_instance_start_logs_event(db_path: Path):
    from scitex_agent_container._state.state_db import open_db, record_instance_start

    iid = record_instance_start("x", host="h")
    with open_db() as conn:
        events = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM events WHERE instance_id=?", (iid,)
            ).fetchall()
        ]
    assert len(events) == 1
    assert events[0]["kind"] == "start"


def test_record_instance_stop_marks_ended(db_path: Path):
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
        record_instance_stop,
    )

    iid = record_instance_start("x", host="h")
    assert record_instance_stop(iid, exit_reason="stopped") is True
    assert list_active_instances() == []
    # Idempotent on re-stop
    assert record_instance_stop(iid) is False


def test_update_heartbeat_appends_and_caches_rolling(db_path: Path):
    from scitex_agent_container._state.state_db import (
        open_db,
        record_instance_start,
        update_heartbeat,
    )

    iid = record_instance_start("x", host="h")
    update_heartbeat(iid, iter=1, input_tokens=10, output_tokens=20)
    # Two beats in the same wall-clock second collapse via ON CONFLICT
    # — the row's iter / token fields advance to the latest values.
    update_heartbeat(iid, iter=2, input_tokens=30, output_tokens=40)

    with open_db() as conn:
        hbs = conn.execute(
            "SELECT count(*) AS n FROM heartbeats WHERE instance_id=?", (iid,)
        ).fetchone()
        hb_row = dict(
            conn.execute(
                "SELECT * FROM heartbeats WHERE instance_id=?", (iid,)
            ).fetchone()
        )
        inst = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    # 1 heartbeat row per (instance_id, ts) at 1-sec resolution; the
    # second update merged into the same row.
    assert hbs["n"] == 1
    assert hb_row["iter"] == 2
    assert hb_row["input_tokens"] == 30
    assert hb_row["output_tokens"] == 40
    # Rolling cache on instances picks up the latest values regardless.
    assert inst["iter_count"] == 2
    assert inst["input_tokens"] == 30
    assert inst["output_tokens"] == 40
    assert inst["last_heartbeat_at"] is not None


def test_update_heartbeat_partial_update_preserves_prev_via_coalesce(db_path: Path):
    from scitex_agent_container._state.state_db import (
        open_db,
        record_instance_start,
        update_heartbeat,
    )

    iid = record_instance_start("x", host="h")
    update_heartbeat(iid, iter=5, input_tokens=100, output_tokens=200)
    # Second call only updates pane_state — other fields must NOT reset.
    update_heartbeat(iid, pane_state="alive")

    with open_db() as conn:
        inst = dict(
            conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
        )
    assert inst["iter_count"] == 5
    assert inst["input_tokens"] == 100
    assert inst["output_tokens"] == 200


def test_gc_dead_instances_marks_dead_local_pid_as_crashed(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A local instance whose pid is dead should be marked 'crashed'."""
    from scitex_agent_container._state import state_db
    from scitex_agent_container._state.state_db import (
        gc_dead_instances,
        list_active_instances,
        record_instance_start,
    )

    # Force a single canonical host for this test so the row's host
    # matches the gc's host filter.
    monkeypatch.setenv("SAC_HOST", "test-host")
    # Avoid the boot-epoch sweep for this test (needs a stable started_at
    # below boot — easier to suppress the proc check entirely).
    monkeypatch.setattr(state_db, "_proc_btime", lambda: None)

    iid = record_instance_start("dead-agent", pid=999_999_999, host="test-host")
    assert list_active_instances(host="test-host")
    counters = gc_dead_instances()
    assert counters["crashed"] >= 1
    assert list_active_instances(host="test-host") == []
    # Record persists with the right exit_reason.
    from scitex_agent_container._state.state_db import open_db

    with open_db() as conn:
        row = conn.execute(
            "SELECT exit_reason FROM instances WHERE id=?", (iid,)
        ).fetchone()
    assert row["exit_reason"] == "crashed"


def test_db_clean_via_cli_emits_counts(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    from scitex_agent_container._state import state_db
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container.cli_pkg.db_group import db_clean

    monkeypatch.setenv("SAC_HOST", "test-host")
    monkeypatch.setattr(state_db, "_proc_btime", lambda: None)
    record_instance_start("dead", pid=999_999_999, host="test-host")

    runner = CliRunner()
    result = runner.invoke(db_clean, ["--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["crashed"] >= 1


def test_db_tick_silent_zero_exit(db_path: Path):
    from scitex_agent_container.cli_pkg.db_group import db_tick

    runner = CliRunner()
    result = runner.invoke(db_tick, [])
    assert result.exit_code == 0
    # Tick is silent on success — no human-facing line.
    assert result.output == ""


# ---------------------------------------------------------------------------
# F-CS14 — export / import (cross-host orochi pull)
# ---------------------------------------------------------------------------


def test_export_state_emits_self_describing_payload(db_path: Path):
    from scitex_agent_container._state.state_db import (
        EXPORT_SCHEMA_VERSION,
        export_state,
        record_instance_start,
    )

    record_instance_start("polish-clew", host="src-host")
    payload = export_state(host="src-host")

    assert payload["schema"] == EXPORT_SCHEMA_VERSION
    assert payload["host"] == "src-host"
    assert payload["since"] is None
    assert "exported_at" in payload
    assert set(payload["tables"]) == set(KNOWN_TABLES)
    assert len(payload["tables"]["instances"]) == 1
    assert payload["tables"]["instances"][0]["name"] == "polish-clew"


def test_export_state_filters_by_since(db_path: Path):
    """Rows older than ``--since`` must be excluded."""
    import time as _time

    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    record_instance_start("old", host="h")
    # Sleep across a wall-clock-second boundary so since cuts cleanly.
    _time.sleep(1.05)
    cut = (
        __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    _time.sleep(1.05)
    record_instance_start("new", host="h")

    payload = export_state(since=cut, host="h")
    names = [r["name"] for r in payload["tables"]["instances"]]
    assert names == ["new"], (
        f"--since={cut!r} should drop 'old' but keep 'new'; got {names}"
    )


def test_import_state_round_trip_idempotent(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Export from one db, import into a fresh db, re-import is a no-op."""
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    # Source: write 2 rows.
    iid1 = record_instance_start("a", host="src")
    iid2 = record_instance_start("b", host="src")
    payload = export_state(host="src")

    # Sink: brand-new db (different env path).
    sink = tmp_path / "sink.db"
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_STATE_DB", str(sink))
    import importlib

    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)

    inserted = mod.import_state(payload)
    assert inserted["instances"] == 2

    # Re-import the same payload — every row already present → 0 inserts.
    inserted_again = mod.import_state(payload)
    assert inserted_again["instances"] == 0

    # Sink contains exactly the source uuids.
    with mod.open_db() as conn:
        rows = sorted(
            r["id"] for r in conn.execute("SELECT id FROM instances").fetchall()
        )
    assert rows == sorted([iid1, iid2])


def test_import_state_rejects_wrong_schema(db_path: Path):
    from scitex_agent_container._state.state_db import import_state

    with pytest.raises(ValueError, match="schema"):
        import_state({"schema": 999, "tables": {}})


def test_db_export_via_cli_emits_json(db_path: Path):
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container.cli_pkg.db_group import db_export

    record_instance_start("x", host="h")
    runner = CliRunner()
    result = runner.invoke(db_export, ["--host", "h"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["host"] == "h"
    assert len(payload["tables"]["instances"]) == 1


def test_db_import_via_cli_reads_stdin(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``sac db import -`` reads the dump from stdin."""
    from scitex_agent_container._state.state_db import (
        export_state,
        record_instance_start,
    )

    record_instance_start("x", host="h")
    payload = export_state(host="h")

    # Switch DB to a fresh one; import via CLI from stdin.
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_STATE_DB", str(tmp_path / "fresh.db"))
    import importlib

    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    from scitex_agent_container.cli_pkg.db_group import db_import

    runner = CliRunner()
    result = runner.invoke(db_import, ["-", "--json"], input=json.dumps(payload))
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["inserted"]["instances"] == 1
    assert body["host"] == "h"
