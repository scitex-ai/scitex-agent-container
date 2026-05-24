"""Tests for dead-session discard + full-history clear helpers.

These are the recovery primitives that fix the ``sac agents restart``
dead-session crash-loop: PR #190's reset cleared only ``session_id``,
leaving a dead uuid in ``session_id_history`` to be re-resumed. The
helpers here clear BOTH (with a backup) so a restart gets a clean slate.

No mocks: real ``_session_id`` helpers against a real state dir under
``tmp_path``. AAA structure, one assertion per test, descriptive names.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._runners import _session_id as sid


def test_discard_dead_session_removes_dead_id_from_history(tmp_path: Path) -> None:
    # Arrange — a single dead id is the only thing in the history.
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "dead-1")
    # Act
    sid.discard_dead_session(state_dir, "dead-1")
    # Assert — the dead id no longer survives in the history.
    assert "dead-1" not in sid.read_session_id_history(state_dir)


def test_discard_dead_session_clears_latest_marker_when_it_is_the_dead_id(
    tmp_path: Path,
) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "dead-1")
    # Act
    sid.discard_dead_session(state_dir, "dead-1")
    # Assert — the resume marker is gone so the next start is fresh.
    assert sid.read_session_id(state_dir) is None


def test_discard_dead_session_preserves_a_newer_valid_marker(tmp_path: Path) -> None:
    # Arrange — the latest marker advanced to a NEW valid id; only the
    # older dead id should be purged, the valid marker left intact.
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "dead-old")
    sid.write_session_id(state_dir, "valid-new")
    # Act
    sid.discard_dead_session(state_dir, "dead-old")
    # Assert
    assert sid.read_session_id(state_dir) == "valid-new"


def test_discard_dead_session_strips_only_the_dead_id_from_history(
    tmp_path: Path,
) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "dead-old")
    sid.write_session_id(state_dir, "valid-new")
    # Act
    sid.discard_dead_session(state_dir, "dead-old")
    # Assert — the surviving valid id remains, the dead one is gone.
    assert sid.read_session_id_history(state_dir) == ["valid-new"]


def test_discard_dead_session_backs_up_history_before_rewrite(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "dead-1")
    # Act
    sid.discard_dead_session(state_dir, "dead-1")
    # Assert — the audit trail survives in a dead-* side file.
    backups = list(state_dir.glob("session_id_history.dead-*"))
    assert len(backups) == 1


def test_discard_dead_session_returns_false_for_unknown_id(tmp_path: Path) -> None:
    # Arrange — the id to discard never appears anywhere.
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "live-1")
    # Act
    changed = sid.discard_dead_session(state_dir, "ghost")
    # Assert
    assert changed is False


def test_discard_dead_session_returns_false_for_empty_id(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "live-1")
    # Act
    changed = sid.discard_dead_session(state_dir, "")
    # Assert
    assert changed is False


def test_clear_session_history_removes_the_history_file(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "id-A")
    sid.write_session_id(state_dir, "id-B")
    # Act
    sid.clear_session_history(state_dir)
    # Assert
    assert sid.read_session_id_history(state_dir) == []


def test_clear_session_history_backs_up_before_removal(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "id-A")
    # Act
    sid.clear_session_history(state_dir)
    # Assert — the cleared ids are preserved in a dead-* side file.
    backups = list(state_dir.glob("session_id_history.dead-*"))
    assert len(backups) == 1


def test_clear_session_history_returns_false_when_no_history(tmp_path: Path) -> None:
    # Arrange — no history file exists.
    state_dir = tmp_path / "alpha"
    # Act
    removed = sid.clear_session_history(state_dir)
    # Assert
    assert removed is False
