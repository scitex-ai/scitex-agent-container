"""Tests for scitex_agent_container._state.state_db (F-CS11) — SQLite core.

Covers:
- init_schema: idempotent creation of the registry tables + attempts.
- table_counts: returns zero counts on a fresh db.
- import_legacy_registry: lifts JSON shards into ``instances`` records
  with ``exit_reason='reboot-swept'``; idempotent on re-run; tolerates
  malformed shards.
- ``sac db show`` / ``sac db migrate`` / ``sac db query`` end-to-end
  via CliRunner.

SPLIT ON 2026-08-28, when ``instances`` and ``events`` moved to per-host
PostgreSQL. The seam is the fixture, not a whim: what stays here is pure
SQLite, and what needed the store went to two siblings —

  * ``test_state_db_lifecycle_writes.py`` — record_instance_start/_stop,
    the instance_events log, update_heartbeat, gc_dead_instances, and the
    ``sac db clean / tick`` CLI.
  * ``test_state_db_export_import.py`` — export_state / import_state and
    their CLI, re-pointed at ``comms_nodes`` because ``instances`` left
    ``KNOWN_TABLES``.

``import_legacy_registry`` stayed HERE even though it now writes to
PostgreSQL, because what it is being tested for is reading the legacy
JSON shard directory; it takes ``pg_schema`` for the write half.

TQ cleanup (state_db slice): every test carries AAA markers and exactly
one assertion. Same-setup invariants (e.g. all-tables-empty on a fresh
db) collapse into ``pytest.parametrize`` over ``KNOWN_TABLES``. Test
names spell out the behaviour being verified (TQ003-compatible).
"""

from __future__ import annotations

import json
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
    assert (pre_existed, db_path.exists()) == (False, True)


@pytest.mark.parametrize("table", sorted(KNOWN_TABLES))
def test_init_schema_creates_each_known_registry_table(db_path: Path, table: str):
    # Arrange
    import sqlite3

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


def test_init_schema_no_longer_creates_the_migrated_instances_table(
    db_path: Path,
):
    # Arrange — the DDL was DELETED, not merely unused. A retired CREATE
    # TABLE keeps handing a stale reader an empty table, and for this one
    # an empty table reads as "no agents are running".
    import sqlite3

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
    assert "instances" not in names


def test_init_schema_called_twice_does_not_raise_on_idempotent_replay(
    db_path: Path,
):
    # Arrange
    init_schema()
    # Act
    init_schema()
    # Assert — reaching here means the replay did not raise.
    assert db_path.exists()


@pytest.mark.parametrize("table", sorted(KNOWN_TABLES))
def test_table_counts_returns_zero_for_each_table_on_fresh_db(
    db_path: Path, table: str
):
    # Arrange
    # (fixture already prepared an empty db env)
    init_schema()
    # Act
    counts = table_counts()
    # Assert
    assert counts[table] == 0


# ---------------------------------------------------------------------------
# import_legacy_registry — legacy JSON shards → ``instances`` records
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


def _imported() -> list[dict]:
    """The imported records, read through the accessor that owns them."""
    from scitex_agent_container._state.state_db import all_instances

    return all_instances()


def test_import_legacy_registry_returns_imported_count_for_two_shards(
    pg_schema: str, tmp_path: Path
):
    # Arrange
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "polish-clew")
    _write_legacy_shard(reg, "polish-sac", started_at="2026-05-05T03:29:42Z")
    # Act
    result = import_legacy_registry(reg, host="ywata-note-win")
    # Assert
    assert result == {"imported": 2, "skipped": 0}


def test_import_legacy_registry_writes_one_record_per_shard(
    pg_schema: str, tmp_path: Path
):
    # Arrange
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "polish-clew")
    _write_legacy_shard(reg, "polish-sac")
    # Act
    import_legacy_registry(reg, host="ywata-note-win")
    # Assert
    assert {r["name"] for r in _imported()} == {"polish-clew", "polish-sac"}


@pytest.mark.parametrize(
    "field, expected",
    [
        ("host", "ywata-note-win"),
        ("exit_reason", "reboot-swept"),
    ],
)
def test_import_legacy_registry_sets_invariant_field_on_swept_records(
    pg_schema: str, tmp_path: Path, field: str, expected: str
):
    # Arrange
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "polish-clew")
    # Act
    import_legacy_registry(reg, host="ywata-note-win")
    # Assert
    assert _imported()[0][field] == expected


@pytest.mark.parametrize("field", ["started_at", "ended_at"])
def test_import_legacy_registry_populates_timestamp_field_on_swept_records(
    pg_schema: str, tmp_path: Path, field: str
):
    # Arrange
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "polish-clew")
    # Act
    import_legacy_registry(reg, host="ywata-note-win")
    # Assert
    assert _imported()[0][field]


def test_import_legacy_registry_preserves_the_shards_own_started_at(
    pg_schema: str, tmp_path: Path
):
    # Arrange — restamping to import time would erase the only thing a
    # swept record is kept for: when that agent actually ran.
    reg = tmp_path / "registry"
    _write_legacy_shard(reg, "polish-clew")
    # Act
    import_legacy_registry(reg, host="ywata-note-win")
    # Assert
    assert _imported()[0]["started_at"] == "2026-05-05T03:29:41Z"


def test_import_legacy_registry_second_run_imports_zero_new_shards(
    pg_schema: str, tmp_path: Path
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
    pg_schema: str, tmp_path: Path
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
    pg_schema: str, tmp_path: Path
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
    body = json.loads(result.stdout)
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
    body = json.loads(result.stdout)
    # Assert
    assert body["tables"][table] == 0


def test_db_migrate_via_cli_reports_one_imported_for_single_shard(
    pg_schema: str, db_path: Path, tmp_path: Path
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


def test_db_query_rejects_the_migrated_instances_table(db_path: Path):
    # Arrange — this replaces two tests that ran ``db query --table
    # instances`` and asserted on the imported row. That query cannot be
    # answered from SQLite any more, and the important half is that it is
    # REFUSED rather than answered with an empty list: an empty list here
    # reads as "no agents are running", which an operator acts on.
    from scitex_agent_container.cli_pkg.db_group import db_query

    runner = CliRunner()
    # Act
    result = runner.invoke(db_query, ["--table", "instances", "--json"])
    # Assert
    assert result.exit_code != 0


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
