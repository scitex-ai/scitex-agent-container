"""Tests for ``_never_stop_when_task_remains._loop_guard``.

PA-306 no-mocks: the counter is a real JSON file on a real filesystem,
redirected through the production ``$SCITEX_AGENT_CONTAINER_RUNTIME_DIR``
knob (read at call time by ``runtime_base_dir()``, so the redirect really
takes effect — no import-time constant to defeat it).

The signature is a digest of the hook executable's OPAQUE block text. These
tests treat that text as a blob and never assert anything about its
structure, because its structure belongs to scitex-cards.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._never_stop_when_task_remains._loop_guard import (
    MAX_CONSECUTIVE_BLOCKS,
    clear_blocks,
    record_block,
    signature,
    state_path,
)

from ._fake_detector import isolate_runtime

_TEXT = "Do NOT stop — card-a and card-b remain."


def test_state_lands_under_the_redirected_runtime_dir(env_save_restore, tmp_path: Path):
    # Arrange
    root = isolate_runtime(env_save_restore, tmp_path)
    # Act
    path = state_path("agent-x")
    # Assert
    assert str(path).startswith(str(root))


def test_first_block_counts_one(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    # Act
    count, tripped = record_block("agent-x", _TEXT)
    # Assert
    assert (count, tripped) == (1, False)


def test_identical_text_increments(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    record_block("agent-x", _TEXT)
    # Act
    count, _ = record_block("agent-x", _TEXT)
    # Assert
    assert count == 2


def test_changed_text_resets_the_counter(env_save_restore, tmp_path: Path):
    """A changed block reason IS observable progress — not a stuck loop."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    record_block("agent-x", _TEXT)
    record_block("agent-x", _TEXT)
    # Act
    count, tripped = record_block("agent-x", "Do NOT stop — only card-b remains.")
    # Assert
    assert (count, tripped) == (1, False)


def test_whitespace_drift_is_not_progress(env_save_restore, tmp_path: Path):
    """Trailing-newline noise from a subprocess must not read as progress."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    record_block("agent-x", _TEXT)
    # Act
    count, _ = record_block("agent-x", f"  {_TEXT}\n")
    # Assert
    assert count == 2


def test_guard_trips_after_max_consecutive_blocks(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    for _ in range(MAX_CONSECUTIVE_BLOCKS):
        record_block("agent-x", _TEXT)
    # Act
    count, tripped = record_block("agent-x", _TEXT)
    # Assert
    assert tripped is True and count == MAX_CONSECUTIVE_BLOCKS + 1


def test_guard_does_not_trip_at_exactly_max(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    for _ in range(MAX_CONSECUTIVE_BLOCKS - 1):
        record_block("agent-x", _TEXT)
    # Act
    _, tripped = record_block("agent-x", _TEXT)
    # Assert
    assert tripped is False


def test_tripping_resets_so_the_alarm_fires_once(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    for _ in range(MAX_CONSECUTIVE_BLOCKS + 1):
        record_block("agent-x", _TEXT)
    # Act
    count, tripped = record_block("agent-x", _TEXT)
    # Assert
    assert (count, tripped) == (1, False)


def test_clear_blocks_forgets_history(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    record_block("agent-x", _TEXT)
    record_block("agent-x", _TEXT)
    # Act
    clear_blocks("agent-x")
    count, _ = record_block("agent-x", _TEXT)
    # Assert
    assert count == 1


def test_clear_blocks_on_absent_state_is_silent(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    # Act
    clear_blocks("never-seen-agent")
    # Assert
    assert not state_path("never-seen-agent").exists()


def test_agents_do_not_share_a_counter(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    record_block("agent-x", _TEXT)
    record_block("agent-x", _TEXT)
    # Act
    count, _ = record_block("agent-y", _TEXT)
    # Assert
    assert count == 1


def test_slashes_in_agent_name_cannot_escape_the_state_dir(
    env_save_restore, tmp_path: Path
):
    # Arrange
    root = isolate_runtime(env_save_restore, tmp_path)
    # Act
    path = state_path("../../etc/passwd")
    # Assert
    assert path.parent == root / "never_stop_when_task_remains"


def test_empty_text_has_a_stable_signature(env_save_restore, tmp_path: Path):
    """A contentless block repeating is still a repeat."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    first = signature("")
    # Act
    second = signature("   ")
    # Assert
    assert first == second


def test_state_file_does_not_record_their_payload(env_save_restore, tmp_path: Path):
    """We persist a DIGEST, not their text. Storing the payload would put
    scitex-cards' format into sac's on-disk state, which is the coupling this
    boundary removes — and would leak card content into the runtime tree."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    record_block("agent-x", _TEXT)
    # Act
    written = state_path("agent-x").read_text()
    # Assert
    assert "card-a" not in written


def test_corrupt_state_file_is_treated_as_no_history(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    path = state_path("agent-x")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json")
    # Act
    count, tripped = record_block("agent-x", _TEXT)
    # Assert
    assert (count, tripped) == (1, False)
