"""Tests for the session-id resume marker + append-only history.

No mocks: every test exercises the real ``_session_id`` helpers against a
real state directory under ``tmp_path``. AAA structure, one assertion
per test, descriptive names.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._runners import _session_id as sid


def test_write_session_id_then_read_returns_same_value(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    # Act
    sid.write_session_id(state_dir, "id-A")
    # Assert
    assert sid.read_session_id(state_dir) == "id-A"


def test_read_session_id_returns_none_when_marker_absent(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    # Act
    result = sid.read_session_id(state_dir)
    # Assert
    assert result is None


def test_read_session_id_history_empty_when_no_history_file(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    # Act
    history = sid.read_session_id_history(state_dir)
    # Assert
    assert history == []


def test_write_session_id_appends_to_history(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    # Act
    sid.write_session_id(state_dir, "id-A")
    # Assert
    assert sid.read_session_id_history(state_dir) == ["id-A"]


def test_history_accumulates_distinct_ids_oldest_first(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    # Act
    sid.write_session_id(state_dir, "id-A")
    sid.write_session_id(state_dir, "id-B")
    # Assert
    assert sid.read_session_id_history(state_dir) == ["id-A", "id-B"]


def test_history_does_not_duplicate_repeated_latest_id(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "id-A")
    # Act — re-writing the same id every turn must not bloat the history.
    sid.write_session_id(state_dir, "id-A")
    # Assert
    assert sid.read_session_id_history(state_dir) == ["id-A"]


def test_history_retains_prior_id_after_fork(tmp_path: Path) -> None:
    # Arrange — id-A recorded, then the SDK "forks" to id-B.
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "id-A")
    # Act
    sid.write_session_id(state_dir, "id-B")
    # Assert — the orphaned prior id is still auditable in the history.
    assert "id-A" in sid.read_session_id_history(state_dir)


def test_latest_marker_advances_to_forked_id(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "id-A")
    # Act
    sid.write_session_id(state_dir, "id-B")
    # Assert — the single overwritten marker tracks the latest (fork).
    assert sid.read_session_id(state_dir) == "id-B"


def test_append_session_id_history_returns_true_on_new_id(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    # Act
    appended = sid.append_session_id_history(state_dir, "id-A")
    # Assert
    assert appended is True


def test_append_session_id_history_returns_false_on_duplicate(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    sid.append_session_id_history(state_dir, "id-A")
    # Act
    appended = sid.append_session_id_history(state_dir, "id-A")
    # Assert
    assert appended is False


def test_append_session_id_history_ignores_empty_id(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    # Act
    sid.append_session_id_history(state_dir, "")
    # Assert
    assert sid.read_session_id_history(state_dir) == []


def test_clear_session_id_removes_latest_marker(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "id-A")
    # Act
    sid.clear_session_id(state_dir)
    # Assert
    assert sid.read_session_id(state_dir) is None


def test_clear_session_id_preserves_history(tmp_path: Path) -> None:
    # Arrange — clearing the resume marker must not wipe the audit trail.
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "id-A")
    # Act
    sid.clear_session_id(state_dir)
    # Assert
    assert sid.read_session_id_history(state_dir) == ["id-A"]


def test_clear_session_id_returns_false_when_nothing_to_remove(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    # Act
    removed = sid.clear_session_id(state_dir)
    # Assert
    assert removed is False
