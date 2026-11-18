"""Outcome-record helpers: atomic writes, manifests, Hermes token usage."""

from __future__ import annotations

import sqlite3

from scitex_agent_container._sandbox._outcome import (
    atomic_write_json,
    hermes_usage,
    manifest,
    manifest_diff,
    read_json,
)


def test_atomic_write_round_trips(tmp_path):
    # Arrange
    path = tmp_path / "out" / "outcome.json"
    # Act
    atomic_write_json(path, {"status": "running", "exit": {"code": None}})
    # Assert
    assert read_json(path) == {"status": "running", "exit": {"code": None}}


def test_atomic_write_leaves_no_temp_file(tmp_path):
    # Arrange
    path = tmp_path / "outcome.json"
    # Act
    atomic_write_json(path, {"a": 1})
    # Assert
    assert [p.name for p in tmp_path.iterdir()] == ["outcome.json"]


def test_unreadable_record_reads_as_none(tmp_path):
    # Arrange
    path = tmp_path / "outcome.json"
    path.write_text("")
    # Act
    got = read_json(path)
    # Assert
    assert got is None


def test_manifest_diff_names_created_modified_deleted(tmp_path):
    # Arrange
    (tmp_path / "keep").write_text("1")
    (tmp_path / "change").write_text("1")
    (tmp_path / "gone").write_text("1")
    before = manifest(tmp_path)
    (tmp_path / "gone").unlink()
    (tmp_path / "change").write_text("22")
    (tmp_path / "new").write_text("1")
    # Act
    diff = manifest_diff(before, manifest(tmp_path))
    # Assert
    assert (diff["created"], diff["modified"], diff["deleted"]) == (
        ["new"],
        ["change"],
        ["gone"],
    )


def test_hermes_usage_totals_token_columns(tmp_path):
    # Arrange
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "create table sessions (id text, model text, input_tokens int, output_tokens int)"
    )
    con.executemany(
        "insert into sessions values (?,?,?,?)",
        [("a", "m", 10, 2), ("b", "m", 5, None)],
    )
    con.commit()
    con.close()
    # Act
    usage = hermes_usage(db)
    # Assert
    assert usage["token_totals"] == {"input_tokens": 15, "output_tokens": 2}


def test_hermes_usage_absent_db_is_none(tmp_path):
    # Arrange
    db = tmp_path / "missing.db"
    # Act
    usage = hermes_usage(db)
    # Assert
    assert usage is None
