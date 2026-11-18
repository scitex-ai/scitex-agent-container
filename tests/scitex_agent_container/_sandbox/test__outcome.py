"""Outcome-record helpers: atomic writes, manifests, Hermes token usage."""

from __future__ import annotations

import sqlite3

from scitex_agent_container._sandbox._outcome import (
    atomic_write_json,
    hermes_transcript,
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


def _transcript_db(tmp_path):
    import json

    db = tmp_path / "hermes" / "state.db"
    db.parent.mkdir()
    con = sqlite3.connect(db)
    con.execute(
        "create table messages (id integer primary key, role text, tool_calls text,"
        " tool_call_id text, content text, timestamp real)"
    )
    call = {"id": "c1", "function": {"name": "read_file", "arguments": json.dumps({"path": "CLAIMS.md", "limit": 50})}}
    term = {"id": "c2", "function": {"name": "terminal", "arguments": json.dumps({"command": "ls"})}}
    con.executemany(
        "insert into messages (role, tool_calls, tool_call_id, content, timestamp) values (?,?,?,?,?)",
        [
            ("user", None, None, "review", 1.0),
            ("assistant", json.dumps([call, term]), None, "", 2.0),
            ("tool", None, "c1", "x" * 120, 3.0),
            ("tool", None, "c2", "a b", 3.5),
        ],
    )
    con.commit()
    con.close()
    return db


def test_transcript_lists_files_read_by_file_tools(tmp_path):
    # Arrange
    db = _transcript_db(tmp_path)
    # Act
    got = hermes_transcript(db, tmp_path / "scratch")
    # Assert
    assert [(f["path"], f["result_bytes"]) for f in got["files_read"]] == [("CLAIMS.md", 120)]


def test_transcript_counts_every_tool_call(tmp_path):
    # Arrange
    db = _transcript_db(tmp_path)
    # Act
    got = hermes_transcript(db, tmp_path / "scratch")
    # Assert
    assert got["tool_call_counts"] == {"read_file": 1, "terminal": 1}


def test_transcript_names_its_source(tmp_path):
    # Arrange
    db = _transcript_db(tmp_path)
    # Act
    got = hermes_transcript(db, tmp_path / "scratch")
    # Assert
    assert got["source"].startswith("harness transcript")


def test_transcript_records_first_assistant_time(tmp_path):
    # Arrange
    db = _transcript_db(tmp_path)
    # Act
    got = hermes_transcript(db, tmp_path / "scratch")
    # Assert
    assert got["first_assistant_message_at"] == 2.0
