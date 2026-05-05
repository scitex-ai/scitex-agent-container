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
