"""Tests for ``_lifecycle._session_movement``.

Covers the helper used by ``sac agents status --json`` to surface
``session_jsonl_bytes`` + ``session_jsonl_last_write`` + ``heartbeat_at``
as top-level keys per the operator mandate (lead a2a 1781e82a,
2026-06-14). Real ``tmp_path`` directories, no mocks. AAA markers
on separate lines. One assertion per test (STX-TQ007).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._session_movement import (
    heartbeat_iso,
    resolve_state_dir,
    session_jsonl_movement,
    status_movement_fields,
)

ISO_8601_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00$")


# ---------------------------------------------------------------------------
# session_jsonl_movement
# ---------------------------------------------------------------------------


def test_session_jsonl_movement_returns_zero_bytes_when_missing(tmp_path: Path):
    # Arrange
    state_dir = tmp_path / "fresh"
    state_dir.mkdir()
    # Act
    bytes_, _last_write = session_jsonl_movement(state_dir)
    # Assert
    assert bytes_ == 0


def test_session_jsonl_movement_returns_empty_iso_when_missing(tmp_path: Path):
    # Arrange
    state_dir = tmp_path / "fresh"
    state_dir.mkdir()
    # Act
    _bytes, last_write = session_jsonl_movement(state_dir)
    # Assert
    assert last_write == ""


def test_session_jsonl_movement_returns_real_bytes(tmp_path: Path):
    # Arrange
    state_dir = tmp_path / "live"
    state_dir.mkdir()
    payload = b'{"type":"user","text":"hi"}\n{"type":"assistant","text":"yo"}\n'
    (state_dir / "session.jsonl").write_bytes(payload)
    # Act
    bytes_, _last_write = session_jsonl_movement(state_dir)
    # Assert
    assert bytes_ == len(payload)


def test_session_jsonl_movement_returns_iso_8601_utc_mtime(tmp_path: Path):
    # Arrange
    state_dir = tmp_path / "live"
    state_dir.mkdir()
    jsonl = state_dir / "session.jsonl"
    jsonl.write_text("{}\n", encoding="utf-8")
    # Pin mtime to a known unix-ts so we can assert the format AND the value.
    pinned = 1_750_000_000.0
    os.utime(jsonl, (pinned, pinned))
    # Act
    _bytes, last_write = session_jsonl_movement(state_dir)
    # Assert
    assert ISO_8601_UTC_RE.match(last_write) is not None


def test_session_jsonl_movement_iso_matches_actual_mtime(tmp_path: Path):
    # Arrange — pin mtime to a known unix-ts and convert via the same
    # tz-aware path the helper uses (utc) so the test is deterministic.
    from datetime import datetime, timezone

    state_dir = tmp_path / "live"
    state_dir.mkdir()
    jsonl = state_dir / "session.jsonl"
    jsonl.write_text("{}\n", encoding="utf-8")
    pinned = 1_750_000_000.0
    os.utime(jsonl, (pinned, pinned))
    expected = datetime.fromtimestamp(pinned, tz=timezone.utc).isoformat()
    # Act
    _bytes, last_write = session_jsonl_movement(state_dir)
    # Assert
    assert last_write == expected


def test_session_jsonl_movement_empty_path_returns_zero(tmp_path: Path):
    # Arrange
    state_dir = ""
    # Act
    bytes_, _last_write = session_jsonl_movement(state_dir)
    # Assert
    assert bytes_ == 0


def test_session_jsonl_movement_missing_state_dir_returns_empty_iso(tmp_path: Path):
    # Arrange — state_dir does not exist at all.
    state_dir = tmp_path / "ghost"
    # Act
    _bytes, last_write = session_jsonl_movement(state_dir)
    # Assert
    assert last_write == ""


# ---------------------------------------------------------------------------
# heartbeat_iso
# ---------------------------------------------------------------------------


def test_heartbeat_iso_returns_empty_when_missing(tmp_path: Path):
    # Arrange
    state_dir = tmp_path / "fresh"
    state_dir.mkdir()
    # Act
    iso = heartbeat_iso(state_dir)
    # Assert
    assert iso == ""


def test_heartbeat_iso_returns_iso_8601_for_valid_payload(tmp_path: Path):
    # Arrange
    state_dir = tmp_path / "live"
    state_dir.mkdir()
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": 1_750_000_000.0, "pid": 1, "state": "idle"}),
        encoding="utf-8",
    )
    # Act
    iso = heartbeat_iso(state_dir)
    # Assert
    assert ISO_8601_UTC_RE.match(iso) is not None


def test_heartbeat_iso_value_matches_expected(tmp_path: Path):
    # Arrange
    from datetime import datetime, timezone

    state_dir = tmp_path / "live"
    state_dir.mkdir()
    pinned = 1_750_000_000.0
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": pinned, "pid": 1, "state": "idle"}),
        encoding="utf-8",
    )
    expected = datetime.fromtimestamp(pinned, tz=timezone.utc).isoformat()
    # Act
    iso = heartbeat_iso(state_dir)
    # Assert
    assert iso == expected


def test_heartbeat_iso_returns_empty_on_corrupt_json(tmp_path: Path):
    # Arrange
    state_dir = tmp_path / "live"
    state_dir.mkdir()
    (state_dir / "heartbeat.json").write_text("not-json{", encoding="utf-8")
    # Act
    iso = heartbeat_iso(state_dir)
    # Assert
    assert iso == ""


def test_heartbeat_iso_returns_empty_when_ts_missing(tmp_path: Path):
    # Arrange
    state_dir = tmp_path / "live"
    state_dir.mkdir()
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"pid": 1, "state": "idle"}),
        encoding="utf-8",
    )
    # Act
    iso = heartbeat_iso(state_dir)
    # Assert
    assert iso == ""


# ---------------------------------------------------------------------------
# status_movement_fields
# ---------------------------------------------------------------------------


def test_status_movement_fields_none_state_dir_returns_all_three_keys():
    # Arrange — no resolvable state dir at all.
    # Act
    fields = status_movement_fields(None)
    # Assert
    assert set(fields) == {
        "session_jsonl_bytes",
        "session_jsonl_last_write",
        "heartbeat_at",
    }


def test_status_movement_fields_none_state_dir_bytes_is_zero():
    # Arrange
    # Act
    fields = status_movement_fields(None)
    # Assert
    assert fields["session_jsonl_bytes"] == 0


def test_status_movement_fields_none_state_dir_last_write_is_empty():
    # Arrange
    # Act
    fields = status_movement_fields(None)
    # Assert
    assert fields["session_jsonl_last_write"] == ""


def test_status_movement_fields_none_state_dir_heartbeat_at_is_empty():
    # Arrange
    # Act
    fields = status_movement_fields(None)
    # Assert
    assert fields["heartbeat_at"] == ""


def test_status_movement_fields_populated_for_live_state_dir(tmp_path: Path):
    # Arrange
    state_dir = tmp_path / "live"
    state_dir.mkdir()
    (state_dir / "session.jsonl").write_text("hello\n", encoding="utf-8")
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": time.time(), "pid": 1, "state": "idle"}),
        encoding="utf-8",
    )
    # Act
    fields = status_movement_fields(state_dir)
    # Assert
    assert fields["session_jsonl_bytes"] == len(b"hello\n")


def test_status_movement_fields_live_state_dir_heartbeat_is_iso(tmp_path: Path):
    # Arrange
    state_dir = tmp_path / "live"
    state_dir.mkdir()
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": 1_750_000_000.0, "pid": 1, "state": "working"}),
        encoding="utf-8",
    )
    # Act
    fields = status_movement_fields(state_dir)
    # Assert
    assert ISO_8601_UTC_RE.match(fields["heartbeat_at"]) is not None


# ---------------------------------------------------------------------------
# resolve_state_dir
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_runtime_root(tmp_path: Path, env_save_restore):
    """Redirect the default runtime root to ``tmp_path/runtime`` via env var
    and reload the ``_session_state`` module so its module-level
    ``DEFAULT_STATE_ROOT`` picks up the new value. Real reload, no monkeypatch.
    """
    import importlib

    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(runtime_root))
    import scitex_agent_container._runners._session_state as _ss

    importlib.reload(_ss)
    return runtime_root


def test_resolve_state_dir_returns_none_when_empty_name():
    # Arrange
    # Act
    resolved = resolve_state_dir("")
    # Assert
    assert resolved is None


def test_resolve_state_dir_finds_home_scope_when_dir_present(
    isolated_runtime_root: Path,
):
    # Arrange
    name = "movement-test-agent"
    (isolated_runtime_root / name).mkdir()
    # Act
    resolved = resolve_state_dir(name)
    # Assert
    assert resolved == isolated_runtime_root / name


def test_resolve_state_dir_returns_none_when_no_candidate_exists(
    isolated_runtime_root: Path,
):
    # Arrange — no per-agent dir was created.
    # Act
    resolved = resolve_state_dir("never-created-agent")
    # Assert
    assert resolved is None
